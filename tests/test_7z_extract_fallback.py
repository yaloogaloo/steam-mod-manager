"""7z archive extract via py7zr fallback (no system 7-Zip required)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("py7zr")

import py7zr

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.importers.archive import ArchiveImporter, extract_archive, find_mod_root
from services.importers.importer_base import ImportContext


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "seven.db")
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_extract_7z_with_py7zr_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "mod.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "mod.pak").write_bytes(b"pak-bytes")
        (payload / "readme.txt").write_text("hello", encoding="utf-8")
        zf.write(payload / "mod.pak", "mod.pak")
        zf.write(payload / "readme.txt", "readme.txt")

    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable", lambda: None
    )

    out = extract_archive(archive, dest_dir=tmp_path / "out")
    root = find_mod_root(out) or out
    assert (root / "mod.pak").is_file()
    assert (root / "mod.pak").read_bytes() == b"pak-bytes"
    assert (root / "readme.txt").read_text(encoding="utf-8") == "hello"


def test_nexus_7z_import_and_deploy(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nexus .7z imports as archive source; deploy extracts without system 7-Zip."""
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable", lambda: None
    )

    archive = tmp_path / "nexus_mod.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        payload = tmp_path / "src"
        payload.mkdir()
        (payload / "mod.dll").write_bytes(b"MZ")
        (payload / "config.ini").write_text("a=1", encoding="utf-8")
        zf.write(payload / "mod.dll", "mod.dll")
        zf.write(payload / "config.ini", "config.ini")

    library = tmp_path / "library"
    library.mkdir()
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()

    ctx = ImportContext(game_id=100, game_name="SomeGame")
    result = ArchiveImporter(db=db).import_mod(
        archive_path=archive,
        platform=PLATFORM_NEXUS,
        nexus_id="88017",
        title="Nexus7zMod",
        library_root=library,
        context=ctx,
    )
    assert result.success, result.error
    managed = Path(result.managed_path)
    assert any(managed.glob("*.7z"))

    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))
    deploy = ModDeployer(library_root=library, db=db).deploy_mod(result.mod_id)
    assert deploy["success"] is True, deploy

    dest = install_mods / "Nexus7zMod"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
    assert list(dest.rglob("*.7z")) == []
