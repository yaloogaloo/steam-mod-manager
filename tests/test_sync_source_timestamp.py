"""Tests for Steam Workshop source timestamp comparison and sync decisions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.models import ModMetadata
from core.scanner import ScannedMod, WorkshopScanner
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.source_timestamp import (
    SourceTimestampDecision,
    compare_source_timestamps,
    normalize_source_timestamp,
)
from services.sync import ModSyncService, SyncOptions


# ---------------------------------------------------------------------------
# Pure comparison (test matrix A–I)
# ---------------------------------------------------------------------------


def test_new_mod_local_missing_incomplete_is_updated() -> None:
    decision = compare_source_timestamps(
        1700000100,
        None,
        local_sync_complete=False,
    )
    assert decision == SourceTimestampDecision.UPDATED


def test_existing_complete_local_missing_is_baseline() -> None:
    decision = compare_source_timestamps(
        1700000100,
        None,
        local_sync_complete=True,
    )
    assert decision == SourceTimestampDecision.BASELINE


def test_same_timestamp_skipped() -> None:
    decision = compare_source_timestamps(1000, 1000, local_sync_complete=True)
    assert decision == SourceTimestampDecision.SKIPPED


def test_steam_updated() -> None:
    decision = compare_source_timestamps(2000, 1000, local_sync_complete=True)
    assert decision == SourceTimestampDecision.UPDATED


def test_steam_not_updated_local_ahead_skipped() -> None:
    decision = compare_source_timestamps(900, 1000, local_sync_complete=True)
    assert decision == SourceTimestampDecision.SKIPPED


def test_both_missing_unknown() -> None:
    decision = compare_source_timestamps(None, None, local_sync_complete=True)
    assert decision == SourceTimestampDecision.UNKNOWN


def test_steam_missing_local_present_unknown() -> None:
    decision = compare_source_timestamps(None, 1000, local_sync_complete=True)
    assert decision == SourceTimestampDecision.UNKNOWN


def test_normalize_rejects_zero() -> None:
    assert normalize_source_timestamp(0) is None
    assert normalize_source_timestamp(-1) is None


# ---------------------------------------------------------------------------
# Scanner: single stat per mod dir (no rglob)
# ---------------------------------------------------------------------------


def test_scanner_attaches_source_dir_mtime(tmp_path: Path) -> None:
    ws = tmp_path / "content" / "424242" / "99001"
    ws.mkdir(parents=True)
    (ws / "mod.dat").write_text("x", encoding="utf-8")
    scanned = WorkshopScanner(tmp_path / "content" / "424242").scan(recursive=False)
    assert len(scanned) == 1
    assert scanned[0].published_file_id == "99001"
    assert scanned[0].source_dir_mtime is not None
    assert scanned[0].source_dir_mtime == normalize_source_timestamp(ws.stat().st_mtime)


# ---------------------------------------------------------------------------
# Sync integration (mocked network)
# ---------------------------------------------------------------------------


def _write_managed_mod(
    library: Path,
    *,
    mod_id: str,
    time_updated: int = 0,
) -> Path:
    mod_dir = library / "Game" / f"Mod_{mod_id}"
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "payload.txt").write_text("data", encoding="utf-8")
    (info / "metadata.json").write_text(
        (
            "{\n"
            f'  "published_file_id": "{mod_id}",\n'
            f'  "title": "Test Mod",\n'
            f'  "app_id": 1,\n'
            f'  "game_name": "Game",\n'
            f'  "time_updated": {time_updated}\n'
            "}"
        ),
        encoding="utf-8",
    )
    return mod_dir


def _write_workshop_mod(ws_root: Path, *, mod_id: str) -> Path:
    mod_dir = ws_root / mod_id
    mod_dir.mkdir(parents=True)
    (mod_dir / "payload.txt").write_text("steam", encoding="utf-8")
    return mod_dir


@pytest.fixture()
def sync_env(tmp_path: Path):
    workshop = tmp_path / "workshop" / "content" / "424242"
    workshop.mkdir(parents=True)
    library = tmp_path / "library"
    library.mkdir()
    client = MagicMock()
    svc = ModSyncService(workshop, library, client=client, archiver=MagicMock())
    return svc, workshop, library, client


def test_sync_skips_when_timestamps_equal(sync_env) -> None:
    svc, workshop, library, client = sync_env
    mod_id = "88001"
    _write_workshop_mod(workshop, mod_id=mod_id)
    _write_managed_mod(library, mod_id=mod_id, time_updated=1700000000)
    client.refresh_details.return_value = [
        ModMetadata(published_file_id=mod_id, time_updated=1700000000, title="Test Mod")
    ]

    result = svc.sync(SyncOptions(skip_existing=True, download_covers=False))

    assert len(result.skipped) == 1
    assert len(result.updated) == 0
    assert len(result.success) == 0
    client.get_details_batch.assert_not_called()


def test_sync_detects_steam_update_and_overwrites(sync_env) -> None:
    svc, workshop, library, client = sync_env
    mod_id = "88002"
    ws_dir = _write_workshop_mod(workshop, mod_id=mod_id)
    managed = _write_managed_mod(library, mod_id=mod_id, time_updated=1700000000)

    client.refresh_details.return_value = [
        ModMetadata(
            published_file_id=mod_id,
            time_updated=1700001000,
            title="Test Mod",
            app_id=1,
            game_name="Game",
        )
    ]
    client.get_details_batch.return_value = client.refresh_details.return_value
    client.resolve_game_names = MagicMock()

    result = svc.sync(SyncOptions(skip_existing=True, download_covers=False))

    assert len(result.updated) == 1
    assert managed.joinpath("payload.txt").read_text(encoding="utf-8") == "steam"
    saved = ModFileManager(library).load_metadata(managed)
    assert saved is not None
    assert saved.time_updated == 1700001000


def test_sync_baseline_does_not_copy(sync_env) -> None:
    svc, workshop, library, client = sync_env
    mod_id = "88003"
    _write_workshop_mod(workshop, mod_id=mod_id)
    managed = _write_managed_mod(library, mod_id=mod_id, time_updated=0)
    before = managed.joinpath("payload.txt").read_text(encoding="utf-8")

    client.refresh_details.return_value = [
        ModMetadata(published_file_id=mod_id, time_updated=1700002000, title="Test Mod")
    ]

    result = svc.sync(SyncOptions(skip_existing=True, download_covers=False))

    assert len(result.baselined) == 1
    assert len(result.updated) == 0
    assert managed.joinpath("payload.txt").read_text(encoding="utf-8") == before
    saved = ModFileManager(library).load_metadata(managed)
    assert saved is not None
    assert saved.time_updated == 1700002000
    client.get_details_batch.assert_not_called()


def test_sync_failed_does_not_advance_timestamp(sync_env, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workshop, library, client = sync_env
    mod_id = "88004"
    _write_workshop_mod(workshop, mod_id=mod_id)
    managed = _write_managed_mod(library, mod_id=mod_id, time_updated=1700000000)

    client.refresh_details.return_value = [
        ModMetadata(
            published_file_id=mod_id,
            time_updated=1700003000,
            title="Test Mod",
            app_id=1,
            game_name="Game",
        )
    ]
    client.get_details_batch.return_value = client.refresh_details.return_value
    client.resolve_game_names = MagicMock()

    def _boom(*_a, **_k):
        raise OSError("copy failed")

    monkeypatch.setattr(svc.files, "copy_mod", _boom)

    result = svc.sync(SyncOptions(skip_existing=True, download_covers=False))

    assert result.failed
    saved = ModFileManager(library).load_metadata(managed)
    assert saved is not None
    assert saved.time_updated == 1700000000


def test_sync_unknown_source_does_not_force_resync(sync_env) -> None:
    svc, workshop, library, client = sync_env
    mod_id = "88005"
    _write_workshop_mod(workshop, mod_id=mod_id)
    _write_managed_mod(library, mod_id=mod_id, time_updated=1700000000)

    client.refresh_details.return_value = [
        ModMetadata(published_file_id=mod_id, time_updated=0, title="Test Mod")
    ]

    result = svc.sync(SyncOptions(skip_existing=True, download_covers=False))

    assert len(result.skipped) == 1
    assert len(result.updated) == 0
    client.get_details_batch.assert_not_called()
