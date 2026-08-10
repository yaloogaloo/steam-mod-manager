"""Source-aware ModFileEntry builders for platform importers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from core.mod_platform import (
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_UNKNOWN,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_MODIO,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_OTHER,
    SOURCE_TYPE_STEAM,
    ModFileEntry,
    ModFilesBundle,
    default_selected_for_role,
    normalize_file_role,
    normalize_file_type,
    normalize_source_type,
)
from services.importers.local_scanner import scan_mod_directory


# Metadata keys for archive-as-source FileEntries (stored in mods.mod_files JSON).
META_ARCHIVE_NAME = "archive_name"
META_INTERNAL_PATH = "internal_path"


def build_archive_source_entries(
    archives: Sequence[str | Path | Mapping[str, Any]],
    *,
    source_type: str,
) -> list[ModFileEntry]:
    """
    Build one FileEntry per archive (Nexus / GitHub source unit).

    Does not extract or scan archive members as ordinary files. Provenance is
    kept in ``metadata``: ``archive_name`` + ``internal_path`` (empty = whole
    archive). ``file_role`` stays ``unknown`` for later Files UI editing.
    """
    plat = normalize_source_type(source_type)
    entries: list[ModFileEntry] = []
    for item in archives:
        if isinstance(item, Mapping):
            original_name = str(
                item.get("archive_name") or item.get("filename") or ""
            ).strip()
            stored_name = str(item.get("path") or item.get("filename") or "").strip()
            internal = str(item.get("internal_path") or "").strip()
            if not original_name and stored_name:
                original_name = Path(stored_name).name
            if not stored_name:
                stored_name = original_name
        else:
            path = Path(item)
            original_name = path.name
            stored_name = path.name
            internal = ""
        if not original_name and not stored_name:
            continue
        if not stored_name:
            stored_name = original_name
        if not original_name:
            original_name = Path(stored_name).name
        entries.append(
            ModFileEntry(
                filename=stored_name,
                path=stored_name,
                name=original_name,
                display_name=original_name,
                source_type=plat,
                file_role=FILE_ROLE_UNKNOWN,
                type=FILE_TYPE_OPTIONAL,
                metadata={
                    META_ARCHIVE_NAME: original_name,
                    META_INTERNAL_PATH: internal,
                },
                enabled=True,
                selected_for_deploy=True,
            )
        )
    return entries


def _entry_from_spec(spec: Mapping[str, Any] | ModFileEntry) -> ModFileEntry:
    if isinstance(spec, ModFileEntry):
        return spec
    return ModFileEntry.from_dict(spec)


def _coarse_type_for_role(role: str) -> str:
    if role in (FILE_ROLE_NEXUS_MAIN, FILE_ROLE_STEAM_CONTENT, FILE_ROLE_GITHUB_RELEASE_ASSET):
        return FILE_TYPE_MAIN
    return FILE_TYPE_OPTIONAL


def apply_steam_file_semantics(bundle: ModFilesBundle) -> ModFilesBundle:
    """
    Steam Workshop: single-content semantics.

    Tag every entry as steam / steam_content and keep it selected. Prefer a
    single conceptual Workshop Content entry when the scan produced files.
    """
    if not bundle.files:
        return bundle
    # Collapse to one Workshop Content row — deploy empty-bundle whole-mod is
    # preserved when importers pass an empty bundle; when files exist (folder
    # scan), keep paths but unify role/selection.
    for entry in bundle.files:
        entry.source_type = SOURCE_TYPE_STEAM
        entry.file_role = FILE_ROLE_STEAM_CONTENT
        entry.type = FILE_TYPE_MAIN
        entry.set_selection(True)
        if not entry.display_name or entry.display_name == entry.filename:
            entry.display_name = "Workshop Content"
        if not entry.name or entry.name in (entry.filename, "Main File"):
            entry.name = "Workshop Content"
    # If multi-file scan, keep files but all selected as workshop content.
    # Callers that want true single-entry whole-mod should pass empty bundle.
    return bundle


def build_steam_workshop_bundle(
    folder: str | Path | None = None,
    *,
    whole_mod: bool = True,
) -> ModFilesBundle:
    """
    Build Steam mod_files.

    Default ``whole_mod=True`` → empty bundle (legacy deploy whole folder).
    When ``whole_mod=False`` and *folder* is a directory, scan then annotate.
    """
    if whole_mod or folder is None:
        return ModFilesBundle()
    root = Path(folder)
    if not root.is_dir():
        return ModFilesBundle()
    return apply_steam_file_semantics(scan_mod_directory(root))


def apply_nexus_file_semantics(bundle: ModFilesBundle) -> ModFilesBundle:
    """
    Annotate scanned entries for Nexus import.

    Import does not infer Main/Optional roles — ``file_role=unknown`` until
    the user assigns one. Explicit ``file_entries`` with roles still win via
    :func:`build_nexus_mod_files`.
    """
    for entry in bundle.files:
        entry.source_type = SOURCE_TYPE_NEXUS
        entry.file_role = FILE_ROLE_UNKNOWN
        if not entry.display_name:
            entry.display_name = entry.filename or entry.name or entry.path
        # Keep scanner selection; do not force role-based selection.
        if not entry.metadata:
            entry.metadata = {}
    return bundle


def build_nexus_mod_files(
    folder: str | Path | None = None,
    *,
    file_entries: Sequence[Mapping[str, Any] | ModFileEntry] | None = None,
) -> ModFilesBundle:
    """
    Build Nexus multi-file bundle.

    Prefer explicit *file_entries* with roles (nexus_main / main_file / …).
    When a role is omitted, keep ``file_role=unknown`` (do not guess from
    extension or coarse ``type``). Folder scan also uses ``unknown``.
    """
    if file_entries:
        files: list[ModFileEntry] = []
        for spec in file_entries:
            entry = _entry_from_spec(spec)
            entry.source_type = SOURCE_TYPE_NEXUS
            role = normalize_file_role(entry.file_role) or FILE_ROLE_UNKNOWN
            entry.file_role = role
            if role != FILE_ROLE_UNKNOWN:
                entry.type = _coarse_type_for_role(role)
            else:
                entry.type = normalize_file_type(entry.type)
            # Explicit selected/enabled in spec wins via from_dict; else defaults.
            if isinstance(spec, Mapping) and (
                "selected_for_deploy" in spec or "enabled" in spec
            ):
                pass  # already resolved in from_dict
            else:
                entry.set_selection(default_selected_for_role(role))
            if not entry.display_name:
                entry.display_name = entry.filename or entry.name or entry.path
            if role == FILE_ROLE_NEXUS_MAIN and (
                not entry.display_name or entry.display_name == entry.filename
            ):
                entry.display_name = entry.name or "Main File"
            if not entry.metadata:
                entry.metadata = {}
            files.append(entry)
        return ModFilesBundle(files=files)

    if folder is None:
        return ModFilesBundle()
    root = Path(folder)
    if not root.is_dir():
        return ModFilesBundle()
    return apply_nexus_file_semantics(scan_mod_directory(root))


def apply_modio_file_semantics(bundle: ModFilesBundle) -> ModFilesBundle:
    """Annotate scanned entries as mod.io sources (roles stay unknown)."""
    for entry in bundle.files:
        entry.source_type = SOURCE_TYPE_MODIO
        entry.file_role = FILE_ROLE_UNKNOWN
        if not entry.display_name:
            entry.display_name = entry.filename or entry.name or entry.path
        if not entry.metadata:
            entry.metadata = {}
    return bundle


def build_modio_mod_files(
    folder: str | Path | None = None,
    *,
    file_entries: Sequence[Mapping[str, Any] | ModFileEntry] | None = None,
) -> ModFilesBundle:
    """Build mod.io multi-file bundle (folder scan or explicit entries)."""
    if file_entries:
        files: list[ModFileEntry] = []
        for spec in file_entries:
            entry = _entry_from_spec(spec)
            entry.source_type = SOURCE_TYPE_MODIO
            role = normalize_file_role(entry.file_role) or FILE_ROLE_UNKNOWN
            entry.file_role = role
            entry.type = normalize_file_type(entry.type)
            if isinstance(spec, Mapping) and (
                "selected_for_deploy" in spec or "enabled" in spec
            ):
                pass
            else:
                entry.set_selection(default_selected_for_role(role))
            if not entry.display_name:
                entry.display_name = entry.filename or entry.name or entry.path
            if not entry.metadata:
                entry.metadata = {}
            files.append(entry)
        return ModFilesBundle(files=files)

    if folder is None:
        return ModFilesBundle()
    root = Path(folder)
    if not root.is_dir():
        return ModFilesBundle()
    return apply_modio_file_semantics(scan_mod_directory(root))


def apply_other_file_semantics(bundle: ModFilesBundle) -> ModFilesBundle:
    """Annotate scanned entries as free-form「其它」sources."""
    for entry in bundle.files:
        entry.source_type = SOURCE_TYPE_OTHER
        entry.file_role = FILE_ROLE_UNKNOWN
        if not entry.display_name:
            entry.display_name = entry.filename or entry.name or entry.path
        if not entry.metadata:
            entry.metadata = {}
    return bundle


def build_other_mod_files(
    folder: str | Path | None = None,
    *,
    file_entries: Sequence[Mapping[str, Any] | ModFileEntry] | None = None,
) -> ModFilesBundle:
    """Build「其它」multi-file bundle (folder scan or explicit archive entries)."""
    if file_entries:
        files: list[ModFileEntry] = []
        for spec in file_entries:
            entry = _entry_from_spec(spec)
            entry.source_type = SOURCE_TYPE_OTHER
            role = normalize_file_role(entry.file_role) or FILE_ROLE_UNKNOWN
            entry.file_role = role
            entry.type = normalize_file_type(entry.type)
            if isinstance(spec, Mapping) and (
                "selected_for_deploy" in spec or "enabled" in spec
            ):
                pass
            else:
                entry.set_selection(default_selected_for_role(role))
            if not entry.display_name:
                entry.display_name = entry.filename or entry.name or entry.path
            if not entry.metadata:
                entry.metadata = {}
            files.append(entry)
        return ModFilesBundle(files=files)

    if folder is None:
        return ModFilesBundle()
    root = Path(folder)
    if not root.is_dir():
        return ModFilesBundle()
    return apply_other_file_semantics(scan_mod_directory(root))


def apply_github_file_semantics(bundle: ModFilesBundle) -> ModFilesBundle:
    """
    Annotate scanned entries for GitHub import.

    Do not infer release/dev/source roles on import — ``file_role=unknown``.
    Explicit ``file_entries`` with roles still win via :func:`build_github_mod_files`.
    """
    for entry in bundle.files:
        entry.source_type = SOURCE_TYPE_GITHUB
        entry.file_role = FILE_ROLE_UNKNOWN
        if not entry.display_name:
            entry.display_name = entry.filename or entry.name or entry.path
        if not entry.metadata:
            entry.metadata = {}
    return bundle


def build_github_mod_files(
    folder: str | Path | None = None,
    *,
    file_entries: Sequence[Mapping[str, Any] | ModFileEntry] | None = None,
) -> ModFilesBundle:
    """
    Build GitHub multi-asset bundle.

    Explicit *file_entries* may set roles (github_release_asset / developer_build /
    github_source_archive). When role is omitted, use ``unknown`` — do not guess
    from filename. Folder scan likewise uses ``unknown``.
    """
    if file_entries:
        files: list[ModFileEntry] = []
        first_release = True
        for spec in file_entries:
            entry = _entry_from_spec(spec)
            entry.source_type = SOURCE_TYPE_GITHUB
            role = normalize_file_role(entry.file_role) or FILE_ROLE_UNKNOWN
            entry.file_role = role
            if role != FILE_ROLE_UNKNOWN:
                entry.type = _coarse_type_for_role(role)
            else:
                entry.type = normalize_file_type(entry.type)
            explicit = isinstance(spec, Mapping) and (
                "selected_for_deploy" in spec or "enabled" in spec
            )
            if not explicit:
                if role == FILE_ROLE_GITHUB_RELEASE_ASSET:
                    entry.set_selection(first_release)
                    first_release = False
                else:
                    entry.set_selection(default_selected_for_role(role))
            elif role == FILE_ROLE_GITHUB_RELEASE_ASSET:
                first_release = False
            if not entry.display_name:
                entry.display_name = entry.filename or entry.name or entry.path
            if not entry.metadata:
                entry.metadata = {}
            files.append(entry)
        return ModFilesBundle(files=files)

    if folder is None:
        return ModFilesBundle()
    root = Path(folder)
    if not root.is_dir():
        return ModFilesBundle()
    return apply_github_file_semantics(scan_mod_directory(root))
