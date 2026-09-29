"""Measure click-to-settle Deploy latency for workspace_id 3308841144.

Deploy identity = Frozen internal_id (resolved from workspace_id).
mod_pk = DB handle only. workspace_id = platform identity.

A = UI deploy action (DeployWorker + post-deploy slots)
B = core ModDeployer.deploy_mod only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WS = "3308841144"
APP_ID = 262060
EXPECTED_NAME = "The Abigail Williams Class"


def _find_row(db, workspace_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT mod_id, internal_id, workspace_id, title, last_known_path, "
            "deploy_status, deploy_path, app_id FROM mods WHERE workspace_id = ?",
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
        "app_id",
    ]
    if hasattr(row, "keys"):
        return {k: row[k] for k in keys}
    return dict(zip(keys, row))


def _probe(source: Path) -> dict:
    files = 0
    dirs = 0
    nbytes = 0
    if source.is_dir():
        for path in source.rglob("*"):
            try:
                if path.is_dir():
                    dirs += 1
                elif path.is_file():
                    files += 1
                    nbytes += int(path.stat().st_size)
            except OSError:
                continue
    return {"files": files, "dirs": dirs, "bytes": nbytes}


def _print_job(label: str, summary: dict) -> None:
    print()
    print(f"===== {label} =====")
    print(f"job_id                 {summary.get('job_id')}")
    print(f"USER_E2E               {summary.get('USER_E2E_s')}s")
    print(f"CORE_DEPLOY            {summary.get('CORE_DEPLOY_s')}s")
    print(f"POST_DEPLOY            {summary.get('POST_DEPLOY_s')}s")
    print(f"UI_SETTLE              {summary.get('UI_SETTLE_s')}s")
    print(f"case                   {summary.get('case')}")
    print(f"one_to_one_ok          {summary.get('one_to_one_ok')}")
    print(f"deploy_action_count    {summary.get('deploy_action_count')}")
    print(f"worker_count           {summary.get('worker_count')}")
    print(f"deploy_mod_count       {summary.get('deploy_mod_count')}")
    print(f"finish_signal_count    {summary.get('finish_signal_count')}")
    print(f"post_refresh_count     {summary.get('post_refresh_count')}")
    print(f"audit_count            {summary.get('audit_count')}")
    print(f"reconcile_count        {summary.get('reconcile_count')}")
    print(f"notify_mod_changed     {summary.get('notify_mod_changed_count')}")
    print(f"show_mod_count         {summary.get('show_mod_count')}")
    print(f"violations             {summary.get('one_to_one_violations')}")
    print(f"pending_at_end         {summary.get('pending_at_end')}")
    print(f"still_running          {summary.get('still_running_background_tasks')}")
    print("--- events ---")
    for ev in summary.get("events") or []:
        print(
            f"{ev.get('elapsed_ms'):8.1f}  d={ev.get('delta_ms'):8.1f}  "
            f"{ev.get('thread'):20}  {ev.get('label')}"
        )
    print("--- ops ---")
    for op in summary.get("ops") or []:
        print(
            f"{op.get('operation'):28}  n={op.get('count'):3}  "
            f"total={op.get('total_ms'):9.1f}  max={op.get('largest_ms'):9.1f}  "
            f"ui={op.get('ui_thread_count')}  files={op.get('files')}  "
            f"thread={op.get('thread')}"
        )


def run_core(frozen: str, library: Path) -> dict:
    from services.deploy import ModDeployer
    from services.deploy_e2e import reset_e2e, start_deploy_job

    reset_e2e()
    job = start_deploy_job(internal_id=frozen, action="deploy", source="core_only")
    job.event("worker created")
    job.event("worker started")
    job.event("worker deploy entered")
    deployer = ModDeployer(library_root=library)
    result = deployer.deploy_mod(frozen)
    job.event("worker deploy returned")
    job.event("worker finished emitted")
    job.count("worker_count")
    job.count("finish_signal_count")
    job.ui_slot_returned = True
    job.worker_thread_finished = True
    job.finish(reason="core_only")
    summary = job.summary()
    summary["deploy_success"] = bool(result.get("success"))
    summary["deploy_error"] = str(result.get("error") or "")
    summary["target"] = str(result.get("target") or "")
    summary["core_timing"] = result.get("deploy_timing") or {}
    return summary


def run_ui(frozen: str, source: Path, game_name: str) -> dict:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from services.deploy_e2e import current_job, reset_e2e, wait_until_ended
    from ui.library_view import ModLibraryView

    reset_e2e()
    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    view = ModLibraryView()
    view._set_current_game_context(game_name, game_id=APP_ID)
    view._selected_mod_id = frozen
    view._selected_mod_ids = [frozen]
    view._selection_anchor_mod_id = frozen
    # Match a user who already has Detail open before clicking Deploy.
    setup_t0 = time.perf_counter()
    view.detail_panel.show_mod(
        source,
        mod_id=frozen,
        game_id=APP_ID,
        game_name=game_name,
    )
    QCoreApplication.processEvents()
    setup_ms = (time.perf_counter() - setup_t0) * 1000.0

    view._on_deploy_action(frozen, "deploy")
    job = current_job()
    if job is None:
        raise SystemExit("UI deploy did not start an E2E job")
    wait_until_ended(timeout_s=600.0, pump_qt=True)
    summary = job.summary()
    summary["setup_show_mod_ms"] = round(setup_ms, 3)
    summary["busy_after"] = bool(getattr(view.detail_panel, "_deploy_busy", False))
    summary["deploy_button"] = str(view.detail_panel.btn_deploy.text())
    summary["deploy_status_label"] = str(view.detail_panel.view_deploy.text())
    view.shutdown_workers()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("both", "core", "ui"), default="both")
    args = parser.parse_args()

    from core.db_manager import DatabaseManager
    from core.mod_platform import PLATFORM_STEAM
    from core.paths import database_path, default_mod_library
    from services.deploy_identity import is_frozen_internal_uuid
    from services.identity_service import resolve_internal_id_from_workspace_id

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(database_path())
    library = Path(default_mod_library())
    row = _find_row(db, WS)
    frozen = resolve_internal_id_from_workspace_id(
        WS, platform=PLATFORM_STEAM, app_id=APP_ID, db=db
    )
    if not is_frozen_internal_uuid(frozen):
        raise SystemExit(f"missing Frozen internal_id for workspace_id={WS}")
    source = Path(str(row["last_known_path"] or "")).resolve()
    if not source.is_dir():
        raise SystemExit(f"source missing: {source}")

    game = db.get_game(APP_ID)
    game_name = str(getattr(game, "folder_name", None) or getattr(game, "name", None) or "暗黑地牢")
    cfg = db.get_game_deploy_config(APP_ID)
    target_root = Path(str(cfg.mod_path or "")) if cfg else None
    probe = _probe(source)
    existing_target = None
    if target_root and str(row.get("deploy_path") or "").strip():
        existing_target = Path(str(row["deploy_path"]))

    header = {
        "workspace_id": WS,
        "title": row.get("title"),
        "internal_id": frozen,
        "mod_pk": row.get("mod_id"),
        "source": str(source),
        "source_probe": probe,
        "deploy_status_before": row.get("deploy_status"),
        "deploy_path_before": row.get("deploy_path"),
        "game_name": game_name,
        "target_root": str(target_root) if target_root else "",
        "expected_name": EXPECTED_NAME,
        "existing_target_exists": bool(existing_target and existing_target.is_dir()),
    }
    print(json.dumps(header, ensure_ascii=False, indent=2))

    out: dict = {"header": header}
    if args.mode in ("both", "ui"):
        ui_summary = run_ui(frozen, source, game_name)
        _print_job("A UI click path", ui_summary)
        out["A_ui"] = ui_summary
    if args.mode in ("both", "core"):
        core_summary = run_core(frozen, library)
        _print_job("B core deploy only", core_summary)
        out["B_core"] = core_summary

    dest = ROOT / "_tmp" / "deploy_e2e" / "measure_report.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print()
    print(f"wrote {dest}")
    if args.mode == "both":
        a = float((out.get("A_ui") or {}).get("USER_E2E_s") or 0.0)
        b = float((out.get("B_core") or {}).get("CORE_DEPLOY_s") or 0.0)
        print(f"A={a:.3f}s  B={b:.3f}s  A-B={a-b:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
