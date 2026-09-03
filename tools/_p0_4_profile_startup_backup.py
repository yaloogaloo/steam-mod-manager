"""P0-4 profiling harness — measure startup reconcile. Does not optimize.

Uses a copied production DB so identity rows are not written. Production
``mod/`` and ``data/mod_backup/`` are read (and backup copies only if hashes
differ). Sidecar ``.info`` writes are suppressed for this run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "mod_manager.db"
PROD_LIB = ROOT / "mod"
REPORT_DIR = ROOT / "data" / "p0_4_profiling"
FORBIDDEN = ROOT / "data" / "p0_1b_id_semantic_repair_backup"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stat(path: Path) -> dict:
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns), "path": str(path)}


def _configure_log(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    try:
        sh.stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logging.getLogger("p0_4_profile")


def _parse_summaries(log_text: str) -> list[str]:
    return [ln.strip() for ln in log_text.splitlines() if "[RECONCILE_SUMMARY]" in ln]


def _parse_kv_line(line: str) -> dict:
    out: dict = {}
    for part in line.split():
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key] = val
    return out


def _case_c_isolate(tmp: Path, log: logging.Logger) -> dict:
    """Unchanged metadata / cover / offline vs actual metadata edit — isolated."""
    from core.db_manager import DatabaseManager
    from core.models import ModMetadata
    from services.file_ops import INFO_DIR_NAME, persist_unified_metadata_dict
    from services.metadata_backup_sync import sync_after_metadata_change
    from services.reconcile_observability import current_bucket

    data_root = tmp / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    import services.metadata_backup as mb

    orig_data_dir = mb.data_dir
    mb.data_dir = lambda: data_root  # type: ignore[method-assign]
    try:
        DatabaseManager.reset_instance()
        db = DatabaseManager.instance(tmp / "case_c.db")
        folder = tmp / "mod" / "GameA" / "CaseC"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        payload = {
            "published_file_id": "910777",
            "title": "Case C",
            "game_name": "GameA",
            "source_type": "github",
            "url": "https://example.com/910777",
            "workspace_id": "ws-910777",
        }
        persist_unified_metadata_dict(folder, payload, sync_backup=False)
        (info / "cover.png").write_bytes(b"\x89PNG" + b"x" * 4096)
        offline = info / "offline"
        offline.mkdir()
        (offline / "index.html").write_text("<html>offline-body</html>", encoding="utf-8")
        db.upsert_mod(
            ModMetadata(
                published_file_id="910777",
                title="Case C",
                game_name="GameA",
                managed_path=str(folder),
            )
        )
        log.info("[CASE] A/B first sync (creates backup)")
        sync_after_metadata_change("910777", folder, "import")
        log.info("[CASE] A/B second sync (unchanged .info + cover + offline)")
        from services.reconcile_observability import start_reconcile_session, finish_reconcile_session

        start_reconcile_session()
        sync_after_metadata_change("910777", folder, "restore")
        s_unchanged = finish_reconcile_session()
        meta = folder / INFO_DIR_NAME / "metadata.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["description"] = "case-c-edited"
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[CASE] C third sync (metadata.json modified)")
        start_reconcile_session()
        sync_after_metadata_change("910777", folder, "edit")
        s_changed = finish_reconcile_session()
        assert current_bucket() is None
        return {
            "unchanged_second_sync": {
                "hash_ms": s_unchanged.hash_ms,
                "hash_files": s_unchanged.hash_files,
                "hash_bytes": s_unchanged.hash_bytes,
                "copy_ms": s_unchanged.copy_ms,
                "copy_files": s_unchanged.copy_files,
                "size_match_then_hash": s_unchanged.size_match_then_hash,
                "mtime_equal_then_hash": s_unchanged.mtime_equal_then_hash,
                "mtime_unequal_then_hash": s_unchanged.mtime_unequal_then_hash,
            },
            "metadata_modified": {
                "hash_ms": s_changed.hash_ms,
                "hash_files": s_changed.hash_files,
                "copy_ms": s_changed.copy_ms,
                "copy_files": s_changed.copy_files,
                "mods_changed": s_changed.mods_changed,
                "size_match_then_hash": s_changed.size_match_then_hash,
            },
        }
    finally:
        mb.data_dir = orig_data_dir  # type: ignore[method-assign]
        DatabaseManager.reset_instance()


def _assert_no_gui() -> None:
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None
    if psutil is None:
        return
    hits = []
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if "main.py" in cmd and str(ROOT) in cmd:
            hits.append(proc.info.get("pid"))
    if hits:
        raise SystemExit(f"Abort: main.py already running pids={hits}")


def main() -> int:
    if FORBIDDEN.exists() and not FORBIDDEN.is_dir():
        pass
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = REPORT_DIR / f"profile_{stamp}.log"
    report_path = REPORT_DIR / f"profile_{stamp}.json"
    log = _configure_log(log_path)
    log.info("[RECONCILE_SESSION] startup_begin")
    _assert_no_gui()
    if not PROD_DB.is_file():
        raise SystemExit(f"missing production DB {PROD_DB}")
    if not PROD_LIB.is_dir():
        raise SystemExit(f"missing library {PROD_LIB}")

    db_before = _stat(PROD_DB)
    work = REPORT_DIR / f"work_{stamp}"
    work.mkdir()
    db_copy = work / "mod_manager.db"
    shutil.copy2(PROD_DB, db_copy)
    log.info("copied production DB -> %s", db_copy)

    case_c = _case_c_isolate(work / "case_c", log)
    log.info("case_c isolate: %s", json.dumps(case_c, ensure_ascii=False))

    os.environ["SMM_TEST_DB"] = str(db_copy)
    from core.db_manager import DatabaseManager

    DatabaseManager.reset_instance()
    DatabaseManager.instance(db_copy)

    import services.file_ops as file_ops

    orig_persist = file_ops.persist_unified_metadata_dict
    suppressed = {"n": 0}

    def _no_sidecar_write(*args, **kwargs):
        suppressed["n"] += 1
        return None

    file_ops.persist_unified_metadata_dict = _no_sidecar_write  # type: ignore[assignment]
    import services.library_reconcile as librec

    librec.persist_unified_metadata_dict = _no_sidecar_write  # type: ignore[attr-defined]

    from services.library_reconcile import reconcile_library

    passes = []
    try:
        for idx in (1, 2):
            log.info("[RECONCILE_SESSION] reconcile_begin pass=%s", idx)
            t0 = time.perf_counter()
            result = reconcile_library(PROD_LIB)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            log.info(
                "[RECONCILE_SESSION] reconcile_end pass=%s elapsed_ms=%.1f scanned=%s synced=%s",
                idx,
                elapsed_ms,
                result.scanned,
                result.synced,
            )
            passes.append(
                {
                    "pass": idx,
                    "elapsed_ms": elapsed_ms,
                    "scanned": result.scanned,
                    "synced": result.synced,
                    "imported": result.imported,
                    "missing": result.missing,
                    "renamed": result.renamed,
                    "conflicts": result.conflicts,
                }
            )
    finally:
        log.info("sidecar persists suppressed=%s", suppressed["n"])
        librec.persist_unified_metadata_dict = orig_persist
        file_ops.persist_unified_metadata_dict = orig_persist
        DatabaseManager.reset_instance()
    db_after = _stat(PROD_DB)
    db_untouched = db_before == db_after
    log.info("production DB untouched=%s before=%s after=%s", db_untouched, db_before, db_after)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    summaries = _parse_summaries(log_text)
    parsed = [_parse_kv_line(s) for s in summaries]
    report = {
        "generated_at": _utc_now(),
        "log_path": str(log_path),
        "production_db_untouched": db_untouched,
        "db_before": db_before,
        "db_after": db_after,
        "case_c_isolate": case_c,
        "sidecar_persists_suppressed": suppressed["n"],
        "passes": passes,
        "summaries": parsed,
        "summary_lines": summaries,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s", report_path)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if db_untouched else 2


if __name__ == "__main__":
    raise SystemExit(main())
