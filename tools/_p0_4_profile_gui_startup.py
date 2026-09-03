"""P0-4 GUI startup I/O overlap profiling. Instrumentation consumer only.

Copies production DB (SMM_TEST_DB). Suppresses sidecar writes so production
``.info`` is not mutated. Does not change CoverLoader / LibraryLoad / QTimer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "mod_manager.db"
REPORT_DIR = ROOT / "data" / "p0_4_profiling"

MAX_WAIT_MS = 480_000
QUIET_MS = 4_000
MIN_WAIT_MS = 3_000


def _stat(path: Path) -> dict:
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def _configure_log(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root_log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    try:
        sh.stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sh.setFormatter(fmt)
    root_log.addHandler(sh)


def _parse_kv(line: str) -> dict:
    out: dict = {}
    for part in line.split():
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key] = val
    return out


def _summaries_from_log(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if "[RECONCILE_SUMMARY]" not in line:
            continue
        kv = _parse_kv(line)
        if kv.get("mods_seen", "").isdigit() and int(kv["mods_seen"]) > 10:
            rows.append(kv)
    return rows


def _patch_sidecar() -> tuple:
    import services.file_ops as file_ops
    import services.library_reconcile as librec

    orig = file_ops.persist_unified_metadata_dict

    def _no_sidecar(*_a, **_k):
        return None

    file_ops.persist_unified_metadata_dict = _no_sidecar  # type: ignore[assignment]
    librec.persist_unified_metadata_dict = _no_sidecar  # type: ignore[attr-defined]
    return orig, file_ops, librec


def run_one_gui() -> dict:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from core.db_manager import get_db
    from main import _probe_modio_playwright_runtime
    from services.startup_io_trace import (
        active_names,
        has_ended,
        reset_startup_io_trace,
        summarize,
    )
    from ui.main_window import MainWindow
    from ui.startup_lifecycle import mark_qapplication_created, reset_startup_timeline
    from ui.styles import APP_STYLE, apply_dark_palette
    from ui.window_chrome import TITLE_BAR_STYLE, apply_application_icon

    reset_startup_io_trace()
    reset_startup_timeline()
    get_db()

    app = QApplication(sys.argv)
    mark_qapplication_created()
    app.setOrganizationName("SteamModManager")
    app.setApplicationName("WorkshopLibrary")
    app.setStyle("Fusion")
    apply_dark_palette(app)
    apply_application_icon(app)
    app.setStyleSheet(APP_STYLE + "\n" + TITLE_BAR_STYLE)

    t_gui0 = time.perf_counter()
    window = MainWindow()
    window.show()
    app.processEvents()
    QTimer.singleShot(800, _probe_modio_playwright_runtime)

    state = {"quiet_at": None, "done": False}

    def _tick() -> None:
        if state["done"]:
            return
        elapsed_ms = (time.perf_counter() - t_gui0) * 1000.0
        active = active_names()
        rec_done = has_ended("reconcile")
        quiet = not active
        if elapsed_ms < MIN_WAIT_MS:
            return
        if not rec_done and elapsed_ms < MAX_WAIT_MS:
            return
        if quiet:
            if state["quiet_at"] is None:
                state["quiet_at"] = time.perf_counter()
            elif (time.perf_counter() - state["quiet_at"]) * 1000.0 >= QUIET_MS:
                state["done"] = True
                window.close()
                app.quit()
        else:
            state["quiet_at"] = None
        if elapsed_ms >= MAX_WAIT_MS:
            state["done"] = True
            window.close()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(_tick)
    timer.start(400)
    app.exec()
    gui_ms = (time.perf_counter() - t_gui0) * 1000.0
    summary = summarize()
    summary["gui_startup_total_ms"] = round(gui_ms, 1)
    summary["timed_out"] = gui_ms >= MAX_WAIT_MS - 50
    return summary


def _run_once() -> int:
    log_path = Path(os.environ["P0_4_GUI_LOG"])
    out_path = Path(os.environ["P0_4_GUI_OUT"])
    _configure_log(log_path)
    orig, file_ops, librec = _patch_sidecar()
    try:
        summary = run_one_gui()
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    finally:
        file_ops.persist_unified_metadata_dict = orig
        librec.persist_unified_metadata_dict = orig
        from core.db_manager import DatabaseManager

        DatabaseManager.reset_instance()
    return 0


def main() -> int:
    if "--once" in sys.argv:
        return _run_once()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = REPORT_DIR / f"gui_startup_{stamp}.log"
    report_path = REPORT_DIR / f"gui_startup_{stamp}.json"
    db_before = _stat(PROD_DB)
    work = REPORT_DIR / f"gui_work_{stamp}"
    work.mkdir()
    db_copy = work / "mod_manager.db"
    shutil.copy2(PROD_DB, db_copy)

    python = sys.executable
    runs = []
    for label in ("A", "B"):
        out_path = work / f"run_{label}.json"
        env = os.environ.copy()
        env["SMM_TEST_DB"] = str(db_copy)
        env["P0_4_GUI_LOG"] = str(log_path)
        env["P0_4_GUI_OUT"] = str(out_path)
        proc = subprocess.run(
            [python, str(Path(__file__).resolve()), "--once"],
            cwd=str(ROOT),
            env=env,
        )
        if proc.returncode != 0:
            raise SystemExit(f"GUI run {label} failed rc={proc.returncode}")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        payload["run"] = label
        runs.append(payload)

    db_after = _stat(PROD_DB)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "log_path": str(log_path),
        "production_db_untouched": db_before == db_after,
        "db_before": db_before,
        "db_after": db_after,
        "runs": runs,
        "reconcile_summaries": _summaries_from_log(log_text),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if db_before == db_after else 2


if __name__ == "__main__":
    raise SystemExit(main())
