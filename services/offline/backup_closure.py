"""Minimal usable offline Backup snapshot (Phase 5 CAS-only assets).

Durable Backup Offline state::

    offline/index.html
    offline/manifest.json   → Asset Store SHA-256 objects

``offline/assets/*`` is **not** durable Backup state. OPEN may materialize
into ``cache/offline_view/`` for ``file://`` viewing.

Structural files from the LIVE closure (index.html and non-assets refs) are
still copied. Asset bytes go only to the Durable Asset Store.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

BACKUP_OFFLINE_INDEX = "index.html"
MAX_CLOSURE_FILES = 2500
MAX_FILE_BYTES = 32 * 1024 * 1024

# Sidecars that must survive closure prune (Phase 3 Asset Store manifest).
_BACKUP_OFFLINE_KEEP_NAMES = frozenset({"manifest.json"})


def is_backup_offline_sidecar(rel_posix: str) -> bool:
    """True for files that are not part of HTML closure but must not be pruned."""
    name = str(rel_posix or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _BACKUP_OFFLINE_KEEP_NAMES:
        return True
    # Atomic-write temps from write_manifest_atomic
    if name.startswith(".manifest_") and name.endswith(".tmp"):
        return True
    return False


_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.I)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"]?)([^'\"\s)]+)\1\s*\)?",
    re.I,
)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"\n]+from\s+)?|require\s*\(\s*)['"](\.\.?/[^'"]+)['"]""",
    re.I,
)
_PARSE_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}


def is_external_or_non_file_ref(ref: str) -> bool:
    raw = str(ref or "").strip()
    if not raw or raw.startswith("#"):
        return True
    lower = raw.lower()
    if lower.startswith(
        ("http://", "https://", "data:", "javascript:", "mailto:", "blob:", "//")
    ):
        return True
    if raw.startswith("/"):
        return True
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"", "file"}:
        return True
    return False


def _looks_like_abs_fs(ref: str) -> bool:
    raw = str(ref or "").strip()
    lower = raw.lower()
    if lower.startswith("file:"):
        return True
    if len(raw) >= 3 and raw[1] == ":" and raw[0].isalpha() and raw[2] in "\\/":
        return True
    return False


def _strip_ref(ref: str) -> str:
    return unquote(str(ref or "").split("?", 1)[0].split("#", 1)[0].strip())


def _split_srcset(value: str) -> list[str]:
    out: list[str] = []
    for part in str(value or "").split(","):
        token = part.strip().split()
        if token:
            out.append(token[0])
    return out


class _HtmlRefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.css_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {str(k).lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "link" and ad.get("href"):
            self.refs.append(ad["href"])
        if tag_l in {
            "script",
            "img",
            "iframe",
            "source",
            "audio",
            "video",
            "embed",
            "track",
        } and ad.get("src"):
            self.refs.append(ad["src"])
        if tag_l == "object" and ad.get("data"):
            self.refs.append(ad["data"])
        if ad.get("srcset"):
            self.refs.extend(_split_srcset(ad["srcset"]))
        if ad.get("poster"):
            self.refs.append(ad["poster"])
        if ad.get("style"):
            self.css_text.append(ad["style"])

    def handle_data(self, data: str) -> None:
        if "url(" in data.lower() or "@import" in data.lower():
            self.css_text.append(data)


def _typed_refs_from_file(path: Path) -> list[tuple[str, bool]]:
    """Return ``(ref, required)`` pairs. HTML/JS/@import are required; CSS url() is not."""
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if suffix in {".html", ".htm"}:
        parser = _HtmlRefParser()
        try:
            parser.feed(text)
        except Exception:  # noqa: BLE001
            return []
        refs: list[tuple[str, bool]] = [(r, True) for r in parser.refs]
        for block in parser.css_text:
            refs.extend((m.group(2), False) for m in _CSS_URL_RE.finditer(block))
            refs.extend((m.group(2), True) for m in _CSS_IMPORT_RE.finditer(block))
        return refs
    if suffix == ".css":
        refs = [(m.group(2), False) for m in _CSS_URL_RE.finditer(text)]
        refs.extend((m.group(2), True) for m in _CSS_IMPORT_RE.finditer(text))
        return refs
    if suffix in {".js", ".mjs", ".cjs"}:
        return [(m.group(1), True) for m in _JS_IMPORT_RE.finditer(text)]
    return []


def _refs_from_file(path: Path) -> list[str]:
    return [ref for ref, _required in _typed_refs_from_file(path)]


def _safe_resolve(base_dir: Path, ref: str, *, root: Path) -> Path | None:
    if is_external_or_non_file_ref(ref):
        return None
    cleaned = _strip_ref(ref)
    if not cleaned or is_external_or_non_file_ref(cleaned):
        return None
    try:
        candidate = (base_dir / cleaned).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


@dataclass
class ClosureReport:
    files: dict[str, Path] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    required_missing: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)


