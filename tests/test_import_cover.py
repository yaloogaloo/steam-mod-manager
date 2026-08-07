"""Import-time primary cover selection."""

from __future__ import annotations

from pathlib import Path

from services.file_ops import COVER_BASENAME, INFO_DIR_NAME
from services.importers.image_scanner import find_cover_candidate, install_cover_from_source
from services.importers.materialize import materialize_imported_mod


def _write_png(path: Path, size: int = 64) -> None:
    # Minimal valid 1x1 PNG; size padding via repeated bytes for "largest" tests.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + (b"\x00" * max(0, size - len(png))))


def test_find_png_and_cover_priority(tmp_path: Path) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    _write_png(src / "screenshot.png", size=5000)
    _write_png(src / "thumbnail.png", size=200)
    chosen = find_cover_candidate(src)
    assert chosen is not None
    assert chosen.name == "thumbnail.png"


def test_largest_when_no_priority_name(tmp_path: Path) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    _write_png(src / "a.png", size=100)
    _write_png(src / "big.png", size=8000)
    chosen = find_cover_candidate(src)
    assert chosen is not None
    assert chosen.name == "big.png"


def test_install_cover_and_no_image_fallback(tmp_path: Path) -> None:
    src = tmp_path / "with_cover"
    src.mkdir()
    _write_png(src / "cover.png", size=300)

    dest = tmp_path / "managed" / "Game" / "ModA"
    dest.mkdir(parents=True)
    installed = install_cover_from_source(src, dest)
    assert installed is not None
    assert installed.name.startswith(COVER_BASENAME)
    assert installed.parent.name == INFO_DIR_NAME
    assert installed.is_file()

    empty = tmp_path / "empty_mod"
    empty.mkdir()
    assert find_cover_candidate(empty) is None
    assert install_cover_from_source(empty, dest) is None


def test_materialize_copies_primary_cover(tmp_path: Path) -> None:
    src = tmp_path / "import_src"
    src.mkdir()
    _write_png(src / "preview_icon.png", size=400)
    (src / "mod.pak").write_bytes(b"pak")

    lib = tmp_path / "library"
    dest = materialize_imported_mod(
        library_root=lib,
        mod_id="9000000000000336",
        title="Pal Analyzer",
        game_name="Palworld",
        source_folder=src,
    )
    cover = dest / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert cover.is_file()
