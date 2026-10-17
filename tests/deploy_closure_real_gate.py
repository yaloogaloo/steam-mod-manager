"""Real-disk Deploy Closure Gate for 3308841144 / 2511735990.

Run:

    python tests/deploy_closure_real_gate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db_manager import DatabaseManager
from core.paths import database_path, default_mod_library
from services.deploy import ModDeployer
from services.deploy_rules.generic import deploy_wrapper_folder
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME
from services.importers.archive import is_archive_path
from services.importers.local_scanner import is_skipped_mod_path_part


WS_A = "3308841144"
WS_B = "2511735990"
EXPECTED_A_NAME = "The Abigail Williams Class"
EXPECTED_B_NAME = "mod_2511735990"
BASELINE_TOTAL_S = 33.0


def _rel_files(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        if any(is_skipped_mod_path_part(part) for part in parts):
            continue
        try:
            out[rel] = int(path.stat().st_size)
        except OSError:
            continue
    return out


def _outer_expected(source: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(source)
        except ValueError:
            continue
        if any(is_skipped_mod_path_part(part) for part in rel.parts):
            continue
        if is_archive_path(path.name):
            continue
        out[rel.as_posix()] = int(path.stat().st_size)
    return out


def _archives_in_source(source: Path) -> list[str]:
    names: list[str] = []
    for path in source.rglob("*"):
        if path.is_file() and is_archive_path(path.name):
            try:
                names.append(path.relative_to(source).as_posix())
            except ValueError:
                names.append(path.name)
    return names


def _find_by_workspace(db: DatabaseManager, workspace_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT mod_id, internal_id, workspace_id, title, last_known_path, "
            "deploy_status, deploy_path FROM mods WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"workspace_id {workspace_id} not in DB")
    keys = [
        "mod_id",
        "internal_id",
        "workspace_id",
        "title",
        "last_known_path",
        "deploy_status",
        "deploy_path",
    ]
    if hasattr(row, "keys"):
        return {k: row[k] for k in keys}
    return dict(zip(keys, row))


def _backup_item_count(managed: Path, frozen: str, pk: str) -> int:
    from services.backup_manager import BackupManager

    try:
        root = BackupManager(managed, internal_id=frozen, mod_pk=pk).backups_root()
    except Exception:  # noqa: BLE001
        return 0
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def _staging_remaining() -> list[str]:
    from services.importers.archive import import_cache_root

    root = import_cache_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.name.startswith("deploy_"))


def _timing(managed: Path) -> dict:
    path = managed / INFO_DIR_NAME / "deploy_timing.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _undeploy(deployer: ModDeployer, frozen: str) -> None:
    out = deployer.undeploy_mod(frozen)
    print("undeploy", frozen, out.get("success"), out.get("error") or "")


def _deploy_one(
    *,
    db: DatabaseManager,
    deployer: ModDeployer,
    row: dict,
    expected_name: str,
    source: Path,
) -> dict:
    frozen = str(row["internal_id"])
    pk = str(row["mod_id"])
    ws = str(row["workspace_id"])
    print("identity", {"internal_id": frozen, "mod_pk": pk, "workspace_id": ws})
    assert frozen.count("-") == 4, frozen
    assert pk != ws
    assert expected_name != f"mod_{pk}"

    _undeploy(deployer, frozen)
    result = deployer.deploy_mod(frozen)
    print("deploy success", result.get("success"), result.get("error") or "")
    if not result.get("success"):
        return {"ok": False, "result": result}

    target = Path(str(result.get("target") or "")).resolve()
    planned_name = deploy_wrapper_folder(source.name, ws)
    timing = _timing(source)
    actual = _rel_files(target)
    outer = _outer_expected(source)
    archives = _archives_in_source(source)
    manifest = load_manifest(source)
    planned: dict[str, int] = {}
    if manifest is not None:
        for entry in manifest.files:
            rel = ""
            try:
                rel = Path(entry.target).resolve().relative_to(target).as_posix()
            except (ValueError, OSError):
                rel = str(entry.relative or entry.source_relative or "").replace("\\", "/")
            if not rel:
                continue
            dest = target / rel
            size = int(dest.stat().st_size) if dest.is_file() else -1
            planned[rel] = size

    missing_outer = sorted(p for p in outer if p not in actual or actual[p] != outer[p])
    extra_archives = [p for p in actual if is_archive_path(Path(p).name)]
    info_in_target = INFO_DIR_NAME in {Path(p).parts[0] for p in actual} or any(
        Path(p).name == INFO_DIR_NAME for p in actual
    )
    staging = _staging_remaining()
    backup_n = _backup_item_count(source, frozen, pk)
    manifest_n = len(manifest.files) if manifest is not None else 0
    leftover_wrong = (target.parent / f"mod_{pk}").is_dir()
    if expected_name.startswith("The Abigail"):
        leftover_wrong = leftover_wrong or (target.parent / f"mod_{ws}").is_dir()
    planned_only = sorted(set(planned) - set(actual))[:15]
    actual_only = sorted(set(actual) - set(planned))[:15]
    set_ok = set(planned) == set(actual) if planned else False
    size_mismatch = [
        rel
        for rel in sorted(set(planned) & set(actual))
        if planned[rel] != actual[rel]
    ]
    extracted = sorted(set(actual) - set(outer))
    diag = timing.get("diagnostics") or {}
    named = {
        k: timing.get(k)
        for k in (
            "resolve_ms",
            "verify_source_ms",
            "extract_ms",
            "plan_ms",
            "fileplan_ms",
            "conflict_scan_ms",
            "backup_ms",
            "copy_ms",
            "manifest_ms",
            "verify_ms",
            "hash_ms",
            "persist_ms",
            "cleanup_ms",
            "total_ms",
            "accounted_ms",
            "unaccounted_ms",
        )
        if k in timing
    }
    report = {
        "ok": bool(
            result.get("success")
            and target.name == expected_name
            and planned_name == expected_name
            and not missing_outer
            and not extra_archives
            and set_ok
            and not size_mismatch
            and target.name != f"mod_{pk}"
            and not info_in_target
            and not staging
            and not leftover_wrong
        ),
        "original_folder": source.name,
        "final_folder": target.name,
        "target": str(target),
        "exists": target.is_dir(),
        "outer_file_count": len(outer),
        "extracted_file_count": len(extracted),
        "actual_file_count": len(actual),
        "planned_file_count": len(planned),
        "missing_outer": missing_outer[:20],
        "extra_archives": extra_archives[:20],
        "path_set_equal": set_ok,
        "planned_only": planned_only,
        "actual_only": actual_only,
        "size_mismatch_count": len(size_mismatch),
        "materialized_outer_bytes": diag.get("outer_bytes_to_temp", diag.get("outer_bytes_to_temp")),
        "timing": named,
        "diagnostics": {
            k: diag.get(k)
            for k in (
                "backup_exists_checks",
                "backup_skipped_missing_tree",
                "backup_files",
                "conflict_folders_scanned",
                "conflict_self_skipped",
                "fingerprint_stat_count",
                "fingerprint_reused_copy_sizes",
                "hash_reused_from_copy",
                "hash_from_disk_files",
                "copied_bytes",
                "from_managed_bytes",
                "from_extract_overlay_bytes",
                "extracted_file_count",
                "has_outer_files",
                "outer_bytes_to_temp",
            )
            if k in diag
        },
        "op_profile": timing.get("op_profile") or {},
        "copy_profile": (timing.get("op_profile") or {}).get("copy_profile") or {},
        "archives_in_source": archives[:12],
        "source": str(source),
        "final_bytes": sum(actual.values()),
        "materialized_bytes": diag.get("materialized_bytes"),
        "cleanup_remaining_entries": staging,
        "backup_item_count": backup_n,
        "manifest_item_count": manifest_n,
        "info_in_target": info_in_target,
        "historical_wrong_target": leftover_wrong,
    }
    return report


def main() -> int:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(database_path())
    library = Path(default_mod_library())
    deployer = ModDeployer(library_root=library, db=db)
    row_a = _find_by_workspace(db, WS_A)
    row_b = _find_by_workspace(db, WS_B)
    path_a = Path(str(row_a["last_known_path"] or "")).resolve()
    path_b = Path(str(row_b["last_known_path"] or "")).resolve()
    if not path_a.is_dir():
        raise SystemExit(f"source A missing: {path_a}")
    if not path_b.is_dir():
        raise SystemExit(f"source B missing: {path_b}")

    leftover = Path()
    cfg = db.get_game_deploy_config(262060)
    mods_root = Path(str(cfg.mod_path or "")) if cfg else None
    if mods_root:
        leftover = mods_root / "mod_3308841144"
        if leftover.is_dir():
            print("leftover dest", leftover)

    report_a = _deploy_one(
        db=db,
        deployer=deployer,
        row=row_a,
        expected_name=EXPECTED_A_NAME,
        source=path_a,
    )
    report_b = _deploy_one(
        db=db,
        deployer=deployer,
        row=row_b,
        expected_name=EXPECTED_B_NAME,
        source=path_b,
    )
    out = {
        "A": report_a,
        "B": report_b,
        "leftover_mod_3308841144_exists": leftover.is_dir() if leftover else False,
        "baseline_total_s": BASELINE_TOTAL_S,
    }
    dest = ROOT / "_tmp" / "deploy_closure_gate_report.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = bool(report_a.get("ok") and report_b.get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
