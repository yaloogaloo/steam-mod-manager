"""One-shot cleanup: strip image paths out of stored ``mods.mod_files`` JSON."""

from __future__ import annotations

from pathlib import Path

from core.db_manager import DatabaseManager, get_db
from services.importers.image_scanner import IMAGE_EXTENSIONS, is_image_path


def _entry_is_image(path: str = "", filename: str = "", name: str = "") -> bool:
    for candidate in (path, filename, name):
        text = str(candidate or "").strip()
        if not text:
            continue
        if is_image_path(text) or Path(text).suffix.lower() in IMAGE_EXTENSIONS:
            return True
    return False


def cleanup_image_entries_in_mod_files(
    db: DatabaseManager | None = None,
) -> dict[str, int]:
    """
    Remove ``*.png/jpg/jpeg/webp/gif`` entries from every Mod's ``mod_files``.

    Only rewrites SQLite JSON — never deletes files on disk.
    Returns ``{"mods_scanned", "mods_updated", "entries_removed"}``.
    """
    database = db if db is not None else get_db()
    scanned = 0
    updated = 0
    removed = 0

    # Bulk id list only — mutations go through public set_mod_files.
    with database._lock:  # noqa: SLF001
        rows = database._conn.execute(  # noqa: SLF001
            "SELECT mod_id FROM mods"
        ).fetchall()
    mod_ids = [str(row["mod_id"]) for row in rows]

    for mid in mod_ids:
        scanned += 1
        bundle = database.get_mod_files(mid)
        before = len(bundle.files)
        kept = [
            entry
            for entry in bundle.files
            if not _entry_is_image(entry.path, entry.filename, entry.name)
        ]
        dropped = before - len(kept)
        if dropped <= 0:
            continue
        bundle.files = kept
        database.set_mod_files(mid, bundle)
        updated += 1
        removed += dropped

    return {
        "mods_scanned": scanned,
        "mods_updated": updated,
        "entries_removed": removed,
    }


# Task-facing alias
cleanup_mod_files_images = cleanup_image_entries_in_mod_files
