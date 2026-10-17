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
    from services.deploy_identity import frozen_internal_id_for_pk

    frozen = frozen_internal_id_for_pk(result.mod_id, db=db) or result.mod_id
    deploy = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert deploy["success"] is True, deploy

    dest = install_mods / "Nexus7zMod"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
    assert list(dest.rglob("*.7z")) == []


def test_py7zr_bcj2_falls_back_to_7z_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.importers.archive import Py7zrUnsupportedFeatureError

    calls: list[tuple[str, str]] = []

    def boom(_src, _dest) -> None:
        raise Py7zrUnsupportedFeatureError(
            "BCJ2 filter is not supported by py7zr"
        )

    def fake_7z(seven: str, src, dest) -> None:
        calls.append((seven, str(src)))
        (dest / "payload.bin").write_bytes(b"from-7z-exe")

    monkeypatch.setattr(
        "services.importers.archive._extract_7z_with_py7zr", boom
    )
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable",
        lambda: r"C:\Program Files\7-Zip\7z.exe",
    )
    monkeypatch.setattr(
        "services.importers.archive._extract_with_7z", fake_7z
    )

    archive = tmp_path / "bcj2.7z"
    archive.write_bytes(b"7z\xbc\xaf fake")
    out = extract_archive(archive, dest_dir=tmp_path / "out")
    assert (out / "payload.bin").read_bytes() == b"from-7z-exe"
    assert calls, "7z.exe x fallback was not invoked"


def test_real_bcj2_7z_extracts_via_7z_exe(tmp_path: Path) -> None:
    import subprocess

    from services.importers.archive import (
        find_7z_executable,
        is_py7zr_unsupported_feature,
    )

    seven = find_7z_executable()
    if not seven:
        pytest.skip("7z.exe not installed")

    payload = tmp_path / "mod.bin"
    payload.write_bytes(b"MZ" + b"\x90" * 8192 + b"bcj2-payload")
    archive = tmp_path / "bcj2.7z"
    proc = subprocess.run(
        [seven, "a", "-t7z", "-m0=BCJ2", "-m1=LZMA2:d=16k", str(archive), str(payload)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 or not archive.is_file():
        pytest.skip(f"7z.exe could not create a BCJ2 archive: {proc.stderr!r}")

    py7zr_failed = False
    try:
        with py7zr.SevenZipFile(archive, mode="r") as zf:
            zf.extractall(path=tmp_path / "py7zr_out")
    except Exception as exc:  # noqa: BLE001
        py7zr_failed = True
        assert is_py7zr_unsupported_feature(exc) or "bcj2" in str(exc).lower(), exc

    dest = tmp_path / "out"
    extract_archive(archive, dest_dir=dest)
    extracted = list(dest.rglob("mod.bin"))
    assert extracted, "7z.exe fallback did not extract mod.bin"
    assert extracted[0].read_bytes() == payload.read_bytes()
    if not py7zr_failed:
        pytest.skip("this py7zr build already supports BCJ2; fallback path untested")


def test_real_bcj2_7z_deploy_continues(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import subprocess

    from services.importers.archive import find_7z_executable

    seven = find_7z_executable()
    if not seven:
        pytest.skip("7z.exe not installed")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.dll").write_bytes(b"MZ" + b"\x90" * 4096)
    (src_dir / "config.ini").write_text("a=1", encoding="utf-8")
    archive = tmp_path / "bcj2_mod.7z"
    proc = subprocess.run(
        [
            seven,
            "a",
            "-t7z",
            "-m0=BCJ2",
            "-m1=LZMA2:d=16k",
            str(archive),
            str(src_dir / "mod.dll"),
            str(src_dir / "config.ini"),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 or not archive.is_file():
        pytest.skip(f"7z.exe could not create a BCJ2 archive: {proc.stderr!r}")

    library = tmp_path / "library"
    library.mkdir()
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()
    ctx = ImportContext(game_id=100, game_name="SomeGame")
    result = ArchiveImporter(db=db).import_mod(
        archive_path=archive,
        platform=PLATFORM_NEXUS,
        nexus_id="88018",
        title="Bcj2Mod",
        library_root=library,
        context=ctx,
    )
    assert result.success, result.error
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))
    from services.deploy_identity import frozen_internal_id_for_pk

    frozen = frozen_internal_id_for_pk(result.mod_id, db=db) or result.mod_id
    deploy = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert deploy["success"] is True, deploy
    dest = install_mods / "Bcj2Mod"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
