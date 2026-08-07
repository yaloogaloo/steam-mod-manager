"""Images must not enter mod_files — cover-only resources."""

from __future__ import annotations

from pathlib import Path

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_TYPE_MAIN,
    ModFileEntry,
    ModFilesBundle,
)
from services.importers.cleanup import cleanup_image_entries_in_mod_files
from services.importers.image_scanner import find_cover_candidate
from services.importers.local_scanner import IMAGE_EXTENSIONS, scan_mod_directory


def test_image_not_mod_file(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / "test.pak").write_bytes(b"pak")
    (folder / "thumbnail.webp").write_bytes(b"WEBP")
    (folder / "preview.png").write_bytes(b"PNG")
    (folder / "icon.gif").write_bytes(b"GIF89a")

    bundle = scan_mod_directory(folder)
    names = {f.filename for f in bundle.files}
    assert names == {"test.pak"}
    assert "thumbnail.webp" not in names
    assert "preview.png" not in names
    assert "icon.gif" not in names

    # Cover scanner still sees images.
    cover = find_cover_candidate(folder)
    assert cover is not None
    assert cover.suffix.lower() in IMAGE_EXTENSIONS


def test_image_extensions_constant() -> None:
    assert IMAGE_EXTENSIONS == {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def test_cleanup_strips_images_from_db(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "cleanup.db")
    try:
        info = db.register_external_mod(
            platform="nexus",
            external_id="1",
            source_url="https://www.nexusmods.com/x/mods/1",
            title="ImgMod",
            app_id=1,
            game_name="Game",
        )
        db.set_mod_files(
            info.mod_id,
            ModFilesBundle(
                files=[
                    ModFileEntry(
                        name="Main",
                        filename="mod.pak",
                        path="mod.pak",
                        type=FILE_TYPE_MAIN,
                        enabled=True,
                    ),
                    ModFileEntry(
                        name="thumb",
                        filename="thumbnail.webp",
                        path="thumbnail.webp",
                        type=FILE_TYPE_MAIN,
                        enabled=False,
                    ),
                    ModFileEntry(
                        name="preview",
                        filename="preview.jpg",
                        path="preview.jpg",
                        type=FILE_TYPE_MAIN,
                        enabled=False,
                    ),
                ]
            ),
        )
        stats = cleanup_image_entries_in_mod_files(db)
        assert stats["entries_removed"] == 2
        assert stats["mods_updated"] == 1
        files = db.get_mod_files(info.mod_id).files
        assert [f.filename for f in files] == ["mod.pak"]
    finally:
        DatabaseManager.reset_instance()
