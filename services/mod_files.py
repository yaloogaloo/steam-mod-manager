"""Mod multi-file JSON manager — UI/deploy must not touch ``mod_files`` directly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_MISC,
    FILE_ROLE_NEXUS_OLD,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_UNKNOWN,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    FILE_TYPE_PATCH,
    ModFileEntry,
    ModFilesBundle,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    default_selected_for_role,
    new_file_id,
    normalize_file_role,
    normalize_platform,
)

_OPTIONAL_ROLES = frozenset(
    {
        FILE_ROLE_NEXUS_OPTIONAL,
        FILE_ROLE_NEXUS_MISC,
        FILE_ROLE_NEXUS_OLD,
        FILE_ROLE_GITHUB_DEVELOPER_BUILD,
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    }
)
_MAIN_ROLES = frozenset(
    {
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_STEAM_CONTENT,
    }
)
# Roles that map to Detail panel Main / Source badges (exclusive assignment).
_BADGE_MAIN_ROLES = frozenset(
    {
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_GITHUB_RELEASE_ASSET,
        FILE_ROLE_STEAM_CONTENT,
    }
)
_BADGE_SOURCE_ROLES = frozenset({FILE_ROLE_GITHUB_SOURCE_ARCHIVE})


def main_role_for_platform(platform: str | None) -> str:
    """Canonical Main badge role for ``platform``."""
    plat = normalize_platform(platform) if platform else ""
    if plat == PLATFORM_NEXUS:
        return FILE_ROLE_NEXUS_MAIN
    if plat == PLATFORM_STEAM:
        return FILE_ROLE_STEAM_CONTENT
    # GitHub / mod.io / other / empty → release asset as Main.
    return FILE_ROLE_GITHUB_RELEASE_ASSET


def source_role_for_platform(platform: str | None) -> str:
    """Canonical Source badge role (platform-agnostic archive role)."""
    return FILE_ROLE_GITHUB_SOURCE_ARCHIVE


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

    def get_selected_files(self, mod_id: int | str) -> list[ModFileEntry]:
        return self._database().get_mod_files(mod_id).selected_files()

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

    def set_file_selection(
        self, mod_id: int | str, file_id: str, selected: bool
    ) -> ModFileEntry | None:
        """
        Set deploy selection for one file.

        Updates ``selected_for_deploy`` and ``enabled`` together, then persists.
        """
        fid = str(file_id or "").strip()
        bundle = self._database().get_mod_files(mod_id)
        target = bundle.find(fid)
        if target is None:
            return None
        target.set_selection(selected)
        self._database().set_mod_files(mod_id, bundle)
        return target

    def set_file_enabled(
        self, mod_id: int | str, file_id: str, enabled: bool
    ) -> ModFileEntry | None:
        """Compat alias for :meth:`set_file_selection`."""
        return self.set_file_selection(mod_id, file_id, enabled)

    def set_file_description(
        self, mod_id: int | str, file_id: str, description: str
    ) -> ModFileEntry | None:
        """Persist a free-form note on ``metadata.description`` for Other files."""
        fid = str(file_id or "").strip()
        if not fid:
            return None
        bundle = self._database().get_mod_files(mod_id)
        target = bundle.find(fid)
        if target is None:
            return None
        meta = dict(target.metadata) if isinstance(target.metadata, dict) else {}
        text = str(description or "").strip()
        if text:
            meta["description"] = text
        else:
            meta.pop("description", None)
        target.metadata = meta
        self._database().set_mod_files(mod_id, bundle)
        return target

    def set_file_role_mapping(
        self,
        mod_id: int | str,
        *,
        main_file_id: str | None = None,
        source_file_id: str | None = None,
        platform: str | None = None,
    ) -> ModFilesBundle:
        """
        Assign exclusive Main / Source badge roles.

        At most one file is Main and one is Source. Replaced former Main/Source
        files are reset to ``FILE_ROLE_UNKNOWN``. If both ids point at the same
        file, Main wins and Source is cleared.
        """
        main_id = str(main_file_id or "").strip()
        source_id = str(source_file_id or "").strip()
        if main_id and source_id and main_id == source_id:
            source_id = ""

        plat = platform
        if not plat:
            meta = self._database().get_mod(mod_id)
            plat = getattr(meta, "platform", None) if meta is not None else None
        plat = normalize_platform(plat) if plat else ""
        # Prefer GitHub release/source roles when platform unknown but entries look GitHub.
        if not plat:
            plat = PLATFORM_GITHUB

        main_role = main_role_for_platform(plat)
        source_role = source_role_for_platform(plat)

        bundle = self._database().get_mod_files(mod_id)
        known_ids = {f.id for f in bundle.files}
        if main_id and main_id not in known_ids:
            main_id = ""
        if source_id and source_id not in known_ids:
            source_id = ""

        for entry in bundle.files:
            role = normalize_file_role(entry.file_role)
            if entry.id == main_id:
                entry.file_role = main_role
                entry.set_selection(True)
                continue
            if entry.id == source_id:
                entry.file_role = source_role
                # Source is never deployed.
                entry.set_selection(False)
                continue
            if role in _BADGE_MAIN_ROLES or role in _BADGE_SOURCE_ROLES:
                entry.file_role = FILE_ROLE_UNKNOWN
                entry.set_selection(False)
        return self._database().set_mod_files(mod_id, bundle)

    def toggle_file(self, mod_id: int | str, file_id: str) -> ModFileEntry | None:
        """Flip selection for ``file_id``; returns updated entry or None."""
        fid = str(file_id or "").strip()
        bundle = self._database().get_mod_files(mod_id)
        target = bundle.find(fid)
        if target is None:
            return None
        target.set_selection(not bool(target.is_selected))
        self._database().set_mod_files(mod_id, bundle)
        return target

    def set_all_selection(
        self, mod_id: int | str, selected: bool
    ) -> ModFilesBundle:
        """Select or clear every file entry."""
        bundle = self._database().get_mod_files(mod_id)
        flag = bool(selected)
        for entry in bundle.files:
            entry.set_selection(flag)
        return self._database().set_mod_files(mod_id, bundle)

    def clear_optional_selection(self, mod_id: int | str) -> ModFilesBundle:
        """
        Deselect optional / misc / patch (and Nexus old / GitHub non-release).

        Keeps main / workshop-content / type=main selected.
        """
        bundle = self._database().get_mod_files(mod_id)
        for entry in bundle.files:
            role = normalize_file_role(entry.file_role)
            if role in _MAIN_ROLES or (
                not role and entry.type == FILE_TYPE_MAIN
            ):
                entry.set_selection(True)
                continue
            if (
                role in _OPTIONAL_ROLES
                or entry.type in (FILE_TYPE_OPTIONAL, FILE_TYPE_PATCH)
            ):
                entry.set_selection(False)
                continue
            # GitHub release assets / unknown: keep type=main only
            entry.set_selection(entry.type == FILE_TYPE_MAIN)
        return self._database().set_mod_files(mod_id, bundle)

    def select_main_only(self, mod_id: int | str) -> ModFilesBundle:
        """Select main_file / Nexus main / type=main; clear everything else."""
        bundle = self._database().get_mod_files(mod_id)
        for entry in bundle.files:
            role = normalize_file_role(entry.file_role)
            is_main = (
                role in _MAIN_ROLES
                or role == "main_file"
                or entry.type == FILE_TYPE_MAIN
            )
            entry.set_selection(bool(is_main))
        return self._database().set_mod_files(mod_id, bundle)

    def reset_default_selection(self, mod_id: int | str) -> ModFilesBundle:
        """
        Restore platform defaults:

        - Steam content / Nexus main → selected
        - First GitHub release asset → selected; other release assets off
        - Optional / misc / old / developer / source → off
        """
        bundle = self._database().get_mod_files(mod_id)
        first_release_seen = False
        for entry in bundle.files:
            role = entry.file_role or ""
            if role == FILE_ROLE_GITHUB_RELEASE_ASSET:
                entry.set_selection(not first_release_seen)
                first_release_seen = True
                continue
            if role:
                entry.set_selection(default_selected_for_role(role))
                continue
            # Legacy coarse type fallback
            entry.set_selection(entry.type == FILE_TYPE_MAIN)
        return self._database().set_mod_files(mod_id, bundle)

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