def collect_offline_closure_report(src_index: Path) -> ClosureReport:
    """Walk index + CSS/JS local refs. Missing relative files are recorded."""
    index = Path(src_index)
    report = ClosureReport()
    if not index.is_file():
        report.missing.append(BACKUP_OFFLINE_INDEX)
        return report
    try:
        root = index.parent.resolve()
        index = index.resolve()
    except OSError:
        report.missing.append(BACKUP_OFFLINE_INDEX)
        return report
    report.files[index.name.replace("\\", "/")] = index
    queue: list[Path] = [index]
    seen: set[str] = {str(index)}
    missing_seen: set[str] = set()
    leak_seen: set[str] = set()

    while queue and len(report.files) < MAX_CLOSURE_FILES:
        current = queue.pop(0)
        if current.suffix.lower() not in _PARSE_SUFFIXES:
            continue
        for ref, required in _typed_refs_from_file(current):
            if is_external_or_non_file_ref(ref):
                continue
            cleaned = _strip_ref(ref)
            if not cleaned or is_external_or_non_file_ref(cleaned):
                continue
            if _looks_like_abs_fs(cleaned):
                if cleaned not in leak_seen:
                    leak_seen.add(cleaned)
                    report.leaks.append(cleaned)
                continue
            try:
                candidate = (current.parent / cleaned).resolve()
            except OSError:
                if cleaned not in missing_seen:
                    missing_seen.add(cleaned)
                    report.missing.append(cleaned)
                    if required:
                        report.required_missing.append(cleaned)
                continue
            try:
                candidate.relative_to(root)
            except ValueError:
                outside = False
                try:
                    outside = candidate.is_file()
                except OSError:
                    outside = False
                if outside:
                    if cleaned not in leak_seen:
                        leak_seen.add(cleaned)
                        report.leaks.append(cleaned)
                elif cleaned not in missing_seen:
                    missing_seen.add(cleaned)
                    report.missing.append(cleaned)
                    if required:
                        report.required_missing.append(cleaned)
                continue
            found = _safe_resolve(current.parent, ref, root=root)
            if found is None:
                if cleaned not in missing_seen:
                    missing_seen.add(cleaned)
                    report.missing.append(cleaned)
                    if required:
                        report.required_missing.append(cleaned)
                continue
            key = str(found)
            if key in seen:
                continue
            try:
                if found.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                rel = found.relative_to(root).as_posix()
            except ValueError:
                continue
            seen.add(key)
            report.files[rel] = found
            if found.suffix.lower() in _PARSE_SUFFIXES:
                queue.append(found)
    return report


def collect_offline_closure(src_index: Path) -> dict[str, Path]:
    """Return ``{relative posix from index parent: source file}`` including index."""
    return collect_offline_closure_report(src_index).files


def _normalize_closure_rel(rel_posix: str) -> str:
    cleaned = str(rel_posix or "").replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _is_backup_asset_rel(rel_posix: str) -> bool:
    cleaned = _normalize_closure_rel(rel_posix).lower()
    return cleaned == "assets" or cleaned.startswith("assets/")


