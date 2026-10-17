"""Steam Sync Junction compatibility — skip self-copy when Workshop is a Junction."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.steam_sync_junction import (
    canonicalize_win_path,
    evaluate_steam_sync_update_copy,
    is_junction,
    paths_equivalent,
    resolve_junction_target,
)
from services.sync import ModSyncService, SyncOptions

WINDOWS = os.name == "nt"


def _managed(library: Path, *, workspace_id: str, title: str = "MyMod") -> Path:
    folder = library / "Game" / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "payload.txt").write_text("smm-real", encoding="utf-8")
    (info / "metadata.json").write_text(
        (
            "{\n"
            f'  "published_file_id": "{workspace_id}",\n'
            f'  "workspace_id": "{workspace_id}",\n'
            f'  "title": "{title}",\n'
            '  "app_id": 262060,\n'
            '  "game_name": "Game",\n'
            '  "source_type": "steam",\n'
            "  \"time_updated\": 1000\n"
            "}"
        ),
        encoding="utf-8",
    )
    return folder


def _workshop(root: Path, workspace_id: str, payload: str = "workshop") -> Path:
    folder = root / workspace_id
    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text(payload, encoding="utf-8")
    return folder


def _make_junction(link: Path, target: Path) -> None:
    if not WINDOWS:
        pytest.skip("Junctions require Windows mklink /J")
    completed = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not is_junction(link):
        pytest.skip(f"mklink /J unavailable: {(completed.stderr or completed.stdout or '').strip()}")


def _service(workshop: Path, library: Path) -> ModSyncService:
    return ModSyncService(workshop, library, client=MagicMock(), archiver=MagicMock())


def _meta(workspace_id: str, source: Path) -> ModMetadata:
    return ModMetadata(
        published_file_id=workspace_id,
        title="MyMod",
        app_id=262060,
        game_name="Game",
        source_path=str(source),
        time_updated=2000,
    )


# ---------------------------------------------------------------------------
# Path helpers (no Junction required)
# ---------------------------------------------------------------------------


def test_paths_equivalent_normalizes_case_and_slashes(tmp_path: Path) -> None:
    folder = tmp_path / "mod" / "暗黑地牢" / "mod_xxx"
    folder.mkdir(parents=True)
    mixed = Path(str(folder).replace("\\", "/")).as_posix()
    if os.name == "nt":
        mixed = mixed[0].swapcase() + mixed[1:]
    assert paths_equivalent(folder, mixed)
    assert canonicalize_win_path(folder) == canonicalize_win_path(mixed)
    other = folder.parent / "another_mod"
    other.mkdir()
    assert not paths_equivalent(folder, other)


def test_missing_destination_is_not_a_junction(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    source = tmp_path / "2853239091"
    source.mkdir()
    decision = evaluate_steam_sync_update_copy(
        source=source,
        destination=missing,
        workspace_id="2853239091",
    )
    assert decision.skip_physical_copy is False
    assert is_junction(missing) is False


def test_plain_directories_do_not_skip(tmp_path: Path) -> None:
    dest = _managed(tmp_path / "lib", workspace_id="111")
    source = _workshop(tmp_path / "ws", "111")
    decision = evaluate_steam_sync_update_copy(
        source=source, destination=dest, workspace_id="111"
    )
    assert decision.skip_physical_copy is False


def test_wrong_workspace_id_folder_name_does_not_skip(tmp_path: Path) -> None:
    dest = _managed(tmp_path / "lib", workspace_id="111")
    source = _workshop(tmp_path / "ws", "999")
    decision = evaluate_steam_sync_update_copy(
        source=source, destination=dest, workspace_id="111"
    )
    assert decision.skip_physical_copy is False


# ---------------------------------------------------------------------------
# _copy_only branches
# ---------------------------------------------------------------------------


def test_unregistered_still_calls_copy_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    source = _workshop(workshop, "333")
    called: list[tuple] = []

    def _copy(self, metadata, **kwargs):
        called.append((metadata.published_file_id, kwargs.get("overwrite_existing"), kwargs.get("destination")))
        dest = library / "Game" / "New"
        dest.mkdir(parents=True)
        metadata.managed_path = str(dest)
        return dest

    monkeypatch.setattr(ModFileManager, "copy_mod", _copy)
    svc = _service(workshop, library)
    hint, managed = svc._copy_only(
        _meta("333", source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {},
    )
    assert hint == "success"
    assert called == [("333", False, None)]
    assert managed.is_dir()


def test_registered_no_update_does_not_call_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    dest = _managed(library, workspace_id="222")
    source = _workshop(workshop, "222")
    called: list[object] = []

    def _boom(*_a, **_k):
        called.append("copy")
        raise AssertionError("copy_mod must not run")

    monkeypatch.setattr(ModFileManager, "copy_mod", _boom)
    svc = _service(workshop, library)
    hint, managed = svc._copy_only(
        _meta("222", source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {"222": dest},
    )
    assert hint == "skipped_incomplete"
    assert managed == dest
    assert called == []


def test_registered_update_plain_destination_calls_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    dest = _managed(library, workspace_id="111")
    source = _workshop(workshop, "111", payload="new")
    called: list[object] = []

    def _copy(self, metadata, **kwargs):
        called.append(kwargs)
        metadata.managed_path = str(dest)
        (dest / "payload.txt").write_text("copied", encoding="utf-8")
        return dest

    monkeypatch.setattr(ModFileManager, "copy_mod", _copy)
    svc = _service(workshop, library)
    svc._force_overwrite_ids.add("111")
    hint, managed = svc._copy_only(
        _meta("111", source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {"111": dest},
    )
    assert hint == "success"
    assert called and called[0].get("overwrite_existing") is True
    assert called[0].get("destination") == dest
    assert managed == dest


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_registered_update_correct_junction_skips_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    workshop.mkdir()
    dest = _managed(library, workspace_id="2853239091")
    link = workshop / "2853239091"
    _make_junction(link, dest)
    assert is_junction(link)
    assert paths_equivalent(resolve_junction_target(link), dest)

    called: list[object] = []

    def _boom(*_a, **_k):
        called.append("copy")
        raise AssertionError("copy_mod must not run")

    monkeypatch.setattr(ModFileManager, "copy_mod", _boom)
    svc = _service(workshop, library)
    svc._force_overwrite_ids.add("2853239091")
    marker = dest / "payload.txt"
    hint, managed = svc._copy_only(
        _meta("2853239091", link),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {"2853239091": dest},
    )
    assert hint == "success"
    assert called == []
    assert managed == dest
    assert marker.read_text(encoding="utf-8") == "smm-real"


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_registered_update_wrong_junction_does_not_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    workshop.mkdir()
    correct = _managed(library, workspace_id="2853239091", title="CORRECT_MOD")
    wrong = _managed(library, workspace_id="999", title="WRONG_MOD")
    link = workshop / "2853239091"
    _make_junction(link, wrong)
    called: list[object] = []

    def _copy(self, metadata, **kwargs):
        called.append(kwargs.get("destination"))
        metadata.managed_path = str(correct)
        return correct

    monkeypatch.setattr(ModFileManager, "copy_mod", _copy)
    svc = _service(workshop, library)
    svc._force_overwrite_ids.add("2853239091")
    hint, _managed_path = svc._copy_only(
        _meta("2853239091", link),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {"2853239091": correct},
    )
    assert hint == "success"
    assert called == [correct]
    assert (wrong / "payload.txt").read_text(encoding="utf-8") == "smm-real"
    assert (correct / "payload.txt").read_text(encoding="utf-8") == "smm-real"


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_junction_target_slash_case_still_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = tmp_path / "ws"
    library = tmp_path / "lib"
    workshop.mkdir()
    dest = _managed(library, workspace_id="2853239091")
    link = workshop / "2853239091"
    _make_junction(link, dest)
    target = resolve_junction_target(link)
    assert target is not None
    mixed = Path(str(dest).replace("\\", "/"))
    assert paths_equivalent(target, mixed)
    decision = evaluate_steam_sync_update_copy(
        source=link, destination=mixed, workspace_id="2853239091"
    )
    assert decision.skip_physical_copy is True

    called: list[object] = []

    def _boom(*_a, **_k):
        called.append("copy")
        raise AssertionError("copy_mod must not run")

    monkeypatch.setattr(ModFileManager, "copy_mod", _boom)
    svc = _service(workshop, library)
    svc._force_overwrite_ids.add("2853239091")
    hint, managed = svc._copy_only(
        _meta("2853239091", link),
        SyncOptions(skip_existing=True, overwrite_files=True),
        {"2853239091": dest},
    )
    assert hint == "success"
    assert called == []
    assert managed == dest


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_windows_junction_helpers_on_real_link(tmp_path: Path) -> None:
    real = tmp_path / "smm_real"
    real.mkdir()
    (real / "a.txt").write_text("ok", encoding="utf-8")
    link = tmp_path / "2853239091"
    _make_junction(link, real)
    assert is_junction(link) is True
    assert is_junction(real) is False
    target = resolve_junction_target(link)
    assert target is not None
    assert paths_equivalent(target, real)
    decision = evaluate_steam_sync_update_copy(
        source=link, destination=real, workspace_id="2853239091"
    )
    assert decision.skip_physical_copy is True
    other = tmp_path / "other"
    other.mkdir()
    wrong = evaluate_steam_sync_update_copy(
        source=link, destination=other, workspace_id="2853239091"
    )
    assert wrong.skip_physical_copy is False
    assert wrong.mismatch is True
