"""Phase 6/11: CAS-only LIVE ``.info`` offline asset runtime.

Durable LIVE assets::

    .info[/offline]/index.html
    .info[/offline]/manifest.json
            ↓
    data/asset_store/<sha256>

OPEN is only::

    manifest → Asset Store → cache/offline_view → browser

Never opens ``.info/assets`` or Backup ``offline/assets`` in place.
Leftover trees are migration/cleanup only.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.cas_runtime import cas_only_info_asset_runtime
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    ManifestError,
    materialize_manifest_to_dir,
    verify_manifest_against_store,
    write_manifest_atomic,
)
from services.asset_store import AssetStore
from services.info_asset_migration import (
    InfoAssetTree,
    discover_info_asset_trees,
    iter_asset_files,
    migrate_info_asset_tree,
    migrate_info_assets,
)
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)

OFFLINE_ASSET_UNAVAILABLE = "OFFLINE_ASSET_UNAVAILABLE"


class CasFinalizeError(RuntimeError):
    """Capture ingest failed. Staging ``assets/`` has been cleared."""


@dataclass
class OfflineOpenResult:
    """Browser-open outcome. ``path`` is always under ``cache/offline_view`` when ok."""

    ok: bool = False
    path: Path | None = None
    reason: str = ""
    source: str = ""
    repair_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": str(self.path) if self.path is not None else "",
            "reason": self.reason,
            "source": self.source,
            "repair_available": self.repair_available,
        }


@dataclass
class OfflineOpenProbe:
    """UI-thread OPEN probe — no Store verify / materialize / large copy."""

    cache_hit: Path | None = None
    can_materialize: bool = False
    reason: str = ""


def _asset_store_dir() -> Path:
    from core.paths import asset_store_dir

    return asset_store_dir()


def _offline_view_dir() -> Path:
    from core.paths import offline_view_cache_dir

    return offline_view_cache_dir()


@dataclass
class FinalizeLiveResult:
    ok: bool = False
    skipped: bool = False
    reason: str = ""
    offline_root: str = ""
    migrated: bool = False
    cleared_files: int = 0
    cleared_bytes: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "offline_root": self.offline_root,
            "migrated": self.migrated,
            "cleared_files": self.cleared_files,
            "cleared_bytes": self.cleared_bytes,
            "issues": list(self.issues),
        }


def clear_info_assets_dir(offline_root: Path | str) -> dict[str, int]:
    """
    Remove durable ``assets/`` under a LIVE offline root (Phase 6).

    Keeps ``index.html``, ``manifest.json``, and other sidecars.
    """
    root = Path(offline_root)
    assets = root / "assets"
    removed = 0
    nbytes = 0
    if not assets.exists():
        return {"files": 0, "bytes": 0}
    try:
        if assets.is_dir():
            for path in list(iter_asset_files(assets)):
                try:
                    nbytes += int(path.stat().st_size)
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            # Remove empty dirs bottom-up
            for path in sorted(assets.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            try:
                assets.rmdir()
            except OSError:
                pass
        elif assets.is_file():
            try:
                nbytes += int(assets.stat().st_size)
                assets.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return {"files": removed, "bytes": nbytes}


def _managed_root_from_offline(folder: Path) -> Path:
    """Walk up from an offline/info dir to the managed Mod root."""
    managed = Path(folder)
    if managed.name in {"offline", ".info", "info"}:
        managed = managed.parent if managed.name == "offline" else managed.parent
        if folder.name == "offline" and managed.name in {".info", "info"}:
            managed = managed.parent
    return managed


def clear_live_staging_assets(managed_or_offline: Path | str) -> dict[str, int]:
    """Delete leftover LIVE ``assets/`` staging trees. Does not touch Store."""
    folder = Path(managed_or_offline)
    totals = {"files": 0, "bytes": 0}
    managed = _managed_root_from_offline(folder)
    seen: set[str] = set()
    roots: list[Path] = []
    try:
        for tree in discover_info_asset_trees(managed):
            roots.append(Path(tree.offline_root))
    except Exception:  # noqa: BLE001
        logger.warning("discover staging trees failed for %s", folder, exc_info=True)
    roots.append(folder)
    if (folder / "assets").is_dir():
        roots.append(folder)
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        cleared = clear_info_assets_dir(root)
        totals["files"] += int(cleared.get("files", 0))
        totals["bytes"] += int(cleared.get("bytes", 0))
    try:
        from services.offline.staging import cleanup_capture_staging

        cleanup_capture_staging(folder)
        cleanup_capture_staging(managed)
        for root in roots:
            cleanup_capture_staging(root)
    except Exception:  # noqa: BLE001
        logger.warning("capture staging cleanup failed for %s", folder, exc_info=True)
    return totals


def live_manifest_path_for_index(index: Path) -> Path:
    return Path(index).parent / MANIFEST_FILENAME


def probe_live_offline_available(managed_path: Path | str) -> Path | None:
    """Cheap OPEN-capability probe — **no** CAS hash verify / materialize.

    Returns a LIVE index path when Detail may treat offline as present.
    Actual ``file://`` OPEN still goes through :func:`ensure_live_offline_openable`.
    """
    root = Path(managed_path)
    indexes = canonical_live_indexes(root)
    if not indexes:
        return None
    for index in indexes:
        try:
            if live_manifest_path_for_index(index).is_file():
                return index
        except OSError:
            continue
    return None


def load_usable_live_manifest(
    index: Path | None,
    *,
    store: AssetStore | None = None,
) -> AssetManifest | None:
    """Return LIVE manifest only when every object verifies in Asset Store."""
    if index is None or not Path(index).is_file():
        return None
    path = live_manifest_path_for_index(index)
    if not path.is_file():
        return None
    store = store or AssetStore(root=_asset_store_dir())
    try:
        manifest = AssetManifest.from_path(path)
    except (OSError, ManifestError):
        return None
    issues = verify_manifest_against_store(manifest, store)
    if issues:
        return None
    return manifest


def canonical_live_indexes(managed_path: Path | str) -> list[Path]:
    """
    Canonical LIVE indexes under a managed Mod folder.

    Order matches ``resolve_offline_page`` preference, but returns *all*
    present indexes so OPEN can pick the one with a usable CAS manifest.
    """
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

    root = Path(managed_path)
    out: list[Path] = []
    seen: set[str] = set()
    for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
        for candidate in (
            root / info_name / "offline" / "index.html",
            root / info_name / "index.html",
        ):
            try:
                if not candidate.is_file() or candidate.stat().st_size <= 0:
                    continue
                key = str(candidate.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def live_assets_need_materialize(index: Path) -> bool:
    """True when HTML/CSS asset refs are missing beside the LIVE index."""
    return bool(missing_live_asset_refs(index))


def missing_live_asset_refs(index: Path) -> list[str]:
    """Return ``assets/...`` closure refs that are not files beside *index*."""
    from services.offline.backup_closure import (
        _is_backup_asset_rel,
        _normalize_closure_rel,
        collect_offline_closure_report,
    )

    report = collect_offline_closure_report(index)
    root = index.parent
    missing: list[str] = []
    seen: set[str] = set()
    for rel in list(report.files) + list(report.required_missing):
        if not _is_backup_asset_rel(str(rel)):
            continue
        norm = _normalize_closure_rel(str(rel))
        if norm in seen:
            continue
        seen.add(norm)
        if not (root / Path(norm)).is_file():
            missing.append(norm)
    return missing


def finalize_live_offline_to_cas(
    managed_or_offline: Path | str,
    *,
    store: AssetStore | None = None,
    mod_id: str = "",
    clear_assets: bool | None = None,
) -> FinalizeLiveResult:
    """
    After a LIVE offline capture: ingest ``assets/`` → Store + manifest.

    When ``CAS_ONLY_INFO_ASSET_RUNTIME`` is on (or *clear_assets* True),
    clears durable ``assets/`` so they are not left as dual-write SoT.
    """
    out = FinalizeLiveResult()
    folder = Path(managed_or_offline)
    store = store or AssetStore(root=_asset_store_dir())
    do_clear = cas_only_info_asset_runtime() if clear_assets is None else bool(clear_assets)

    # Accept either managed Mod root or an offline root (…/.info or …/.info/offline).
    index = resolve_offline_page(folder)
    if index is None and (folder / "index.html").is_file():
        index = folder / "index.html"
    if index is None:
        out.skipped = True
        out.ok = True
        out.reason = "no offline index"
        return out

    offline_root = index.parent
    out.offline_root = str(offline_root)

    # Migrate any physical trees under the managed path (covers both layouts).
    managed = _managed_root_from_offline(folder)

    mig = migrate_info_assets(managed, store=store, mod_id=mod_id)
    if not mig.ok and not mig.skipped:
        out.ok = False
        out.reason = mig.reason or "migrate failed"
        out.issues = list(mig.issues)
        return out

    from services.offline.staging import capture_assets_dir, cleanup_capture_staging

    staging_assets = capture_assets_dir(offline_root, create=False)
    if staging_assets.is_dir() and any(iter_asset_files(staging_assets)):
        st_mig = migrate_info_asset_tree(
            InfoAssetTree(
                offline_root=offline_root,
                assets_dir=staging_assets,
                manifest_path=offline_root / MANIFEST_FILENAME,
            ),
            store=store,
            mod_id=mod_id,
            managed_path=managed,
        )
        if not st_mig.ok and not st_mig.skipped:
            out.ok = False
            out.reason = st_mig.reason or "staging migrate failed"
            out.issues = list(st_mig.issues)
            return out
        mig = st_mig

    sibling_assets = offline_root / "assets"
    try:
        staging_key = (
            str(staging_assets.resolve()) if staging_assets.exists() else ""
        )
        sibling_key = (
            str(sibling_assets.resolve()) if sibling_assets.exists() else ""
        )
    except OSError:
        staging_key = ""
        sibling_key = ""
    if (
        sibling_assets.is_dir()
        and sibling_key
        and sibling_key != staging_key
        and any(iter_asset_files(sibling_assets))
    ):
        sib_mig = migrate_info_asset_tree(
            InfoAssetTree(
                offline_root=offline_root,
                assets_dir=sibling_assets,
                manifest_path=offline_root / MANIFEST_FILENAME,
            ),
            store=store,
            mod_id=mod_id,
            managed_path=managed,
        )
        if not sib_mig.ok and not sib_mig.skipped:
            out.ok = False
            out.reason = sib_mig.reason or "sibling assets migrate failed"
            out.issues = list(sib_mig.issues)
            return out
        mig = sib_mig

    out.migrated = bool(mig.ok and mig.migrated_files > 0) or bool(
        (offline_root / MANIFEST_FILENAME).is_file()
    )

    # Ensure manifest exists beside this index when assets were under this root.
    man = load_usable_live_manifest(index, store=store)
    source_asset_files = 0
    for tree in discover_info_asset_trees(managed):
        source_asset_files += sum(1 for _ in iter_asset_files(tree.assets_dir))
    if source_asset_files == 0 and (offline_root / "assets").is_dir():
        source_asset_files += sum(
            1 for _ in iter_asset_files(offline_root / "assets")
        )
    staging_assets = capture_assets_dir(offline_root, create=False)
    if staging_assets.is_dir():
        source_asset_files += sum(1 for _ in iter_asset_files(staging_assets))

    if man is None and source_asset_files == 0:
        try:
            write_manifest_atomic(offline_root / MANIFEST_FILENAME, AssetManifest())
        except OSError as exc:
            out.ok = False
            out.reason = f"empty manifest write failed: {exc}"
            return out
        man = load_usable_live_manifest(index, store=store)

    if man is None and source_asset_files > 0:
        # Re-try migrate scoped: discover trees already ran; fail soft
        out.ok = False
        out.reason = "manifest missing or CAS verify failed after migrate"
        return out

    if do_clear:
        # Never clear durable asset *files* unless LIVE CAS is usable.
        if man is None and source_asset_files > 0:
            out.ok = False
            out.reason = "refusing clear without usable LIVE manifest"
            return out
        # Clear all discovered asset trees under managed path.
        for tree in discover_info_asset_trees(managed):
            cleared = clear_info_assets_dir(tree.offline_root)
            out.cleared_files += int(cleared["files"])
            out.cleared_bytes += int(cleared["bytes"])
        # Also clear beside this index if discover missed it.
        cleared = clear_info_assets_dir(offline_root)
        out.cleared_files += int(cleared["files"])
        out.cleared_bytes += int(cleared["bytes"])

    cleanup_capture_staging(offline_root)
    cleanup_capture_staging(folder)

    out.ok = True
    out.reason = "finalized" if do_clear else "migrated_kept_assets"
    return out


def safe_finalize_live_offline(
    managed_or_offline: Path | str,
    *,
    store: AssetStore | None = None,
    mod_id: str = "",
    clear_assets: bool | None = None,
    context: str = "",
) -> FinalizeLiveResult:
    """
    Call :func:`finalize_live_offline_to_cas`. Fail-closed.

    On failure (exception or ``ok=False``): delete staging ``assets/``, log,
    and return ``ok=False``. Callers must not continue a success flow.
    Does not raise for ordinary finalize failures — use
    :func:`require_cas_finalize` when capture must abort.
    """
    result: FinalizeLiveResult | None = None
    try:
        result = finalize_live_offline_to_cas(
            managed_or_offline,
            store=store,
            mod_id=mod_id,
            clear_assets=clear_assets,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "LIVE CAS finalize raised (%s): %s",
            context or managed_or_offline,
            exc,
            exc_info=True,
        )
        result = FinalizeLiveResult()
        result.ok = False
        result.reason = f"exception: {exc}"
    if result is None:
        result = FinalizeLiveResult()
        result.ok = False
        result.reason = "finalize returned nothing"
    if not result.ok and not result.skipped:
        cleared = clear_live_staging_assets(managed_or_offline)
        logger.error(
            "LIVE CAS finalize failed (%s): %s issues=%s staging_cleared=%s",
            context or managed_or_offline,
            result.reason,
            result.issues[:5],
            cleared,
        )
        result.cleared_files += int(cleared.get("files", 0))
        result.cleared_bytes += int(cleared.get("bytes", 0))
    return result


def require_cas_finalize(
    managed_or_offline: Path | str,
    *,
    store: AssetStore | None = None,
    mod_id: str = "",
    clear_assets: bool | None = None,
    context: str = "",
) -> FinalizeLiveResult:
    """Fail-closed finalize: raises if ingest did not succeed."""
    result = safe_finalize_live_offline(
        managed_or_offline,
        store=store,
        mod_id=mod_id,
        clear_assets=clear_assets,
        context=context,
    )
    if result.ok or result.skipped:
        return result
    raise CasFinalizeError(
        f"CAS finalize failed ({context or managed_or_offline}): {result.reason}"
    )


def _resolve_mod_id_for_view(managed_path: Path, *, mod_id: str = "") -> str:
    mid = str(mod_id or "").strip()
    if mid.isdigit():
        return mid
    for name in (".info", "info"):
        marker = Path(managed_path) / name / "internal_id"
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text.isdigit():
            return text
    return ""


def probe_live_offline_view_hit(
    managed_path: Path | str,
    *,
    mod_id: str = "",
) -> Path | None:
    """UI-thread cache hit: parse LIVE manifest + fingerprint. No Store verify."""
    from services.offline_view_cache import (
        fingerprint_manifest,
        require_offline_view_path,
        resolve_live_view_dir,
        try_offline_view_hit,
    )

    root = Path(managed_path)
    if not root.is_dir():
        return None
    indexes = canonical_live_indexes(root)
    view_mod_id = _resolve_mod_id_for_view(root, mod_id=mod_id)
    for index in indexes:
        man_path = live_manifest_path_for_index(index)
        try:
            if not man_path.is_file():
                continue
            manifest = AssetManifest.from_path(man_path)
        except (OSError, ManifestError):
            continue
        fp = fingerprint_manifest(manifest)
        view = resolve_live_view_dir(mod_id=view_mod_id, index=index)
        hit = try_offline_view_hit(view, fp)
        if hit is not None:
            return require_offline_view_path(hit)
    return None


def live_manifest_exists(managed_path: Path | str) -> bool:
    """True when a LIVE ``manifest.json`` sits beside a canonical index."""
    root = Path(managed_path)
    if not root.is_dir():
        return False
    for index in canonical_live_indexes(root):
        try:
            if live_manifest_path_for_index(index).is_file():
                return True
        except OSError:
            continue
    return False


def probe_offline_open(
    managed_path: Path | str | None,
    *,
    mod_id: str = "",
) -> OfflineOpenProbe:
    """UI-thread OPEN probe: cache fingerprint hit and/or manifest presence.

    Never hashes Asset Store objects or materializes.
    """
    from services.offline.backup_closure import (
        probe_backup_manifest_exists,
        probe_backup_offline_view_hit,
    )
    from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root

    out = OfflineOpenProbe()
    root = Path(managed_path) if managed_path is not None else None
    mid = str(mod_id or "").strip()
    if root is not None and root.is_dir():
        hit = probe_live_offline_view_hit(root, mod_id=mid)
        if hit is not None:
            out.cache_hit = hit
            out.can_materialize = True
            return out
        if live_manifest_exists(root):
            out.can_materialize = True
    if mid.isdigit():
        dest = backup_root(mid) / BACKUP_OFFLINE_DIR
        hit = probe_backup_offline_view_hit(dest)
        if hit is not None:
            out.cache_hit = hit
            out.can_materialize = True
            return out
        if probe_backup_manifest_exists(dest):
            out.can_materialize = True
    if not out.can_materialize:
        out.reason = OFFLINE_ASSET_UNAVAILABLE
    return out


def prepare_offline_open(
    managed_path: Path | str | None,
    *,
    mod_id: str = "",
    store: AssetStore | None = None,
    repair_first: bool = False,
) -> OfflineOpenResult:
    """Worker-thread OPEN: verify Store + materialize ``cache/offline_view``.

    Never returns ``.info/assets`` or Backup ``offline/assets`` as the index.
    """
    from services.offline.backup_closure import (
        ensure_backup_offline_openable,
        probe_backup_offline_view_hit,
    )
    from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root, load_backup
    from services.offline_view_cache import require_offline_view_path

    mid = str(mod_id or "").strip()
    root = Path(managed_path) if managed_path is not None else None
    repair_ok = bool(root is not None and root.is_dir()) or mid.isdigit()

    if repair_first and root is not None and root.is_dir():
        try:
            repair_live_from_cas(root, mod_id=mid or "", store=store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("repair_first failed: %s", exc, exc_info=True)

    if root is not None and root.is_dir():
        hit = probe_live_offline_view_hit(root, mod_id=mid)
        if hit is not None:
            return OfflineOpenResult(
                ok=True, path=hit, source="cache_hit", repair_available=repair_ok
            )
        opened = require_offline_view_path(
            ensure_live_offline_openable(root, store=store, mod_id=mid)
        )
        if opened is not None:
            return OfflineOpenResult(
                ok=True, path=opened, source="live_view", repair_available=repair_ok
            )

    if not mid.isdigit() and root is not None:
        # Identity may still be on the path marker.
        mid = _resolve_mod_id_for_view(root, mod_id=mid)

    if mid.isdigit():
        dest = backup_root(mid) / BACKUP_OFFLINE_DIR
        hit = probe_backup_offline_view_hit(dest) if dest.is_dir() else None
        if hit is not None:
            return OfflineOpenResult(
                ok=True, path=hit, source="cache_hit", repair_available=repair_ok
            )
        if dest.is_dir():
            opened = require_offline_view_path(ensure_backup_offline_openable(dest))
            if opened is not None:
                return OfflineOpenResult(
                    ok=True,
                    path=opened,
                    source="backup_view",
                    repair_available=repair_ok,
                )
        backup = load_backup(mid)
        if backup is not None:
            bmid = str(getattr(backup, "mod_id", "") or mid).strip()
            if bmid.isdigit():
                dest = backup_root(bmid) / BACKUP_OFFLINE_DIR
                opened = require_offline_view_path(
                    ensure_backup_offline_openable(dest)
                )
                if opened is not None:
                    return OfflineOpenResult(
                        ok=True,
                        path=opened,
                        source="backup_view",
                        repair_available=repair_ok,
                    )

    return OfflineOpenResult(
        ok=False,
        reason=OFFLINE_ASSET_UNAVAILABLE,
        repair_available=repair_ok,
    )


def ensure_live_offline_openable(
    managed_path: Path | str,
    *,
    store: AssetStore | None = None,
    mod_id: str = "",
) -> Path | None:
    """
    Return a ``file://``-openable LIVE offline index under ``cache/offline_view``.

    Cache hit (fingerprint match + intact view assets): no CAS re-verify.

    Cache miss: verify LIVE manifest against Asset Store, then materialize
    into ``cache/offline_view``. Never writes durable ``.info/.../assets``.
    Never opens LIVE ``.info`` (or leftover ``assets/``) in place.
    """
    from services.offline_view_cache import (
        fingerprint_manifest,
        require_offline_view_path,
        resolve_live_view_dir,
        try_offline_view_hit,
    )

    root = Path(managed_path)
    indexes = canonical_live_indexes(root)
    if not indexes:
        single = resolve_offline_page(root)
        if single is None:
            return None
        indexes = [single]

    store = store or AssetStore(root=_asset_store_dir())
    view_mod_id = _resolve_mod_id_for_view(root, mod_id=mod_id)

    cheap_candidates: list[tuple[Path, AssetManifest, str]] = []
    for index in indexes:
        man_path = live_manifest_path_for_index(index)
        if not man_path.is_file():
            continue
        try:
            manifest = AssetManifest.from_path(man_path)
        except (OSError, ManifestError):
            continue
        fp = fingerprint_manifest(manifest)
        cheap_candidates.append((index, manifest, fp))
        view = resolve_live_view_dir(mod_id=view_mod_id, index=index)
        hit = try_offline_view_hit(view, fp)
        if hit is not None:
            return require_offline_view_path(hit)
    cheap_candidates.sort(key=lambda item: len(item[1].assets), reverse=True)

    cas_candidates: list[tuple[Path, AssetManifest]] = []
    for index, _manifest, _fp in cheap_candidates:
        usable = load_usable_live_manifest(index, store=store)
        if usable is not None:
            cas_candidates.append((index, usable))
    cas_candidates.sort(key=lambda item: len(item[1].assets), reverse=True)

    for index, manifest in cas_candidates:
        opened = _materialize_live_view(
            index, manifest, store=store, mod_id=view_mod_id
        )
        if opened is not None:
            return require_offline_view_path(opened)
    return None


def _materialize_live_view(
    index: Path,
    manifest: AssetManifest,
    *,
    store: AssetStore,
    mod_id: str = "",
) -> Path | None:
    from services.offline_view_cache import (
        fingerprint_manifest,
        invalidate_view_dir,
        resolve_live_view_dir,
        touch_lru,
        write_fingerprint,
    )

    fp = fingerprint_manifest(manifest)
    view = resolve_live_view_dir(mod_id=mod_id, index=index)
    try:
        invalidate_view_dir(view)
        view.mkdir(parents=True)
        shutil.copy2(index, view / "index.html")
        write_manifest_atomic(view / MANIFEST_FILENAME, manifest)
        write_fingerprint(view, fp)
    except OSError as exc:
        logger.warning("live offline_view stage failed: %s", exc)
        return None

    try:
        materialize_manifest_to_dir(manifest, store, view, only_missing=False)
    except ManifestError as exc:
        logger.warning("live offline_view materialize failed: %s", exc)
        return None

    out = view / "index.html"
    if not out.is_file():
        return None
    # Refuse a "successful" empty materialize when HTML still needs assets/*.
    missing = missing_live_asset_refs(out)
    if missing:
        logger.warning(
            "live offline_view incomplete after materialize: %s missing=%s "
            "(manifest assets=%s)",
            out,
            missing[:8],
            len(manifest.assets),
        )
        return None
    touch_lru(view)
    from services.offline_view_cache import require_offline_view_path

    return require_offline_view_path(out)


def repair_live_from_cas(
    managed_path: Path | str,
    *,
    mod_id: str | int = "",
    store: AssetStore | None = None,
) -> "RestoreAssetsResult":
    """
    Repair LIVE offline durability via manifest + Asset Store.

    Verify CAS + ensure LIVE manifest. Never recreates ``.info/.../assets``.
    OPEN uses ``cache/offline_view``.
    """
    from services.backup_asset_migration import (
        RestoreAssetsResult,
        backup_offline_manifest_path,
        backup_root,
    )
    from services.metadata_backup import BACKUP_OFFLINE_DIR

    folder = Path(managed_path)
    store = store or AssetStore(root=_asset_store_dir())
    out = RestoreAssetsResult()

    live_index = resolve_offline_page(folder)
    live_root = live_index.parent if live_index is not None else (folder / ".info")
    live_man_path = live_root / MANIFEST_FILENAME

    manifest: AssetManifest | None = None
    if live_man_path.is_file():
        try:
            candidate = AssetManifest.from_path(live_man_path)
            if not verify_manifest_against_store(candidate, store):
                manifest = candidate
        except (OSError, ManifestError):
            pass

    if manifest is None:
        mid = str(mod_id or "").strip()
        if not mid.isdigit():
            out.reason = "no usable LIVE manifest and no mod_id for Backup"
            return out
        bak = backup_root(mid) / BACKUP_OFFLINE_DIR
        bak_man = backup_offline_manifest_path(bak)
        if not bak_man.is_file():
            out.reason = "backup offline/manifest.json missing"
            return out
        try:
            manifest = AssetManifest.from_path(bak_man)
        except (OSError, ManifestError) as exc:
            out.reason = f"backup manifest unreadable: {exc}"
            return out

    issues = verify_manifest_against_store(manifest, store)
    if issues:
        out.reason = "CAS verification failed"
        out.issues = list(issues)
        return out

    # Integrity materialize into temp view only — never durable .info/assets.
    try:
        from core.paths import cache_temp_dir

        view = (
            cache_temp_dir()
            / f"repair_check_{hashlib.sha256(str(live_root).encode()).hexdigest()[:12]}"
        )
        if view.exists():
            shutil.rmtree(view)
        view.mkdir(parents=True)
        stats = materialize_manifest_to_dir(manifest, store, view, only_missing=False)
        shutil.rmtree(view, ignore_errors=True)
    except ManifestError as exc:
        out.reason = str(exc)
        out.issues.append(str(exc))
        return out
    except OSError as exc:
        out.reason = f"repair materialize check failed: {exc}"
        out.issues.append(str(exc))
        return out

    try:
        live_root.mkdir(parents=True, exist_ok=True)
        write_manifest_atomic(live_man_path, manifest)
    except OSError as exc:
        out.reason = f"CAS ok but LIVE manifest write failed: {exc}"
        out.issues.append(str(exc))
        return out

    out.ok = True
    out.written = 0  # no durable .info/assets
    out.skipped = int(stats.get("written", 0)) + int(stats.get("skipped", 0))
    out.bytes_written = 0
    out.reason = "cas_verified_no_durable_info_assets"
    return out
