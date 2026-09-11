"""Isolated deploy stage timing table — instrumentation evidence, not an optimizer."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.game_info import GameInfo
from services.deploy import ModDeployer
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "profile.db")
    manager.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(
    db: DatabaseManager, library: Path, mid: str, *, files: dict[str, bytes]
) -> Path:
    folder = library / "Palworld" / f"Mod{mid}"
    folder.mkdir(parents=True)
    for name, payload in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    created = create_steam_test_mod(
        db, external_id=mid, title=f"Mod{mid}", app_id=1623730, game_name="Palworld"
    )
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title=f"Mod{mid}",
        external_id=mid,
        workspace_id=str(created.workspace_id or mid),
        app_id=1623730,
        game_name="Palworld",
    )
    bind_managed_path(
        db, created.mod_id, folder, title=f"Mod{mid}", game_name="Palworld"
    )
    return folder


def _deploy(tmp_path: Path, db: DatabaseManager, mid: str, files: dict[str, bytes]):
    library = tmp_path / "mod"
    target = tmp_path / "Mods"
    target.mkdir(exist_ok=True)
    _mod_folder(db, library, mid, files=files)
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(tmp_path / "game"),
        mod_path=str(target),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    return out


def _stage_table(out: dict) -> list[dict]:
    timing = out.get("deploy_timing") or {}
    return list(timing.get("stages") or [])


def test_profile_kb_mod(tmp_path: Path, db: DatabaseManager) -> None:
    out = _deploy(tmp_path, db, "37801001", {"a.txt": b"x" * 2048})
    assert out.get("success") is True, out
    stages = {row["stage"] for row in _stage_table(out)}
    assert "copy" in stages
    assert "hash" in stages or "persist" in stages
    assert out.get("source")
    assert out.get("target") or (out.get("deploy_timing") or {}).get("target") is not None


def test_profile_multifile_and_overwrite(tmp_path: Path, db: DatabaseManager) -> None:
    files = {f"f{i}.bin": b"y" * 512 for i in range(12)}
    out1 = _deploy(tmp_path, db, "37801002", files)
    assert out1.get("success") is True, out1
    out2 = ModDeployer(library_root=tmp_path / "mod", db=db).deploy_mod("37801002")
    assert out2.get("success") is True, out2
    names = [row["stage"] for row in _stage_table(out2)]
    assert "backup" in names
    assert "copy" in names
    assert "conflict_scan" in names or "plan" in names


def test_profile_stages_include_required_names(tmp_path: Path, db: DatabaseManager) -> None:
    out = _deploy(tmp_path, db, "37801003", {"z.txt": b"hello"})
    assert out.get("success") is True, out
    names = [row["stage"] for row in _stage_table(out)]
    for required in ("resolve", "plan", "copy", "validate"):
        assert required in names, names
