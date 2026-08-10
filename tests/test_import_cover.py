"""Import-time cover — explicit pick preferred; directory sidecars auto-applied."""

from __future__ import annotations

from pathlib import Path

from services.file_ops import COVER_BASENAME, INFO_DIR_NAME
from services.importers.image_scanner import find_cover_candidate, install_cover_from_source
from services.importers.image_picker import install_cover_file
from services.importers.materialize import materialize_imported_mod
from services.importers.importer_base import ImportContext


def _write_png(path: Path, size: int = 64) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + (b"\x00" * max(0, size - len(png))))


def test_legacy_find_cover_candidate_disabled(tmp_path: Path) -> None:
    """Deprecated scanner hook stays inert; materialize uses directory_batch."""
    src = tmp_path / "mod"
    src.mkdir()
    _write_png(src / "thumbnail.png", size=200)
    _write_png(src / "cover.png", size=300)
    assert find_cover_candidate(src) is None


def test_install_explicit_cover(tmp_path: Path) -> None:
    cover = tmp_path / "art.png"
    _write_png(cover, size=300)
    dest = tmp_path / "managed" / "Game" / "ModA"
    dest.mkdir(parents=True)
    installed = install_cover_file(cover, dest)
    assert installed is not None
    assert installed.name.startswith(COVER_BASENAME)
    assert installed.parent.name == INFO_DIR_NAME

    assert install_cover_from_source(tmp_path, dest) is None
    assert install_cover_from_source(tmp_path, dest, cover_source=cover) is not None


def test_materialize_prefers_explicit_cover(tmp_path: Path) -> None:
    src = tmp_path / "import_src"
    src.mkdir()
    _write_png(src / "preview_icon.png", size=400)
    (src / "mod.pak").write_bytes(b"pak")
    cover = tmp_path / "chosen.png"
    _write_png(cover)

    lib = tmp_path / "library"
    dest = materialize_imported_mod(
        library_root=lib,
        mod_id="9000000000000336",
        title="Pal Analyzer",
        game_name="Palworld",
        source_folder=src,
        cover_source=cover,
        context=ImportContext(game_id=1623730, game_name="Palworld"),
    )
    cover_out = dest / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert cover_out.is_file()
    # Sidecar image must not be copied into the managed Mod body.
    assert not (dest / "preview_icon.png").exists()


def test_materialize_auto_cover_from_directory(tmp_path: Path) -> None:
    src = tmp_path / "import_src"
    src.mkdir()
    _write_png(src / "preview_icon.png", size=400)
    (src / "mod.pak").write_bytes(b"pak")

    lib = tmp_path / "library"
    dest = materialize_imported_mod(
        library_root=lib,
        mod_id="9000000000000337",
        title="Auto Cover",
        game_name="Palworld",
        source_folder=src,
        context=ImportContext(game_id=1623730, game_name="Palworld"),
    )
    assert list((dest / INFO_DIR_NAME).glob(f"{COVER_BASENAME}.*"))
    assert (dest / "mod.pak").is_file()
    assert not (dest / "preview_icon.png").exists()
