"""Canonical deploy path contract — source lookup and target projection.

Deploy / undeploy / validate_source / manifest save must share this module.
Does not expand allowed roots. Does not write identity or production paths.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 2

ROOT_KIND_CUSTOM = "custom"
ROOT_KIND_GAME_MODS = "game_mods"
ROOT_KIND_INSTALL = "install"
ROOT_KIND_STAMPS = "stamps"

_WINDOWS = os.name == "nt"


class DeployPathError(ValueError):
    """Target cannot be canonicalized or remapped onto the current allowed root."""


def _norm_part(part: str) -> str:
    return part.casefold() if _WINDOWS else part


def _as_posix_relative(rel: Path) -> str:
    text = rel.as_posix().strip()
    return text.lstrip("/")


def _safe_resolve(path: Path | str) -> Path:
    from services.deploy_op_profile import cached_resolve

    return Path(cached_resolve(path))


def resolve_deploy_identity(internal_id: int | str, *, db: Any) -> str:
    """Canonical deploy entry: Frozen ``internal_id`` → ``mods.mod_id``.

    Business callers pass TEXT ``internal_id``. Digit PK tokens are not
    resolved here — pass ``mod_pk`` to SQL APIs directly.

    Never resolves via ``workspace_id`` / path / folder name.
    """
    from services.identity_service import resolve_mod_pk

    return resolve_mod_pk(internal_id, db=db)


def resolve_deploy_managed_path(
    internal_id: int | str,
    *,
    db: Any,
    library_root: Path | str | None = None,
    file_manager: Any | None = None,
    explicit: Path | str | None = None,
) -> Path | None:
    """
    Deploy source lookup — Path Authority only.

    Delegates to :func:`services.path_lifecycle.resolve_managed_folder`.
    Never calls ``resolve_deploy_identity`` / ``resolve_mod_pk`` to locate a
    folder. Never scans by workspace_id / published_file_id / folder name.
    *file_manager* is unused (kept for call-site compatibility).
    """
    _ = file_manager
    from services.path_lifecycle import resolve_managed_folder

    token = str(internal_id or "").strip()
    if not token:
        logger.info(
            "[DEPLOY] source resolve fn=resolve_deploy_managed_path "
            "input=%s output=None resolved_from=empty_token",
            token,
        )
        return None
    resolved = resolve_managed_folder(
        token,
        hint_path=explicit,
        library_root=library_root,
        db=db,
    )
    path = resolved.path
    if path is not None and path.is_dir():
        out = path.resolve()
        logger.info(
            "[DEPLOY] source resolve fn=resolve_deploy_managed_path "
            "input=%s output=%s resolved_from=%s",
            token,
            out,
            resolved.resolved_from,
        )
        return out
    logger.info(
        "[DEPLOY] source resolve fn=resolve_deploy_managed_path "
        "input=%s output=None resolved_from=%s",
        token,
        resolved.resolved_from or "unresolved",
    )
    return None


def iter_typed_allowed_roots(ctx: DeployContext) -> list[tuple[str, Path]]:
    """Current allowed roots with root_kind, longest path first."""
    typed: list[tuple[str, Path]] = []

    def _add(kind: str, raw: str | Path | None) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            resolved = _safe_resolve(text)
        except OSError:
            return
        typed.append((kind, resolved))

    _add(ROOT_KIND_CUSTOM, ctx.custom_deploy_path)
    _add(ROOT_KIND_GAME_MODS, getattr(ctx.config, "mod_path", "") or "")
    _add(ROOT_KIND_INSTALL, getattr(ctx.config, "install_path", "") or "")
    deploy_type = str(ctx.deploy_type or "").strip().lower()
    app_id = int(getattr(ctx, "app_id", 0) or 0)
    if deploy_type in {"anno_1800", "anno"} or app_id == 916440:
        try:
            from services.deploy_rules.anno import (
                resolve_anno_mods_root,
                resolve_anno_stamps_dir,
            )

            mods_root = resolve_anno_mods_root(ctx.config)
            if mods_root is not None:
                _add(ROOT_KIND_GAME_MODS, mods_root)
            stamps = resolve_anno_stamps_dir()
            _add(ROOT_KIND_STAMPS, stamps)
        except OSError:
            pass

    # Longest path wins (game_mods over install).
    typed.sort(key=lambda item: len(str(item[1])), reverse=True)
    seen: set[str] = set()
    out: list[tuple[str, Path]] = []
    for kind, path in typed:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append((kind, path))
    return out


def root_for_kind(ctx: DeployContext, root_kind: str) -> Path | None:
    kind = str(root_kind or "").strip()
    if not kind:
        return None
    for item_kind, path in iter_typed_allowed_roots(ctx):
        if item_kind == kind:
            return path
    return None


def classify_absolute_target(
    absolute: Path | str,
    ctx: DeployContext,
) -> tuple[str, str, Path] | None:
    """If *absolute* is under a current allowed root, return (kind, relative, resolved)."""
    try:
        resolved = _safe_resolve(absolute)
    except OSError:
        return None
    for kind, root in iter_typed_allowed_roots(ctx):
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if ".." in rel.parts:
            continue
        posix = _as_posix_relative(rel)
        if not posix:
            continue
        return kind, posix, resolved
    return None


def derive_legacy_relative(absolute: Path | str, current_root: Path) -> str | None:
    """
    Derive a relative path from a historical absolute target.

    Requires a contiguous match of the current root's last **two** path
    components (semantic root, e.g. ``Anno 1800/mods`` or ``game/mods``).
    Single-component roots (bare ``mods``) are refused — too ambiguous.
    Rejects ``..``. Never uses the old drive as an allowed root.
    """
    try:
        root = current_root.resolve()
    except OSError:
        return None
    abs_parts = Path(absolute).expanduser().parts
    root_tail = root.parts[1:] if root.anchor else root.parts
    # Need ≥2 components so ``D:\\other\\mods\\Foo`` cannot match ``…\\mods``.
    if len(root_tail) < 2:
        return None
    needle = root_tail[-2:]
    abs_norm = [_norm_part(p) for p in abs_parts]
    needle_norm = [_norm_part(p) for p in needle]
    found = -1
    span = len(needle_norm)
    for index in range(len(abs_norm) - span, -1, -1):
        if abs_norm[index : index + span] == needle_norm:
            found = index
            break
    if found < 0:
        return None
    # Ambiguous if the needle appears more than once in the absolute path.
    earlier = -1
    for index in range(found):
        if abs_norm[index : index + span] == needle_norm:
            earlier = index
            break
    if earlier >= 0:
        return None
    rel_parts = abs_parts[found + span :]
    if not rel_parts:
        return None
    if any(part in {"..", ""} for part in rel_parts):
        return None
    rel = Path(*rel_parts)
    posix = _as_posix_relative(rel)
    if not posix:
        return None
    try:
        projected = project_relative(root, posix)
        projected.relative_to(root)
    except (DeployPathError, ValueError, OSError):
        return None
    return posix


def project_relative(root: Path, relative: str) -> Path:
    posix = str(relative or "").replace("\\", "/").strip().lstrip("/")
    if not posix or posix.startswith("..") or "/../" in f"/{posix}/":
        raise DeployPathError("relative path traversal rejected")
    # Refuse absolute / drive-relative fragments before join.
    probe = Path(posix)
    if probe.is_absolute() or (getattr(probe, "drive", "") or ""):
        raise DeployPathError("absolute relative path rejected")
    parts = probe.parts
    if ".." in parts or any(part in {"..", ""} for part in parts):
        raise DeployPathError("relative path traversal rejected")
    if any(part.endswith(":") for part in parts):
        raise DeployPathError("absolute relative path rejected")
    try:
        resolved_root = root.resolve()
    except OSError as exc:
        raise DeployPathError(f"cannot resolve target root: {root}") from exc
    projected = (resolved_root / Path(*parts)).resolve()
    try:
        projected.relative_to(resolved_root)
    except ValueError as exc:
        raise DeployPathError(
            f"projected target escapes allowed root: {projected}"
        ) from exc
    return projected


def canonicalize_entry_target(
    entry: ManifestFileEntry,
    ctx: DeployContext,
) -> None:
    """Fill root_kind + relative and rewrite target to the current projection."""
    classified = classify_absolute_target(entry.target, ctx)
    if classified is None:
        raise DeployPathError(
            f"target outside allowed deploy roots: {entry.target}"
        )
    kind, relative, resolved = classified
    entry.root_kind = kind
    entry.relative = relative
    entry.target = str(resolved)


def attach_canonical_targets(manifest: DeployManifest, ctx: DeployContext) -> None:
    """Stamp schema v2 fields onto a just-built deploy manifest (current roots)."""
    from services.deploy_op_profile import cached_resolve

    manifest.schema_version = MANIFEST_SCHEMA_VERSION
    lib = cached_resolve(ctx.library_folder())
    content = cached_resolve(ctx.content_root())
    for entry in manifest.files:
        canonicalize_entry_target(entry, ctx)
        _maybe_relativize_source(entry, ctx, lib_root=lib, content_root=content)


def _maybe_relativize_source(
    entry: ManifestFileEntry,
    ctx: DeployContext,
    *,
    lib_root: str = "",
    content_root: str = "",
) -> None:
    raw = str(entry.source or "").strip()
    if not raw:
        return
    from services.deploy_op_profile import cached_resolve

    try:
        src = Path(cached_resolve(raw))
    except OSError:
        return
    roots = [lib_root, content_root]
    if not any(roots):
        roots = [
            cached_resolve(ctx.library_folder()),
            cached_resolve(ctx.content_root()),
        ]
    for root_s in roots:
        if not root_s:
            continue
        try:
            resolved_root = Path(root_s)
            rel = src.relative_to(resolved_root)
        except (ValueError, OSError):
            continue
        if ".." in rel.parts:
            continue
        entry.source = str(src)
        entry.source_relative = _as_posix_relative(rel)
        return


def remap_manifest_targets(manifest: DeployManifest, ctx: DeployContext) -> None:
    """
    Project every entry onto the current allowed root.

    New manifests use stored root_kind + relative.
    Legacy absolute-only entries may derive relative from the current root suffix.
    Failure refuses the whole manifest — no allowed-root expansion, no deletes.
    """
    typed = iter_typed_allowed_roots(ctx)
    if not typed:
        raise DeployPathError("no allowed target roots for undeploy remap")
    for entry in manifest.files:
        remapped = remap_entry_target(entry, ctx, typed_roots=typed)
        entry.root_kind = remapped.root_kind
        entry.relative = remapped.relative
        entry.target = remapped.target


def remap_entry_target(
    entry: ManifestFileEntry,
    ctx: DeployContext,
    *,
    typed_roots: list[tuple[str, Path]] | None = None,
) -> ManifestFileEntry:
    kind = str(entry.root_kind or "").strip()
    relative = str(entry.relative or "").replace("\\", "/").strip().lstrip("/")
    typed = typed_roots if typed_roots is not None else iter_typed_allowed_roots(ctx)

    if kind and relative:
        root = root_for_kind(ctx, kind)
        if root is None:
            raise DeployPathError(
                f"manifest root_kind={kind} has no current allowed root"
            )
        projected = project_relative(root, relative)
        return ManifestFileEntry(
            source=entry.source,
            target=str(projected),
            type=entry.type,
            backup=entry.backup,
            source_hash=entry.source_hash,
            root_kind=kind,
            relative=relative,
            source_relative=entry.source_relative,
        )

    classified = classify_absolute_target(entry.target, ctx)
    if classified is not None:
        found_kind, found_rel, resolved = classified
        return ManifestFileEntry(
            source=entry.source,
            target=str(resolved),
            type=entry.type,
            backup=entry.backup,
            source_hash=entry.source_hash,
            root_kind=found_kind,
            relative=found_rel,
            source_relative=entry.source_relative,
        )

    raw = str(entry.target or "").strip()
    if not raw:
        raise DeployPathError("manifest entry has empty target")
    if ".." in Path(raw).parts:
        raise DeployPathError(f"target path traversal rejected: {raw}")

    candidates: list[ManifestFileEntry] = []
    for found_kind, root in typed:
        derived = derive_legacy_relative(raw, root)
        if not derived:
            continue
        projected = project_relative(root, derived)
        candidates.append(
            ManifestFileEntry(
                source=entry.source,
                target=str(projected),
                type=entry.type,
                backup=entry.backup,
                source_hash=entry.source_hash,
                root_kind=found_kind,
                relative=derived,
                source_relative=entry.source_relative,
            )
        )
    if not candidates:
        raise DeployPathError(
            f"target outside allowed deploy roots: {raw}"
        )
    # Deterministic: multiple semantic roots must agree on the same projection.
    unique_targets = {c.target for c in candidates}
    if len(unique_targets) > 1:
        raise DeployPathError(
            f"ambiguous legacy target cannot be remapped safely: {raw}"
        )
    return candidates[0]


def planned_absolute_targets(
    entries: Iterable[ManifestFileEntry],
) -> list[str]:
    return [str(entry.target) for entry in entries if str(entry.target or "").strip()]
