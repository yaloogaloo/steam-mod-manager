"""Regression: deploy projection, missing-target backup, large-mod apply diags."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.identity import create_steam_test_mod

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services.backup_manager import BackupManager
from services.deploy_apply import apply_file_plan
from services.deploy_file_plan import (
    OP_EXTRACT_MEMBER,
    DeployFilePlan,
    DeployFilePlanEntry,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import get_library_cache, reset_library_cache
from services.mod_projection_events import notify_mod_changed, reset_mod_changed_listeners
from services.mod_source_integrity import enrich_manifest_source_hashes
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    manager = DatabaseManager.instance(tmp_path / "deploy_runtime_reg.db")
    yield manager
    reset_mod_changed_listeners()
    reset_library_cache()
    DatabaseManager.reset_instance()


def _write_mod(folder: Path, mid: str, *, title: str = "DeployReg") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": mid,
                "published_file_id": mid,
                "title": title,
                "display_name": title,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "payload.bin").write_bytes(b"x" * 32)


def test_deployment_refresh_keeps_deployment_records_visible(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Deploy mutation → notify → projection reload keeps deploy + record ids."""
    mid = "7101"
    lib = tmp_path / "library"
    folder = lib / "Game" / f"Mod_{mid}"
    _write_mod(folder, mid, title="RecordVisible")
    db.upsert_game(GameInfo(app_id=99, name="Game", folder_name="Game"))
    create_steam_test_mod(db, external_id=mid, title="RecordVisible", app_id=99)

    db.update_mod_identity_fields(
        mid, folder_present=True, last_known_path=str(folder), app_id=99
    )
    db.update_mod_deploy_status(
        mid,
        deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
        deploy_path="",
        app_id=99,
    )
    record = db.create_deployment_record(99, "pack-a", [mid])
    assert mid in {str(x) for x in db.get_deployment_record_mod_ids(record.id)}

    db.update_mod_deploy_status(
        mid,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "game" / "mods"),
        app_id=99,
    )
    notify_mod_changed(mid)

    cache = get_library_cache()
    card = cache.refresh_projection(mid)
    assert card is not None
    assert card.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert card.deployed is True
    # Deployment records remain queryable (not cleared by projection reload).
    assert mid in {str(x) for x in db.get_deployment_record_mod_ids(record.id)}
    info = db.get_mod_deploy_info(mid)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED


def test_missing_target_file_does_not_fail_backup_stage(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    existing = game / "keep.dll"
    existing.write_bytes(b"original")
    missing = game / "missing.dll"
    assert not missing.exists()

    mgr = BackupManager(managed)
    prep = mgr.prepare_overwrite([existing, missing])
    assert prep.backup_for(existing) is not None
    # Missing destination → backup=None, stage succeeds.
    assert prep.backup_for(missing) is None


def test_missing_target_toctou_file_not_found_skips_backup(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    target = tmp_path / "game" / "ghost.dll"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"soon-gone")

    mgr = BackupManager(managed)

    real_is_file = Path.is_file

    def _is_file_then_vanish(self: Path) -> bool:
        if self.resolve() == target.resolve():
            # Report exists, then delete before hash/copy.
            try:
                target.unlink()
            except OSError:
                pass
            return True
        return real_is_file(self)

    with patch.object(Path, "is_file", _is_file_then_vanish):
        prep = mgr.prepare_overwrite([target])
    assert prep.backup_for(target) is None


def test_deploy_large_mod_reports_copy_progress_no_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply reports diagnostics; source hash enrichment does not rehash zip N times."""
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(12):
            zf.writestr(f"files/f{i:02d}.dat", b"payload" * 64)

    dest = tmp_path / "dest"
    dest.mkdir()
    entries = []
    for i in range(12):
        rel = f"files/f{i:02d}.dat"
        entries.append(
            DeployFilePlanEntry(
                source_relative=rel,
                target_relative=rel,
                target_absolute=str(dest / rel),
                source=str(archive),
                op=OP_EXTRACT_MEMBER,
            )
        )
    plan = DeployFilePlan(
        internal_id="1",
        deploy_type="anno_1800",
        source=str(archive),
        source_kind="zip",
        content_root=str(tmp_path),
        managed_path=str(tmp_path),
        target_root=str(dest),
        files=entries,
    )
    plan.refresh_planned_count()

    extract_calls = {"n": 0}
    from services import deploy_apply as apply_mod

    real_extract = apply_mod.extract_archive_via_core

    def _counting_extract(archive_path, dest_path):
        extract_calls["n"] += 1
        return real_extract(archive_path, dest_path)

    monkeypatch.setattr(apply_mod, "extract_archive_via_core", _counting_extract)

    result = apply_file_plan(plan, staging_parent=tmp_path / "stage")
    assert result.success
    assert result.source_file_count == 12
    assert result.copied_files == 12
    assert result.total_bytes > 0
    assert result.group_timings_ms
    assert extract_calls["n"] == 0  # member stream; no full-archive apply_* extract
    assert not list((tmp_path / "stage").glob("apply_*"))
    for i in range(12):
        assert (dest / f"files/f{i:02d}.dat").is_file()

    # Source-hash memoization: one zip hashed once across N members.
    hash_calls: list[str] = []

    def _count_sha(path: Path, *, chunk: int = 1024 * 1024) -> str:
        del chunk
        hash_calls.append(str(path))
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            data = fh.read()
        digest.update(data)
        return digest.hexdigest()

    manifest = DeployManifest(
        mod_id="1",
        deploy_type="anno_1800",
        deploy_time="t0",
        files=[
            ManifestFileEntry(
                source=str(archive),
                target=str(dest / f"files/f{i:02d}.dat"),
            )
            for i in range(12)
        ],
    )
    with patch(
        "services.mod_source_integrity._sha256_file", side_effect=_count_sha
    ):
        enrich_manifest_source_hashes(manifest)
    assert len(hash_calls) == 1
    assert all(e.source_hash for e in manifest.files)