def clear_backup_offline_assets_dir(dest_offline: Path) -> dict[str, int]:
    """
    Remove durable ``offline/assets`` tree under Backup (Phase 5).

    Does not touch ``index.html`` / ``manifest.json``. Safe when absent.
    """
    assets = Path(dest_offline) / "assets"
    removed = 0
    nbytes = 0
    if not assets.exists():
        return {"files": 0, "bytes": 0}
    try:
        if assets.is_dir():
            for path in list(assets.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    nbytes += int(path.stat().st_size)
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
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


def snapshot_offline_closure(src_index: Path, dest_offline: Path) -> str:
    """
    Phase 5/6 CAS-only Backup Offline Snapshot.

    Writes::

        dest/index.html          (and any non-assets structural refs)
        dest/manifest.json       (assets/* → Asset Store SHA-256)

    Does **not** persist ``dest/assets/*``. Legacy asset trees are cleared.

    Phase 6: when LIVE physical ``assets/`` are absent but LIVE
    ``manifest.json`` + Asset Store cover the closure, snapshot from CAS
    without requiring durable ``.info/.../assets``.
    """
    index = Path(src_index)
    dest = Path(dest_offline)
    if not index.is_file():
        return ""
    report = collect_offline_closure_report(index)
    mapping = dict(report.files)
    if not mapping:
        return ""
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create backup offline dir %s: %s", dest, exc)
        return ""

    from core.paths import asset_store_dir
    from services.asset_manifest import (
        AssetManifest,
        AssetReference,
        build_manifest,
        validate_manifest_path,
        verify_manifest_against_store,
        write_manifest_atomic,
    )
    from services.asset_store import AssetStore, AssetStoreError, sha256_file
    from services.backup_asset_migration import (
        backup_offline_manifest_path,
        load_live_manifest,
    )

    store = AssetStore(root=asset_store_dir())
    live_man = load_live_manifest(index)
    live_by_path = {a.path: a for a in (live_man.assets if live_man else [])}

    needed_assets: list[str] = []
    for rel in list(report.files) + list(report.required_missing):
        rel_posix = _normalize_closure_rel(str(rel).replace("\\", "/"))
        if not _is_backup_asset_rel(rel_posix):
            continue
        try:
            needed_assets.append(validate_manifest_path(rel_posix))
        except Exception:  # noqa: BLE001
            continue
    # Stable unique order
    seen_need: set[str] = set()
    needed_unique: list[str] = []
    for p in needed_assets:
        if p not in seen_need:
            seen_need.add(p)
            needed_unique.append(p)

    copied_structural = 0
    refs: list[AssetReference] = []
    issues: list[str] = []

    # Phase 6 fast path: LIVE manifest + CAS covers all closure assets
    # (physical .info/assets may be absent).
    cas_only_live = False
    if live_man is not None and needed_unique:
        if all(p in live_by_path for p in needed_unique):
            subset = [live_by_path[p] for p in needed_unique]
            try:
                candidate = build_manifest(subset)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"live manifest subset failed: {exc}")
                candidate = None
            if candidate is not None:
                store_issues = verify_manifest_against_store(candidate, store)
                if not store_issues:
                    refs = list(candidate.assets)
                    cas_only_live = True
                else:
                    issues.extend(store_issues[:5])

    if not cas_only_live:
        for rel, src in mapping.items():
            rel_posix = _normalize_closure_rel(str(rel).replace("\\", "/"))
            if _is_backup_asset_rel(rel_posix):
                try:
                    size = int(src.stat().st_size)
                except OSError as exc:
                    issues.append(f"unreadable live asset {rel_posix}: {exc}")
                    continue
                if size <= 0:
                    issues.append(f"empty live asset skipped: {rel_posix}")
                    continue
                try:
                    manifest_rel = validate_manifest_path(rel_posix)
                except Exception as exc:  # noqa: BLE001
                    issues.append(f"unsafe asset path {rel_posix}: {exc}")
                    continue
                live_ref = live_by_path.get(manifest_rel)
                try:
                    if live_ref is not None:
                        digest = live_ref.sha256
                        if int(live_ref.size) != size:
                            digest = sha256_file(src)
                            if digest != live_ref.sha256:
                                issues.append(
                                    f"info manifest mismatch {manifest_rel}: "
                                    f"info={live_ref.sha256} live={digest}"
                                )
                                continue
                    else:
                        digest = sha256_file(src)
                except OSError as exc:
                    issues.append(f"hash failed {manifest_rel}: {exc}")
                    continue
                try:
                    store.put_file(src, expected_sha256=digest)
                    store.verify(digest)
                except AssetStoreError as exc:
                    issues.append(f"{manifest_rel}: put/verify failed: {exc}")
                    continue
                refs.append(
                    AssetReference(path=manifest_rel, sha256=digest, size=size)
                )
                continue

            # Structural files (index.html, nested html/js outside assets/, …)
            target = dest / Path(_normalize_closure_rel(str(rel)))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file() and src.is_file():
                    s_st = src.stat()
                    t_st = target.stat()
                    if s_st.st_size == t_st.st_size and int(s_st.st_mtime) == int(
                        t_st.st_mtime
                    ):
                        continue
                shutil.copy2(src, target)
                copied_structural += 1
            except OSError as exc:
                logger.warning(
                    "Failed to copy offline snapshot %s -> %s: %s", src, target, exc
                )

        # Missing on disk but present in LIVE manifest + CAS
        have = {a.path for a in refs}
        for manifest_rel in needed_unique:
            if manifest_rel in have:
                continue
            live_ref = live_by_path.get(manifest_rel)
            if live_ref is None:
                issues.append(f"missing live asset and no manifest ref: {manifest_rel}")
                continue
            try:
                store.verify(live_ref.sha256)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{manifest_rel}: CAS verify failed: {exc}")
                continue
            refs.append(live_ref)
            have.add(manifest_rel)

    else:
        # Structural copy only (assets come from CAS).
        for rel, src in mapping.items():
            rel_posix = _normalize_closure_rel(str(rel).replace("\\", "/"))
            if _is_backup_asset_rel(rel_posix):
                continue
            target = dest / Path(rel_posix)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file() and src.is_file():
                    s_st = src.stat()
                    t_st = target.stat()
                    if s_st.st_size == t_st.st_size and int(s_st.st_mtime) == int(
                        t_st.st_mtime
                    ):
                        continue
                shutil.copy2(src, target)
                copied_structural += 1
            except OSError as exc:
                logger.warning(
                    "Failed to copy offline snapshot %s -> %s: %s", src, target, exc
                )

    if issues and not cas_only_live:
        logger.warning(
            "backup CAS snapshot issues dest=%s count=%s sample=%s",
            dest,
            len(issues),
            issues[:5],
        )
        asset_rels = needed_unique or [
            r for r in mapping if _is_backup_asset_rel(str(r))
        ]
        if asset_rels and not refs:
            logger.error("backup CAS snapshot produced zero asset refs; aborting")
            return ""

    # Prefer full LIVE manifest when it covers the closure asset set.
    if live_man is not None and refs:
        live_paths = {a.path for a in live_man.assets}
        needed = {a.path for a in refs}
        if needed.issubset(live_paths):
            subset = [a for a in live_man.assets if a.path in needed]
            try:
                manifest = build_manifest(subset)
            except Exception as exc:  # noqa: BLE001
                logger.warning("live manifest subset failed: %s", exc)
                manifest = build_manifest(refs)
        else:
            manifest = build_manifest(refs)
    elif refs:
        manifest = build_manifest(refs)
    else:
        # Offline page with no local assets — empty manifest is OK.
        manifest = AssetManifest()

    store_issues = verify_manifest_against_store(manifest, store) if refs else []
    if store_issues:
        logger.error(
            "backup CAS snapshot store verify failed dest=%s: %s",
            dest,
            store_issues[:5],
        )
        return ""

    try:
        write_manifest_atomic(backup_offline_manifest_path(dest), manifest)
    except OSError as exc:
        logger.error("backup manifest write failed dest=%s: %s", dest, exc)
        return ""

    # Phase 5: never leave durable dual-write payload.
    clear_backup_offline_assets_dir(dest)

    # Prune extras except index / structural copies / sidecars.
    allowed = {
        str(rel).replace("\\", "/").lower()
        for rel, _src in mapping.items()
        if not _is_backup_asset_rel(str(rel))
    }
    allowed.add("index.html")
    try:
        for path in list(dest.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(dest).as_posix().lower()
            if rel in allowed or is_backup_offline_sidecar(rel):
                continue
            # Never keep assets/* after Phase 5 snapshot
            if rel.startswith("assets/"):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            try:
                path.unlink()
            except OSError:
                continue
        for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                try:
                    path.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
    except OSError as exc:
        logger.warning("Failed to prune backup offline extras %s: %s", dest, exc)

    backup_index = dest / BACKUP_OFFLINE_INDEX
    try:
        if backup_index.is_file():
            logger.debug(
                "CAS-only offline snapshot structural=%s assets=%s dest=%s",
                copied_structural,
                len(refs),
                dest,
            )
            return str(backup_index.resolve())
    except OSError:
        return ""
    return ""


def _asset_covered_by_manifest_store(
    dest_offline: Path,
    rel: str,
    *,
    store: Any | None = None,
) -> bool:
    """True when Backup manifest + Asset Store cover *rel* (no physical file)."""
    from services.asset_manifest import ManifestError
    from services.asset_store import AssetCorruption, AssetNotFound, AssetStore
    from services.backup_asset_migration import backup_offline_manifest_path
    from core.paths import asset_store_dir

    man_path = backup_offline_manifest_path(dest_offline)
    if not man_path.is_file():
        return False
    try:
        from services.asset_manifest import AssetManifest

        manifest = AssetManifest.from_path(man_path)
    except (OSError, ManifestError):
        return False
    rel_n = _normalize_closure_rel(str(rel).replace("\\", "/"))
    ref = next((a for a in manifest.assets if a.path == rel_n), None)
    if ref is None:
        return False
    cas = store or AssetStore(root=asset_store_dir())
    try:
        obj = cas.verify(ref.sha256)
    except (AssetNotFound, AssetCorruption):
        return False
    return int(obj.size) == int(ref.size)


def validate_offline_snapshot(
    dest_offline: Path,
    *,
    source_index: Path | None = None,
) -> list[str]:
    """Structural + local-closure checks for ``backup/offline/``.

    Phase 5: ``assets/*`` may be absent on disk when Backup ``manifest.json``
    references verified Asset Store objects. Structural files (e.g. index.html)
    must still exist physically.
    """
    dest = Path(dest_offline)
    issues: list[str] = []
    index = dest / BACKUP_OFFLINE_INDEX
    try:
        if not index.is_file():
            return ["offline/index.html missing"]
        if index.stat().st_size <= 0:
            return ["offline/index.html empty"]
        index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"offline/index.html unreadable: {exc}"]

    try:
        root = dest.resolve()
    except OSError as exc:
        return [f"offline dir unreadable: {exc}"]

    mapping = collect_offline_closure_report(index)
    for rel in mapping.files:
        norm = _normalize_closure_rel(rel)
        target = dest / Path(norm)
        if target.is_file():
            try:
                target.resolve().relative_to(root)
            except (OSError, ValueError):
                issues.append(f"resource escapes backup offline: {rel}")
            continue
        if _is_backup_asset_rel(norm) and _asset_covered_by_manifest_store(dest, norm):
            continue
        issues.append(f"missing local resource: {rel}")
    for leak in mapping.leaks:
        issues.append(f"backup html leaks outside snapshot: {leak}")

    src = Path(source_index) if source_index is not None else None
    src_required: set[str] = set()
    if src is not None and src.is_file():
        src_report = collect_offline_closure_report(src)
        src_required = set(src_report.required_missing)
        for rel in src_report.files:
            norm = _normalize_closure_rel(rel)
            if (dest / Path(norm)).is_file():
                continue
            if _is_backup_asset_rel(norm) and _asset_covered_by_manifest_store(
                dest, norm
            ):
                continue
            issues.append(f"missing local resource: {rel}")
    for miss in mapping.required_missing:
        if miss in src_required:
            continue
        norm = _normalize_closure_rel(miss)
        if _is_backup_asset_rel(norm) and _asset_covered_by_manifest_store(dest, norm):
            continue
        issues.append(f"missing local resource: {miss}")
    return issues


