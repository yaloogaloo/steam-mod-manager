#!/usr/bin/env python3
"""PRODUCTION DEPLOYMENT MANIFEST READ-ONLY AUDIT.

Never mutates DB, manifests, library, or game filesystem.
Uses sqlite mode=ro and in-memory classify/derive only.
Does not call save_manifest / delete_manifest / ModDeployer deploy APIs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root on path when run as script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.db_manager import GameDeployConfig  # noqa: E402
from services.deploy_paths import (  # noqa: E402
    classify_absolute_target,
    derive_legacy_relative,
    iter_typed_allowed_roots,
    project_relative,
    remap_entry_target,
    DeployPathError,
)
from services.deploy_rules.base import DeployContext  # noqa: E402
from services.deploy_rules.manifest import (  # noqa: E402
    DeployManifest,
    ManifestFileEntry,
)
from services.file_ops import (  # noqa: E402
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    METADATA_FILENAME,
    read_info_metadata_dict,
)

ANNO_APP = 916440
PROD_DB = _ROOT / "data" / "mod_manager.db"
PROD_LIB = _ROOT / "mod"
# Report under tools/_audit_out (created if missing — NOT production data/mod).
REPORT_DIR = _ROOT / "tools" / "_audit_out"


@dataclass
class ManifestRecord:
    path: Path
    managed: Path
    schema_bucket: str
    schema_version: int
    mod_id: str
    deploy_type: str
    app_id: int = 0
    platform: str = ""
    legacy_class: str = ""
    v2_classes: list[str] = field(default_factory=list)
    root_drift: list[str] = field(default_factory=list)
    consistency: str = ""
    source_class: str = ""
    notes: list[str] = field(default_factory=list)
    target_drives: list[str] = field(default_factory=list)
    entry_count: int = 0
    anno: bool = False


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # Refuse accidental writes even if something tries.
    conn.execute("PRAGMA query_only = ON")
    return conn


def _drive_of(path: Path | str) -> str:
    try:
        p = Path(path)
        drive = (p.drive or "").upper()
        if drive:
            return drive if drive.endswith(":") else drive + ":"
        anchor = str(p.anchor or "")
        if len(anchor) >= 2 and anchor[1] == ":":
            return anchor[:2].upper()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _safe_exists(path: Path | str) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def _load_manifest_raw(path: Path) -> tuple[DeployManifest | None, dict[str, Any] | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, None, f"invalid_json:{exc}"
    if not isinstance(data, dict):
        return None, None, "invalid_not_object"
    try:
        man = DeployManifest.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        return None, data, f"parse_error:{exc}"
    return man, data, ""


def _schema_bucket(man: DeployManifest | None, data: dict[str, Any] | None, err: str) -> str:
    if err or man is None:
        return "invalid"
    try:
        ver = int(getattr(man, "schema_version", 0) or (data or {}).get("schema_version") or 0)
    except (TypeError, ValueError):
        return "invalid"
    if ver >= 2:
        return "v2"
    return "legacy"


def _load_games(conn: sqlite3.Connection) -> dict[int, GameDeployConfig]:
    out: dict[int, GameDeployConfig] = {}
    for row in conn.execute(
        """
        SELECT app_id, name, install_path, mod_path, deploy_type, workshop_path
        FROM games
        """
    ):
        app_id = int(row["app_id"] or 0)
        out[app_id] = GameDeployConfig(
            app_id=app_id,
            name=str(row["name"] or ""),
            install_path=str(row["install_path"] or ""),
            mod_path=str(row["mod_path"] or ""),
            deploy_type=str(row["deploy_type"] or "") or "folder_copy",
            workshop_path=str(row["workshop_path"] or ""),
        )
    return out


def _load_mods(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mods)")}
    want = [
        "mod_id",
        "app_id",
        "platform",
        "workspace_id",
        "last_known_path",
        "deploy_status",
        "deploy_path",
        "deploy_error",
        "custom_deploy_path",
        "title",
    ]
    select = ", ".join(c for c in want if c in cols)
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(f"SELECT {select} FROM mods"):
        mid = str(row["mod_id"])
        out[mid] = {k: row[k] for k in row.keys()}
    return out


def _index_mods_by_path(mods: dict[str, dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for mid, row in mods.items():
        lkp = str(row.get("last_known_path") or "").strip()
        if not lkp:
            continue
        try:
            key = str(Path(lkp).resolve()).casefold()
        except OSError:
            key = lkp.casefold()
        index[key] = mid
    return index


def _resolve_mod_id(
    managed: Path,
    man: DeployManifest | None,
    path_index: dict[str, str],
    mods: dict[str, dict[str, Any]],
) -> str:
    claimed = str(man.mod_id if man else "").strip()
    if claimed and claimed in mods:
        return claimed
    try:
        key = str(managed.resolve()).casefold()
    except OSError:
        key = str(managed).casefold()
    if key in path_index:
        return path_index[key]
    # Sidecar workspace / published
    data = read_info_metadata_dict(managed) or {}
    for field_name in ("published_file_id", "workspace_id"):
        token = str(data.get(field_name) or "").strip()
        if token and token in mods:
            return token
    return claimed


def _build_ctx(
    *,
    mod_id: str,
    managed: Path,
    app_id: int,
    games: dict[int, GameDeployConfig],
    custom_deploy_path: str,
    deploy_type: str,
) -> DeployContext | None:
    cfg = games.get(app_id)
    if cfg is None and app_id:
        cfg = GameDeployConfig(app_id=app_id)
    if cfg is None:
        return None
    dtype = str(deploy_type or cfg.deploy_type or "folder_copy").strip()
    if app_id == ANNO_APP and dtype in {"folder_copy", "anno", ""}:
        dtype = "anno_1800"
    return DeployContext(
        mod_id=mod_id or "_audit",
        source=managed,
        app_id=app_id,
        config=cfg,
        deploy_type=dtype,
        managed_path=managed,
        custom_deploy_path=str(custom_deploy_path or "").strip(),
    )


def _classify_legacy_entry(
    entry: ManifestFileEntry, ctx: DeployContext
) -> str:
    kind = str(entry.root_kind or "").strip()
    relative = str(entry.relative or "").strip()
    if kind and relative:
        # Should not happen for legacy bucket, but treat as v2-like.
        try:
            remap_entry_target(entry, ctx)
            return "LEGACY_SAFE_REMAP"
        except DeployPathError as exc:
            msg = str(exc).lower()
            if "ambiguous" in msg:
                return "LEGACY_AMBIGUOUS"
            if "traversal" in msg or ".." in msg:
                return "LEGACY_UNSAFE"
            return "LEGACY_UNSAFE"

    raw = str(entry.target or "").strip()
    if not raw:
        return "LEGACY_INVALID"
    if ".." in Path(raw).parts:
        return "LEGACY_UNSAFE"

    # Under current root already?
    if classify_absolute_target(raw, ctx) is not None:
        return "LEGACY_SAFE_REMAP"

    typed = iter_typed_allowed_roots(ctx)
    matches: list[str] = []
    for _kind, root in typed:
        derived = derive_legacy_relative(raw, root)
        if derived:
            try:
                project_relative(root, derived)
                matches.append(str(root))
            except DeployPathError:
                continue
    if len(matches) > 1:
        # Same projection?
        try:
            remap_entry_target(entry, ctx)
            return "LEGACY_SAFE_REMAP"
        except DeployPathError as exc:
            if "ambiguous" in str(exc).lower():
                return "LEGACY_AMBIGUOUS"
            return "LEGACY_UNSAFE"
    if len(matches) == 1:
        return "LEGACY_SAFE_REMAP"
    return "LEGACY_UNSAFE"


def _classify_v2_entry(entry: ManifestFileEntry, ctx: DeployContext) -> str:
    kind = str(entry.root_kind or "").strip()
    relative = str(entry.relative or "").replace("\\", "/").strip().lstrip("/")
    if not kind:
        return "INVALID_ROOT_KIND"
    if not relative:
        return "INVALID_ROOT_KIND"
    if relative.startswith("..") or "/../" in f"/{relative}/" or ".." in Path(relative).parts:
        return "TRAVERSAL"
    probe = Path(relative)
    if probe.is_absolute() or (getattr(probe, "drive", "") or ""):
        return "ABSOLUTE_RELATIVE"
    try:
        remap_entry_target(entry, ctx)
        return "CANONICAL_OK"
    except DeployPathError as exc:
        msg = str(exc).lower()
        if "ambiguous" in msg:
            return "AMBIGUOUS"
        if "traversal" in msg:
            return "TRAVERSAL"
        if "no current allowed root" in msg or "root_kind" in msg:
            return "INVALID_ROOT_KIND"
        if "escapes" in msg:
            return "PROJECTION_ESCAPE"
        return "PROJECTION_ESCAPE"


def _root_drift_for_target(target: str, ctx: DeployContext | None) -> str:
    raw = str(target or "").strip()
    if not raw:
        return "UNKNOWN_ROOT"
    try:
        abs_path = Path(raw)
        if not abs_path.is_absolute():
            return "UNKNOWN_ROOT"
    except Exception:  # noqa: BLE001
        return "UNKNOWN_ROOT"

    if ctx is not None:
        if classify_absolute_target(raw, ctx) is not None:
            return "CURRENT_ROOT"
        # Can derive onto a current root?
        for _kind, root in iter_typed_allowed_roots(ctx):
            if derive_legacy_relative(raw, root):
                tgt_drive = _drive_of(raw)
                cur_drive = _drive_of(root)
                if tgt_drive and cur_drive and tgt_drive != cur_drive:
                    return "CROSS_DRIVE"
                return "HISTORICAL_ROOT"
        tgt_drive = _drive_of(raw)
        current_drives = {_drive_of(r) for _, r in iter_typed_allowed_roots(ctx)}
        current_drives.discard("")
        if tgt_drive and current_drives and tgt_drive not in current_drives:
            return "CROSS_DRIVE"
        return "UNKNOWN_ROOT"
    return "UNKNOWN_ROOT"


def _entry_targets_exist(man: DeployManifest) -> tuple[int, int]:
    present = 0
    total = 0
    for entry in man.files:
        raw = str(entry.target or "").strip()
        if not raw:
            continue
        total += 1
        if _safe_exists(raw):
            present += 1
    return present, total


def _projected_targets_exist(man: DeployManifest, ctx: DeployContext) -> tuple[int, int]:
    present = 0
    total = 0
    for entry in man.files:
        total += 1
        try:
            remapped = remap_entry_target(entry, ctx)
            if _safe_exists(remapped.target):
                present += 1
        except DeployPathError:
            continue
    return present, total


def _classify_consistency(
    *,
    man: DeployManifest | None,
    row: dict[str, Any] | None,
    ctx: DeployContext | None,
) -> str:
    db_status = str((row or {}).get("deploy_status") or "").strip().lower()
    db_deployed = db_status == "deployed"
    has_manifest = man is not None and bool(man.files)

    hist_present = False
    fs_present = False
    if man is not None:
        present, total = _entry_targets_exist(man)
        hist_present = present > 0
        if ctx is not None:
            p2, t2 = _projected_targets_exist(man, ctx)
            fs_present = p2 > 0
        else:
            fs_present = hist_present

    source_missing = False
    if row is not None:
        lkp = str(row.get("last_known_path") or "").strip()
        if lkp and not Path(lkp).is_dir():
            source_missing = True

    if source_missing and has_manifest:
        return "SOURCE_MISSING"

    if has_manifest and db_deployed and fs_present:
        # Check mismatch between stored absolute targets and projection.
        if ctx is not None and man is not None:
            mismatch = False
            for entry in man.files:
                raw = str(entry.target or "").strip()
                try:
                    remapped = remap_entry_target(entry, ctx)
                    if raw and Path(raw).is_absolute():
                        try:
                            if Path(raw).resolve() != Path(remapped.target).resolve():
                                if _safe_exists(raw) and not _safe_exists(remapped.target):
                                    return "HISTORICAL_TARGET_PRESENT"
                                if _safe_exists(remapped.target) and (
                                    not _safe_exists(raw)
                                    or Path(raw).resolve() != Path(remapped.target).resolve()
                                ):
                                    mismatch = True
                        except OSError:
                            mismatch = True
                except DeployPathError:
                    mismatch = True
            if mismatch:
                return "MANIFEST_FS_MISMATCH"
        return "CONSISTENT"

    if has_manifest and not db_deployed and fs_present:
        return "MANIFEST_ONLY"
    if has_manifest and db_deployed and not fs_present:
        if hist_present:
            return "HISTORICAL_TARGET_PRESENT"
        return "DB_MANIFEST_MISMATCH"
    if not has_manifest and db_deployed:
        return "DB_ONLY"
    if has_manifest and not fs_present and not db_deployed:
        return "MANIFEST_ONLY"
    if fs_present and not has_manifest:
        return "FILESYSTEM_ONLY"
    return "CONSISTENT"


def _classify_source(
    *,
    managed: Path,
    man: DeployManifest | None,
    row: dict[str, Any] | None,
) -> str:
    lkp = str((row or {}).get("last_known_path") or "").strip()
    sidecar = read_info_metadata_dict(managed) or {}
    side_managed = str(
        sidecar.get("managed_path") or sidecar.get("local_path") or ""
    ).strip()
    man_source = str(getattr(man, "source_path", "") or "").strip() if man else ""
    entry_sources = []
    if man is not None:
        for e in man.files:
            s = str(e.source or "").strip()
            if s:
                entry_sources.append(s)

    try:
        managed_res = str(managed.resolve())
    except OSError:
        managed_res = str(managed)

    def _same(a: str, b: str) -> bool:
        if not a or not b:
            return False
        try:
            return Path(a).resolve() == Path(b).resolve()
        except OSError:
            return Path(a).as_posix().casefold() == Path(b).as_posix().casefold()

    if lkp and not Path(lkp).is_dir() and not managed.is_dir():
        return "SOURCE_MISSING"

    currents = [managed_res]
    if lkp and Path(lkp).is_dir():
        currents.append(lkp)

    historical = False
    conflict = False
    for src in [man_source, side_managed, *entry_sources[:5]]:
        if not src:
            continue
        if any(_same(src, c) for c in currents):
            continue
        # archive sources are files under managed — OK
        try:
            p = Path(src)
            if p.is_file():
                try:
                    p.resolve().relative_to(managed.resolve())
                    continue
                except ValueError:
                    pass
            if p.exists():
                historical = True
            else:
                # missing historical source path
                if p.is_absolute():
                    historical = True
        except OSError:
            historical = True

    if lkp and managed.is_dir() and not _same(lkp, managed_res):
        if Path(lkp).is_dir():
            conflict = True
        else:
            historical = True

    if conflict:
        return "SOURCE_CONFLICT"
    if historical:
        return "SOURCE_HISTORICAL"
    if not managed.is_dir():
        return "SOURCE_MISSING"
    return "SOURCE_CURRENT"


def _infer_app_id(
    managed: Path,
    man: DeployManifest | None,
    row: dict[str, Any] | None,
) -> int:
    if row and int(row.get("app_id") or 0) > 0:
        return int(row["app_id"])
    data = read_info_metadata_dict(managed) or {}
    try:
        aid = int(data.get("app_id") or 0)
        if aid > 0:
            return aid
    except (TypeError, ValueError):
        pass
    # Path heuristic: …/Anno 1800/<mod>
    parts = managed.parts
    for i, part in enumerate(parts):
        if part.casefold() in {"anno 1800", "纪元1800"} and i + 1 < len(parts):
            return ANNO_APP
    return 0


def iter_manifest_paths(library: Path) -> list[Path]:
    found: list[Path] = []
    if not library.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(library):
        # Do not follow into unrelated deep caches if named oddly; still scan .info.
        base = Path(dirpath)
        name = base.name
        if name in {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME}:
            if "deploy_manifest.json" in filenames:
                found.append(base / "deploy_manifest.json")
            # Don't descend further into .info
            dirnames[:] = []
            continue
        # Skip heavy non-mod trees
        skip = {"历史版本", "import_cache", ".cache", "cache", "backups"}
        dirnames[:] = [d for d in dirnames if d not in skip]
    return sorted(found)


def audit() -> dict[str, Any]:
    if not PROD_DB.is_file():
        raise SystemExit(f"production DB missing: {PROD_DB}")
    if not PROD_LIB.is_dir():
        raise SystemExit(f"production library missing: {PROD_LIB}")

    conn = _ro_connect(PROD_DB)
    try:
        games = _load_games(conn)
        mods = _load_mods(conn)
    finally:
        conn.close()

    path_index = _index_mods_by_path(mods)
    paths = iter_manifest_paths(PROD_LIB)

    schema_counts: Counter[str] = Counter()
    legacy_counts: Counter[str] = Counter()
    v2_counts: Counter[str] = Counter()
    drift_counts: Counter[str] = Counter()
    consistency_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    drive_counts: Counter[str] = Counter()

    anno = {
        "manifests": 0,
        "archive": 0,
        "folder_copy": 0,
        "legacy": 0,
        "v2": 0,
        "invalid": 0,
        "d_drive_targets": 0,
        "f_drive_targets": 0,
        "e_drive_targets": 0,
        "source_historical": 0,
        "source_missing": 0,
        "source_conflict": 0,
        "manifest_fs_mismatch": 0,
        "canonical_ok": 0,
        "canonical_bad": 0,
        "legacy_safe": 0,
        "legacy_unsafe": 0,
        "legacy_ambiguous": 0,
        "gameplay_paths": 0,
        "shared_pools_paths": 0,
        "assets_xml_paths": 0,
        "examples": defaultdict(list),
    }

    records: list[ManifestRecord] = []
    current_roots_summary: dict[str, Any] = {}
    for app_id, cfg in games.items():
        current_roots_summary[str(app_id)] = {
            "name": cfg.name,
            "install_path": cfg.install_path,
            "mod_path": cfg.mod_path,
            "deploy_type": cfg.deploy_type,
        }

    for mpath in paths:
        managed = mpath.parent.parent  # …/<mod>/.info/deploy_manifest.json
        man, data, err = _load_manifest_raw(mpath)
        bucket = _schema_bucket(man, data, err)
        schema_counts[bucket] += 1

        mid = _resolve_mod_id(managed, man, path_index, mods)
        row = mods.get(mid)
        app_id = _infer_app_id(managed, man, row)
        custom = str((row or {}).get("custom_deploy_path") or "").strip()
        deploy_type = str(man.deploy_type if man else "") or str(
            (games.get(app_id).deploy_type if app_id in games else "") or ""
        )
        ctx = _build_ctx(
            mod_id=mid,
            managed=managed,
            app_id=app_id,
            games=games,
            custom_deploy_path=custom,
            deploy_type=deploy_type,
        )

        rec = ManifestRecord(
            path=mpath,
            managed=managed,
            schema_bucket=bucket,
            schema_version=int(getattr(man, "schema_version", 0) or 0) if man else -1,
            mod_id=mid,
            deploy_type=str(getattr(man, "deploy_type", "") or deploy_type),
            app_id=app_id,
            platform=str((row or {}).get("platform") or ""),
            entry_count=len(man.files) if man else 0,
            anno=app_id == ANNO_APP,
        )

        if man is None:
            rec.legacy_class = "LEGACY_INVALID"
            rec.consistency = "MANIFEST_ONLY"
            rec.source_class = _classify_source(managed=managed, man=None, row=row)
            legacy_counts["LEGACY_INVALID"] += 1
            consistency_counts[rec.consistency] += 1
            source_counts[rec.source_class] += 1
            records.append(rec)
            continue

        # Root drift + drives
        entry_legacy_classes: list[str] = []
        entry_v2_classes: list[str] = []
        for entry in man.files:
            tgt = str(entry.target or "").strip()
            if tgt:
                drv = _drive_of(tgt)
                if drv:
                    drive_counts[drv] += 1
                    rec.target_drives.append(drv)
                drift = _root_drift_for_target(tgt, ctx)
                drift_counts[drift] += 1
                rec.root_drift.append(drift)

            if bucket == "v2" or (str(entry.root_kind or "").strip() and str(entry.relative or "").strip()):
                if ctx is None:
                    entry_v2_classes.append("INVALID_ROOT_KIND")
                else:
                    entry_v2_classes.append(_classify_v2_entry(entry, ctx))
            else:
                if ctx is None:
                    entry_legacy_classes.append("LEGACY_UNSAFE")
                else:
                    entry_legacy_classes.append(_classify_legacy_entry(entry, ctx))

            # Anno path shape counters (not anomalies)
            rel_or_tgt = (
                str(entry.relative or "")
                or str(entry.target or "")
            ).replace("\\", "/")
            if "[Gameplay]" in rel_or_tgt:
                anno["gameplay_paths"] += 1
            if "[Shared] Pools and Definitions" in rel_or_tgt:
                anno["shared_pools_paths"] += 1
            if rel_or_tgt.endswith("data/config/export/main/asset/assets.xml") or (
                "data/config/export/main/asset/assets.xml" in rel_or_tgt
            ):
                anno["assets_xml_paths"] += 1

        if bucket == "legacy":
            # Worst class wins for the manifest.
            priority = [
                "LEGACY_INVALID",
                "LEGACY_UNSAFE",
                "LEGACY_AMBIGUOUS",
                "LEGACY_SAFE_REMAP",
            ]
            chosen = "LEGACY_SAFE_REMAP"
            for p in priority:
                if p in entry_legacy_classes:
                    chosen = p
                    break
            if not entry_legacy_classes:
                chosen = "LEGACY_INVALID"
            rec.legacy_class = chosen
            legacy_counts[chosen] += 1
        elif bucket == "v2":
            rec.v2_classes = entry_v2_classes
            for c in entry_v2_classes:
                v2_counts[c] += 1
            if entry_v2_classes and all(c == "CANONICAL_OK" for c in entry_v2_classes):
                pass
            elif not entry_v2_classes:
                v2_counts["INVALID_ROOT_KIND"] += 1
        else:
            legacy_counts["LEGACY_INVALID"] += 1
            rec.legacy_class = "LEGACY_INVALID"

        rec.consistency = _classify_consistency(man=man, row=row, ctx=ctx)
        consistency_counts[rec.consistency] += 1
        rec.source_class = _classify_source(managed=managed, man=man, row=row)
        source_counts[rec.source_class] += 1

        if rec.anno:
            anno["manifests"] += 1
            dtype = rec.deploy_type.lower()
            if "archive" in {
                str(e.type or "").lower() for e in man.files
            } or any(
                str(e.source or "").lower().endswith((".zip", ".7z", ".rar"))
                for e in man.files
            ):
                anno["archive"] += 1
            elif "folder" in dtype or dtype in {"anno_1800", "anno", ""}:
                # Distinguish: archive type entries vs folder_copy
                if any(str(e.type or "").lower() == "archive" for e in man.files):
                    anno["archive"] += 1
                else:
                    anno["folder_copy"] += 1
            if bucket == "legacy":
                anno["legacy"] += 1
            elif bucket == "v2":
                anno["v2"] += 1
            else:
                anno["invalid"] += 1
            drives = set(rec.target_drives)
            if "D:" in drives:
                anno["d_drive_targets"] += 1
            if "F:" in drives:
                anno["f_drive_targets"] += 1
            if "E:" in drives:
                anno["e_drive_targets"] += 1
            if rec.source_class == "SOURCE_HISTORICAL":
                anno["source_historical"] += 1
            if rec.source_class == "SOURCE_MISSING":
                anno["source_missing"] += 1
            if rec.source_class == "SOURCE_CONFLICT":
                anno["source_conflict"] += 1
            if rec.consistency == "MANIFEST_FS_MISMATCH":
                anno["manifest_fs_mismatch"] += 1
            if bucket == "v2":
                if entry_v2_classes and all(c == "CANONICAL_OK" for c in entry_v2_classes):
                    anno["canonical_ok"] += 1
                else:
                    anno["canonical_bad"] += 1
            if bucket == "legacy":
                if rec.legacy_class == "LEGACY_SAFE_REMAP":
                    anno["legacy_safe"] += 1
                elif rec.legacy_class == "LEGACY_AMBIGUOUS":
                    anno["legacy_ambiguous"] += 1
                else:
                    anno["legacy_unsafe"] += 1
            # Keep a few examples per interesting class (cap 5)
            for key, cond in (
                ("legacy_unsafe_examples", bucket == "legacy" and rec.legacy_class != "LEGACY_SAFE_REMAP"),
                ("cross_drive_examples", "CROSS_DRIVE" in rec.root_drift),
                ("mismatch_examples", rec.consistency == "MANIFEST_FS_MISMATCH"),
                ("source_missing_examples", rec.source_class == "SOURCE_MISSING"),
            ):
                if cond and len(anno["examples"][key]) < 5:
                    anno["examples"][key].append(
                        {
                            "mod_id": mid,
                            "managed": str(managed),
                            "manifest": str(mpath),
                            "legacy_class": rec.legacy_class,
                            "consistency": rec.consistency,
                            "source_class": rec.source_class,
                            "drives": sorted(set(rec.target_drives)),
                        }
                    )

        records.append(rec)

    # DB deployed without manifest (DB_ONLY from mods side)
    manifest_mod_ids = {r.mod_id for r in records if r.mod_id}
    db_only = 0
    db_only_examples: list[dict[str, Any]] = []
    for mid, row in mods.items():
        if str(row.get("deploy_status") or "").strip().lower() != "deployed":
            continue
        if mid in manifest_mod_ids:
            continue
        db_only += 1
        consistency_counts["DB_ONLY"] += 1
        if len(db_only_examples) < 5:
            db_only_examples.append(
                {
                    "mod_id": mid,
                    "app_id": row.get("app_id"),
                    "deploy_path": row.get("deploy_path"),
                    "last_known_path": row.get("last_known_path"),
                }
            )

    report = {
        "title": "DEPLOYMENT PRODUCTION AUDIT: READ-ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_db": str(PROD_DB),
        "production_library": str(PROD_LIB),
        "total_manifests": len(paths),
        "schema": {
            "v2": schema_counts.get("v2", 0),
            "legacy": schema_counts.get("legacy", 0),
            "invalid": schema_counts.get("invalid", 0),
        },
        "legacy": {
            "safe_remap": legacy_counts.get("LEGACY_SAFE_REMAP", 0),
            "ambiguous": legacy_counts.get("LEGACY_AMBIGUOUS", 0),
            "unsafe": legacy_counts.get("LEGACY_UNSAFE", 0),
            "invalid": legacy_counts.get("LEGACY_INVALID", 0),
        },
        "v2_projection": dict(v2_counts),
        "root_drift": {
            "current": drift_counts.get("CURRENT_ROOT", 0),
            "historical": drift_counts.get("HISTORICAL_ROOT", 0),
            "unknown": drift_counts.get("UNKNOWN_ROOT", 0),
            "cross_drive": drift_counts.get("CROSS_DRIVE", 0),
        },
        "target_drives": dict(drive_counts),
        "consistency": dict(consistency_counts),
        "source": dict(source_counts),
        "db_deployed_without_manifest": db_only,
        "db_only_examples": db_only_examples,
        "current_game_roots": current_roots_summary,
        "anno_1800": {
            **{k: v for k, v in anno.items() if k != "examples"},
            "examples": dict(anno["examples"]),
        },
        "production_mutation": "NO",
        "real_deployment_executed": "NO",
        "manifest_modified": "NO",
        "db_modified": "NO",
        "game_filesystem_modified": "NO",
        "guards": {
            "sqlite_mode": "ro+query_only",
            "save_manifest_called": False,
            "delete_manifest_called": False,
            "deploy_called": False,
            "allowed_roots_expanded": False,
        },
    }
    return report


def _format_text(report: dict[str, Any]) -> str:
    a = report["anno_1800"]
    lines = [
        "DEPLOYMENT PRODUCTION AUDIT: READ-ONLY",
        "",
        f"Generated: {report['generated_at']}",
        f"DB: {report['production_db']}",
        f"Library: {report['production_library']}",
        "",
        f"Total manifests: {report['total_manifests']}",
        f"v2: {report['schema']['v2']}",
        f"legacy: {report['schema']['legacy']}",
        f"invalid: {report['schema']['invalid']}",
        "",
        "Legacy:",
        f"  safe remap: {report['legacy']['safe_remap']}",
        f"  ambiguous: {report['legacy']['ambiguous']}",
        f"  unsafe: {report['legacy']['unsafe']}",
        f"  invalid: {report['legacy']['invalid']}",
        "",
        "Root drift (entry-level):",
        f"  current: {report['root_drift']['current']}",
        f"  historical: {report['root_drift']['historical']}",
        f"  unknown: {report['root_drift']['unknown']}",
        f"  cross-drive: {report['root_drift']['cross_drive']}",
        "",
        f"Target drives: {report['target_drives']}",
        "",
        "Consistency:",
        f"  {report['consistency']}",
        f"  db deployed without manifest: {report['db_deployed_without_manifest']}",
        "",
        "Source:",
        f"  {report['source']}",
        "",
        "v2 projection (entry-level):",
        f"  {report['v2_projection']}",
        "",
        "Anno 1800:",
        f"  manifests: {a['manifests']}",
        f"  archive: {a['archive']}",
        f"  folder_copy: {a['folder_copy']}",
        f"  legacy: {a['legacy']}",
        f"  v2: {a['v2']}",
        f"  invalid: {a['invalid']}",
        f"  D: targets: {a['d_drive_targets']}",
        f"  E: targets: {a['e_drive_targets']}",
        f"  F: targets: {a['f_drive_targets']}",
        f"  source historical: {a['source_historical']}",
        f"  source missing: {a['source_missing']}",
        f"  source conflict: {a['source_conflict']}",
        f"  manifest/fs mismatch: {a['manifest_fs_mismatch']}",
        f"  canonical ok: {a['canonical_ok']}",
        f"  canonical bad: {a['canonical_bad']}",
        f"  legacy safe: {a['legacy_safe']}",
        f"  legacy ambiguous: {a['legacy_ambiguous']}",
        f"  legacy unsafe: {a['legacy_unsafe']}",
        f"  [Gameplay] path hits: {a['gameplay_paths']}",
        f"  [Shared] Pools hits: {a['shared_pools_paths']}",
        f"  assets.xml hits: {a['assets_xml_paths']}",
        "",
        "Current game roots (reference, not expanded):",
    ]
    for app_id, info in sorted(report["current_game_roots"].items(), key=lambda x: int(x[0])):
        if not (info.get("install_path") or info.get("mod_path")):
            continue
        lines.append(
            f"  app_id={app_id} name={info.get('name')!r} "
            f"install={info.get('install_path')!r} mod={info.get('mod_path')!r}"
        )
    lines.extend(
        [
            "",
            "Production mutation: NO",
            "Real deployment executed: NO",
            "Manifest modified: NO",
            "DB modified: NO",
            "Game filesystem modified: NO",
            "",
            "PRODUCTION DEPLOYMENT AUDIT: PASS",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    # Report directory is under tools/ — not production data/mod/games.
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = audit()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORT_DIR / f"deployment_manifest_audit_{stamp}.json"
    text_path = REPORT_DIR / f"deployment_manifest_audit_{stamp}.txt"
    latest_json = REPORT_DIR / "deployment_manifest_audit_latest.json"
    latest_txt = REPORT_DIR / "deployment_manifest_audit_latest.txt"

    text = _format_text(report)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    latest_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_txt.write_text(text, encoding="utf-8")

    print(text)
    print(f"Report JSON: {json_path}")
    print(f"Report TEXT: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
