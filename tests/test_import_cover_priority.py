"""Phase 6 — cover priority among sibling images next to archives."""

from __future__ import annotations

from pathlib import Path

from services.file_ops import COVER_BASENAME, INFO_DIR_NAME
from services.importers.image_scanner import (
    find_cover_candidate,
    find_cover_candidate_in_roots,
    install_cover_from_source,
)
from services.importers.local_scanner import scan_mod_directory


def _write_png(path: Path, size: int = 64) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + (b"\x00" * max(0, size - len(png))))


def test_thumbnail_beats_preview_beside_zip(tmp_path: Path) -> None:
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "mod.zip").write_bytes(b"PK\x03\x04")
    _write_png(folder / "preview.png", size=2000)
    _write_png(folder / "thumbnail.png", size=100)

    chosen = find_cover_candidate(folder, recursive=False)
    assert chosen is not None
    assert chosen.name == "thumbnail.png"

    # Same ranking across extract root + sibling flat root
    extract = tmp_path / "extract"
    extract.mkdir()
    _write_png(extract / "preview.png", size=5000)
    best = find_cover_candidate_in_roots(
        [extract],
        flat_roots=[folder],
    )
    assert best is not None
    assert best.name == "thumbnail.png"


def test_images_excluded_from_mod_files(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / "BP_Mod.pak").write_bytes(b"pak")
    _write_png(folder / "thumbnail.png")
    bundle = scan_mod_directory(folder)
    names = [f.filename for f in bundle.files]
    assert "BP_Mod.pak" in names
    assert "thumbnail.png" not in names


def test_install_cover_from_flat_siblings(tmp_path: Path) -> None:
    src = tmp_path / "extracted"
    src.mkdir()
    (src / "main.pak").write_bytes(b"pak")
    siblings = tmp_path / "download"
    siblings.mkdir()
    _write_png(siblings / "thumbnail.png", size=200)

    dest = tmp_path / "library" / "Game" / "Mod"
    dest.mkdir(parents=True)
    installed = install_cover_from_source(
        src, dest, extra_flat_roots=[siblings]
    )
    assert installed is not None
    assert installed.name.startswith(COVER_BASENAME)
    assert (dest / INFO_DIR_NAME / installed.name).is_file()
