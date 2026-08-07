"""Mod multi-file JSON manager — UI/deploy must not touch ``mod_files`` directly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import ModFileEntry, ModFilesBundle, new_file_id


class ModFileManager:
    """
    Read / mutate ``mods.mod_files`` for one Mod.

    Distinct from :class:`services.file_ops.ModFileManager` (filesystem library ops).
    """

    def __init__(self, db: DatabaseManager | None = None) -> None:
        self._db = db

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def get_files(self, mod_id: int | str) -> list[ModFileEntry]:
        return list(self._database().get_mod_files(mod_id).files)

    def get_enabled_files(self, mod_id: int | str) -> list[ModFileEntry]:
        return self._database().get_mod_files(mod_id).enabled_files()

    def add_file(
        self,
        mod_id: int | str,
        file_info: Mapping[str, Any] | ModFileEntry,
    ) -> ModFileEntry:
        """Append one file entry and persist JSON."""
        if isinstance(file_info, ModFileEntry):
            entry = file_info
            if not entry.id:
                entry.id = new_file_id()
        else:
            entry = ModFileEntry.from_dict(file_info)
        bundle = self._database().get_mod_files(mod_id)
        # Replace same id if present
        bundle.files = [f for f in bundle.files if f.id != entry.id]
        bundle.files.append(entry)
        self._database().set_mod_files(mod_id, bundle)
        return entry

    def remove_file(self, mod_id: int | str, file_id: str) -> bool:
        fid = str(file_id or "").strip()
        if not fid:
            return False
        bundle = self._database().get_mod_files(mod_id)
        before = len(bundle.files)
        bundle.files = [f for f in bundle.files if f.id != fid]
        if len(bundle.files) == before:
            return False
        self._database().set_mod_files(mod_id, bundle)
        return True

    def toggle_file(self, mod_id: int | str, file_id: str) -> ModFileEntry | None:
        """Flip ``enabled`` for ``file_id``; returns updated entry or None."""
        fid = str(file_id or "").strip()
        bundle = self._database().get_mod_files(mod_id)
        target = bundle.find(fid)
        if target is None:
            return None
        target.enabled = not bool(target.enabled)
        self._database().set_mod_files(mod_id, bundle)
        return target

    def set_file_enabled(
        self, mod_id: int | str, file_id: str, enabled: bool
    ) -> ModFileEntry | None:
        fid = str(file_id or "").strip()
        bundle = self._database().get_mod_files(mod_id)
        target = bundle.find(fid)
        if target is None:
            return None
        target.enabled = bool(enabled)
        self._database().set_mod_files(mod_id, bundle)
        return target

    def replace_all(
        self, mod_id: int | str, files: list[ModFileEntry] | ModFilesBundle
    ) -> ModFilesBundle:
        if isinstance(files, ModFilesBundle):
            bundle = files
        else:
            bundle = ModFilesBundle(files=list(files))
        return self._database().set_mod_files(mod_id, bundle)


def scan_folder_to_mod_files(
    folder: str | Path,
    *,
    relative_to: str | Path | None = None,
) -> ModFilesBundle:
    """
    Scan a source folder into a ``ModFilesBundle``.

    Delegates to :func:`services.importers.local_scanner.scan_mod_directory`
    (platform-agnostic). First archive / first file → ``main`` + enabled;
    others → ``optional`` + disabled. Skips ``.info`` / ``info`` / hidden files.
    """
    from services.importers.local_scanner import scan_mod_directory

    return scan_mod_directory(folder, relative_to=relative_to)