def backup_offline_snapshot_valid(
    dest_offline: Path,
    *,
    source_index: Path | None = None,
) -> bool:
    return not validate_offline_snapshot(dest_offline, source_index=source_index)


def usable_backup_offline_index(dest_offline: Path) -> Path | None:
    dest = Path(dest_offline)
    index = dest / BACKUP_OFFLINE_INDEX
    if not dest.is_dir() or not index.is_file():
        return None
    if not backup_offline_snapshot_valid(dest):
        return None
    try:
        return index if index.is_file() else None
    except OSError:
        return None


def probe_backup_manifest_exists(dest_offline: Path | str) -> bool:
    """True when Backup ``offline/manifest.json`` exists. No Store verify."""
    from services.backup_asset_migration import backup_offline_manifest_path

    dest = Path(dest_offline)
    index = dest / BACKUP_OFFLINE_INDEX
    try:
        if not dest.is_dir() or not index.is_file():
            return False
        return backup_offline_manifest_path(dest).is_file()
    except OSError:
        return False


def probe_backup_offline_view_hit(dest_offline: Path | str) -> Path | None:
    """UI-thread Backup cache hit: manifest fingerprint only. No Store verify."""
    from services.asset_manifest import AssetManifest, ManifestError
    from services.backup_asset_migration import backup_offline_manifest_path
    from services.offline_view_cache import (
        backup_view_dirname,
        fingerprint_manifest,
        require_offline_view_path,
        try_offline_view_hit,
    )
    from core.paths import offline_view_cache_dir

    dest = Path(dest_offline)
    index = dest / BACKUP_OFFLINE_INDEX
    man_path = backup_offline_manifest_path(dest)
    try:
        if not dest.is_dir() or not index.is_file() or not man_path.is_file():
            return None
        manifest = AssetManifest.from_path(man_path)
        fp = fingerprint_manifest(manifest)
    except (OSError, ManifestError):
        return None
    view = offline_view_cache_dir() / backup_view_dirname(dest)
    hit = try_offline_view_hit(view, fp, index_name=BACKUP_OFFLINE_INDEX)
    return require_offline_view_path(hit)


