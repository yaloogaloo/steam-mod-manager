"""Sibling cover suggestion — prompt-only, never auto-bound."""

from __future__ import annotations

from pathlib import Path

from services.file_ops import COVER_BASENAME, INFO_DIR_NAME
from services.importers.image_picker import install_cover_file, suggest_sibling_covers
from services.importers.local_scanner import scan_mod_directory


def _write_png(path: Path, size: int = 64) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + (b"\x00" * max(0, size - len(png))))


def test_suggest_sibling_same_stem(tmp_path: Path) -> None:
    folder = tmp_path / "drop"
    folder.mkdir()
    zpath = folder / "mod.zip"
    zpath.write_bytes(b"PK\x03\x04")
    _write_png(folder / "mod.png", size=200)
    _write_png(folder / "thumbnail.png", size=100)

    suggested = suggest_sibling_covers(zpath)
    assert [p.name for p in suggested] == ["mod.png"]


def test_images_excluded_from_mod_files(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / "BP_Mod.zip").write_bytes(b"PK")
    (folder / "BP_Mod.pak").write_bytes(b"pak")
    _write_png(folder / "thumbnail.png")
    bundle = scan_mod_directory(folder)
    names = [f.filename for f in bundle.files]
    assert names == ["BP_Mod.zip"]
    assert "thumbnail.png" not in names


def test_install_explicit_sibling_cover(tmp_path: Path) -> None:
    dest = tmp_path / "library" / "Game" / "Mod"
    dest.mkdir(parents=True)
    siblings = tmp_path / "download"
    siblings.mkdir()
    cover = siblings / "thumbnail.png"
    _write_png(cover, size=200)

    installed = install_cover_file(cover, dest)
    assert installed is not None
    assert installed.name.startswith(COVER_BASENAME)
    assert (dest / INFO_DIR_NAME / installed.name).is_file()