def ensure_backup_offline_openable(dest_offline: Path) -> Path | None:
    """
    Return a ``file://``-openable index under ``cache/offline_view``.

    Requires Backup ``offline/manifest.json`` + Asset Store. Never opens
    Backup ``offline/index.html`` in place (leftover ``offline/assets`` is
    not an OPEN source).
    """
    dest = Path(dest_offline)
    index = dest / BACKUP_OFFLINE_INDEX
    try:
        if not dest.is_dir() or not index.is_file():
            return None
    except OSError:
        return None

    from core.paths import asset_store_dir, offline_view_cache_dir
    from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, ManifestError
    from services.asset_store import AssetStore
    from services.backup_asset_migration import (
        backup_offline_manifest_path,
        restore_backup_assets_from_store,
    )
    from services.offline_view_cache import (
        backup_view_dirname,
        fingerprint_manifest,
        invalidate_view_dir,
        require_offline_view_path,
        touch_lru,
        try_offline_view_hit,
        write_fingerprint,
    )

    man_path = backup_offline_manifest_path(dest)
    if not man_path.is_file():
        return None
    try:
        manifest = AssetManifest.from_path(man_path)
        fp = fingerprint_manifest(manifest)
    except (OSError, ManifestError):
        return None

    view = offline_view_cache_dir() / backup_view_dirname(dest)
    hit = try_offline_view_hit(view, fp, index_name=BACKUP_OFFLINE_INDEX)
    if hit is not None:
        return require_offline_view_path(hit)

    try:
        invalidate_view_dir(view)
        view.mkdir(parents=True)
        shutil.copy2(index, view / BACKUP_OFFLINE_INDEX)
        shutil.copy2(man_path, view / MANIFEST_FILENAME)
        write_fingerprint(view, fp)
    except OSError as exc:
        logger.warning("offline_view stage failed: %s", exc)
        return None

    store = AssetStore(root=asset_store_dir())
    restore = restore_backup_assets_from_store(view, store=store)
    if not restore.ok:
        logger.warning("offline_view materialize failed: %s", restore.reason)
        return None
    out = view / BACKUP_OFFLINE_INDEX
    if out.is_file():
        touch_lru(view)
        return require_offline_view_path(out)
    return None


def prune_unreferenced_backup_offline(dest_offline: Path) -> dict[str, int]:
    """Delete backup offline files not referenced by the snapshot index.

    Phase 5: also strips durable ``assets/`` (CAS is SoT). Keeps sidecars.
    """
    dest = Path(dest_offline)
    index = dest / BACKUP_OFFLINE_INDEX
    removed = 0
    nbytes = 0
    if not dest.is_dir() or not index.is_file():
        return {"files": 0, "bytes": 0}
    cleared = clear_backup_offline_assets_dir(dest)
    removed += int(cleared["files"])
    nbytes += int(cleared["bytes"])
    mapping = collect_offline_closure(index)
    allowed = {
        rel.replace("\\", "/").lower()
        for rel in mapping
        if not _is_backup_asset_rel(rel)
    }
    try:
        for path in list(dest.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(dest).as_posix().lower()
            if rel in allowed or is_backup_offline_sidecar(rel):
                continue
            try:
                nbytes += int(path.stat().st_size)
                path.unlink()
                removed += 1
            except OSError:
                continue
        for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                try:
                    path.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
    except OSError:
        pass
    return {"files": removed, "bytes": nbytes}
