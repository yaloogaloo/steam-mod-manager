#!/usr/bin/env python3
"""
Deploy System smoke runner — real DeployWorker / DeployResult call chain.

Creates an isolated library + game target, exercises:

  SUCCESS  — folder-copy mod deploys to target
  FAILED   — source missing must terminate FAILED
  REDEPLOY — second deploy is idempotent (no nested duplicate dirs)

Does not redesign DeployResult / Manifest / Strategy / Worker lifecycle.

Usage:
  python tools/deploy_smoke_runner.py
  python tools/deploy_smoke_runner.py --workspace E:/tmp/smoke --out report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo root on sys.path when invoked as ``python tools/deploy_smoke_runner.py``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager  # noqa: E402
from core.models import ModMetadata  # noqa: E402
from services.deploy import ModDeployer  # noqa: E402
from services.deploy_fs import safe_iter_files  # noqa: E402
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME  # noqa: E402
from services.deploy_rules.manifest import load_manifest  # noqa: E402

logger = logging.getLogger("deploy_smoke")

SUCCESS_MOD_ID = "3780425019"
FAILED_MOD_ID = "3780425099"
APP_ID = 1623730
GAME_NAME = "Palworld"
STRATEGY = DEPLOY_TYPE_FOLDER_COPY

_STAGE_RE = re.compile(r"\[DEPLOY_STAGE\].*\bstage=(\w+)\b")
_RESULT_RE = re.compile(r"\[DEPLOY_RESULT\].*\bstatus=(\w+)\b")


@dataclass
class LogCapture(logging.Handler):
    """Capture ``[DEPLOY_STAGE]`` / ``[DEPLOY_RESULT]`` lines."""

    lines: list[str] = field(default_factory=list)

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "[DEPLOY_STAGE]" in msg or "[DEPLOY_RESULT]" in msg:
            self.lines.append(msg)


def _ensure_qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _tree_stats(root: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for regular files under *root*."""
    if not root.exists():
        return 0, 0
    count = 0
    nbytes = 0
    for path in safe_iter_files(root):
        count += 1
        try:
            nbytes += int(path.stat().st_size)
        except OSError:
            pass
    return count, nbytes


def _parse_stages(log_lines: list[str]) -> list[str]:
    """Ordered unique stage names (prefer finished events)."""
    finished: list[str] = []
    started: list[str] = []
    for line in log_lines:
        m = _STAGE_RE.search(line)
        if not m:
            continue
        name = m.group(1)
        if "event=finished" in line:
            if name not in finished:
                finished.append(name)
        elif "event=started" in line:
            if name not in started:
                started.append(name)
    return finished or started


def _seed_folder_mod(
    library: Path,
    *,
    mod_id: str,
    title: str,
    payload_bytes: int = 256 * 1024,
) -> Path:
    folder = library / GAME_NAME / title
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(exist_ok=True)
    (folder / "payload.bin").write_bytes(b"S" * payload_bytes)
    nested = folder / "nested"
    nested.mkdir(exist_ok=True)
    (nested / "note.txt").write_text("smoke-ok\n", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mod_id,
                "title": title,
                "app_id": APP_ID,
                "game_name": GAME_NAME,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return folder


def _setup_workspace(workspace: Path) -> tuple[Path, Path, Path, DatabaseManager]:
    library = workspace / "mod"
    game_mods = workspace / "game" / "Mods"
    data = workspace / "data"
    library.mkdir(parents=True, exist_ok=True)
    game_mods.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    db_file = data / "mod_manager.db"
    os_environ_set = __import__("os").environ
    os_environ_set["SMM_TEST_DB"] = str(db_file)

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_file)
    db.update_game_deploy_config(
        APP_ID,
        name=GAME_NAME,
        install_path=str(workspace / "game"),
        mod_path=str(game_mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    return library, game_mods, db_file, db


def _run_worker(
    *,
    mod_id: str,
    library: Path,
    deployer: ModDeployer,
    action: str = "deploy",
    timeout_s: float = 120.0,
) -> tuple[dict[str, Any], list[str]]:
    """Drive real DeployWorker; return (result_dict, captured_log_lines)."""
    from PySide6.QtCore import QCoreApplication

    from ui.deploy_thread import DeployWorker

    app = _ensure_qapp()
    capture = LogCapture()
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    # Single root handler — child loggers propagate; avoid duplicate lines.
    root_logger.addHandler(capture)

    holder: dict[str, Any] = {"result": None, "failed": None}

    worker = DeployWorker(
        mod_id,
        library_root=library,
        action=action,  # type: ignore[arg-type]
        deployer=deployer,
    )
    worker.deploy_finished.connect(lambda payload: holder.__setitem__("result", payload))
    worker.deploy_failed.connect(lambda err: holder.__setitem__("failed", err))

    t0 = time.perf_counter()
    worker.start()
    deadline = t0 + timeout_s
    while worker.isRunning():
        QCoreApplication.processEvents()
        if time.perf_counter() > deadline:
            worker.requestInterruption()
            worker.wait(3000)
            break
        worker.wait(50)
    QCoreApplication.processEvents()
    worker.wait(2000)
    QCoreApplication.processEvents()

    root_logger.removeHandler(capture)
    root_logger.setLevel(prev_level)

    result = holder["result"]
    if result is None:
        result = {
            "success": False,
            "status": "FAILED",
            "mod_id": mod_id,
            "error": "smoke: no terminal DeployWorker result",
            "error_code": "smoke_no_result",
        }
    return result, list(capture.lines)


def _case_report(
    *,
    case: str,
    mod_id: str,
    result: dict[str, Any],
    log_lines: list[str],
    files_before: int,
    files_after: int,
    bytes_before: int,
    bytes_after: int,
    elapsed_ms: float,
    passed: bool,
    assertions: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "case": case,
        "mod_id": mod_id,
        "app_id": APP_ID,
        "game": GAME_NAME,
        "strategy": str(result.get("strategy") or result.get("deploy_type") or STRATEGY),
        "stages": _parse_stages(log_lines),
        "elapsed_ms": round(elapsed_ms, 1),
        "files_before": files_before,
        "files_after": files_after,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "status": str(result.get("status") or ("SUCCESS" if result.get("success") else "FAILED")),
        "error_code": str(
            result.get("error_code")
            or (
                "deploy_failed"
                if str(result.get("status") or "").upper() == "FAILED"
                or (not result.get("success") and result.get("error"))
                else ""
            )
        ),
        "passed": passed,
        "log_timeline": log_lines,
        "assertions": assertions,
        "worker_result": {
            "success": bool(result.get("success")),
            "status": result.get("status"),
            "error": result.get("error"),
            "target": result.get("target"),
            "copied_files": result.get("copied_files"),
        },
    }
    if extra:
        out.update(extra)
    return out


def run_success(library: Path, game_mods: Path, db: DatabaseManager) -> dict[str, Any]:
    source = _seed_folder_mod(library, mod_id=SUCCESS_MOD_ID, title="SmokeMod")
    db.upsert_mod(
        ModMetadata(
            published_file_id=SUCCESS_MOD_ID,
            title="SmokeMod",
            app_id=APP_ID,
            managed_path=str(source),
            game_name=GAME_NAME,
        )
    )
    target = game_mods / "SmokeMod"
    files_before, bytes_before = _tree_stats(target)
    deployer = ModDeployer(library_root=library, db=db)

    t0 = time.perf_counter()
    result, logs = _run_worker(mod_id=SUCCESS_MOD_ID, library=library, deployer=deployer)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    files_after, bytes_after = _tree_stats(target)

    assertions: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        assertions.append(("PASS: " if cond else "FAIL: ") + msg)
        if not cond:
            ok = False

    check(str(result.get("status")) == "SUCCESS", "status == SUCCESS")
    check(bool(result.get("success")), "success == True")
    check(target.is_dir(), f"target exists: {target}")
    check((target / "payload.bin").is_file(), "payload.bin deployed")
    check((target / "nested" / "note.txt").is_file(), "nested/note.txt deployed")
    check(not (target / INFO_DIR_NAME).exists(), ".info not copied into target")
    check(files_after > files_before, "target file count increased")
    check(any("[DEPLOY_STAGE]" in line for line in logs), "captured [DEPLOY_STAGE]")
    check(any("[DEPLOY_RESULT]" in line for line in logs), "captured [DEPLOY_RESULT]")
    stages = _parse_stages(logs)
    for required in ("plan", "copy", "validate", "persist"):
        check(required in stages, f"stage finished: {required}")
    check(
        any("status=SUCCESS" in line for line in logs),
        "[DEPLOY_RESULT] status=SUCCESS",
    )

    return _case_report(
        case="SUCCESS",
        mod_id=SUCCESS_MOD_ID,
        result=result,
        log_lines=logs,
        files_before=files_before,
        files_after=files_after,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        elapsed_ms=elapsed_ms,
        passed=ok,
        assertions=assertions,
        extra={"source": str(source), "target": str(target)},
    )


def run_failed(library: Path, game_mods: Path, db: DatabaseManager) -> dict[str, Any]:
    """Source-missing failure must terminate FAILED (never SUCCESS)."""
    source = _seed_folder_mod(library, mod_id=FAILED_MOD_ID, title="MissingSourceMod")
    db.upsert_mod(
        ModMetadata(
            published_file_id=FAILED_MOD_ID,
            title="MissingSourceMod",
            app_id=APP_ID,
            managed_path=str(source),
            game_name=GAME_NAME,
        )
    )
    # Remove managed folder after DB registration — forces missing-source path.
    shutil.rmtree(source)
    target = game_mods / "MissingSourceMod"
    files_before, bytes_before = _tree_stats(target)
    deployer = ModDeployer(library_root=library, db=db)

    t0 = time.perf_counter()
    result, logs = _run_worker(mod_id=FAILED_MOD_ID, library=library, deployer=deployer)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    files_after, bytes_after = _tree_stats(target)

    assertions: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        assertions.append(("PASS: " if cond else "FAIL: ") + msg)
        if not cond:
            ok = False

    check(str(result.get("status")) == "FAILED", "status == FAILED")
    check(not bool(result.get("success")), "success == False")
    check(str(result.get("status")) != "SUCCESS", "must not masquerade as SUCCESS")
    check(not target.exists() or files_after == files_before, "target not falsely populated")
    check(any("[DEPLOY_RESULT]" in line for line in logs), "captured [DEPLOY_RESULT]")
    check(
        any("status=FAILED" in line for line in logs),
        "[DEPLOY_RESULT] status=FAILED",
    )

    return _case_report(
        case="FAILED",
        mod_id=FAILED_MOD_ID,
        result=result,
        log_lines=logs,
        files_before=files_before,
        files_after=files_after,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        elapsed_ms=elapsed_ms,
        passed=ok,
        assertions=assertions,
        extra={"source_removed": str(source), "target": str(target)},
    )


def run_redeploy(library: Path, game_mods: Path, db: DatabaseManager) -> dict[str, Any]:
    """Second deploy of SUCCESS mod — idempotent, no nested SmokeMod/SmokeMod."""
    source = library / GAME_NAME / "SmokeMod"
    target = game_mods / "SmokeMod"
    if not source.is_dir():
        source = _seed_folder_mod(library, mod_id=SUCCESS_MOD_ID, title="SmokeMod")
        db.upsert_mod(
            ModMetadata(
                published_file_id=SUCCESS_MOD_ID,
                title="SmokeMod",
                app_id=APP_ID,
                managed_path=str(source),
                game_name=GAME_NAME,
            )
        )

    deployer = ModDeployer(library_root=library, db=db)
    # Ensure first deploy exists (SUCCESS case may have already done this).
    if not (target / "payload.bin").is_file():
        _run_worker(mod_id=SUCCESS_MOD_ID, library=library, deployer=deployer)

    man1 = load_manifest(source)
    files1 = len(man1.files) if man1 else 0
    files_before, bytes_before = _tree_stats(target)
    nested_bad = target / "SmokeMod"

    t0 = time.perf_counter()
    result, logs = _run_worker(mod_id=SUCCESS_MOD_ID, library=library, deployer=deployer)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    files_after, bytes_after = _tree_stats(target)
    man2 = load_manifest(source)
    files2 = len(man2.files) if man2 else -1

    assertions: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        assertions.append(("PASS: " if cond else "FAIL: ") + msg)
        if not cond:
            ok = False

    check(str(result.get("status")) == "SUCCESS", "redeploy status == SUCCESS")
    check(bool(result.get("success")), "redeploy success == True")
    check((target / "payload.bin").is_file(), "payload still present")
    check(not nested_bad.exists(), "no nested SmokeMod/SmokeMod")
    check(files2 == files1 or files1 == 0, f"manifest file count stable ({files1} → {files2})")
    check(any("[DEPLOY_STAGE]" in line for line in logs), "captured [DEPLOY_STAGE]")
    check(any("[DEPLOY_RESULT]" in line for line in logs), "captured [DEPLOY_RESULT]")
    if files_before > 0:
        check(files_after == files_before, f"idempotent file count ({files_before} → {files_after})")
        check(bytes_after == bytes_before, f"idempotent byte count ({bytes_before} → {bytes_after})")
    else:
        check(files_after > 0, "redeploy produced target files")

    return _case_report(
        case="REDEPLOY",
        mod_id=SUCCESS_MOD_ID,
        result=result,
        log_lines=logs,
        files_before=files_before,
        files_after=files_after,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        elapsed_ms=elapsed_ms,
        passed=ok,
        assertions=assertions,
        extra={
            "source": str(source),
            "target": str(target),
            "manifest_files_before": files1,
            "manifest_files_after": files2,
            "nested_duplicate_present": nested_bad.exists(),
        },
    )


def run_smoke(workspace: Path) -> dict[str, Any]:
    library, game_mods, _db_file, db = _setup_workspace(workspace)
    cases = [
        run_success(library, game_mods, db),
        run_failed(library, game_mods, db),
        run_redeploy(library, game_mods, db),
    ]
    closed = all(bool(c.get("passed")) for c in cases)
    return {
        "tool": "deploy_smoke_runner",
        "workspace": str(workspace),
        "closed": closed,
        "final_status": "CLOSED" if closed else "NOT CLOSED",
        "cases": cases,
        "summary": {
            "SUCCESS": cases[0].get("passed"),
            "FAILED": cases[1].get("passed"),
            "REDEPLOY": cases[2].get("passed"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy System real-pipeline smoke runner")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Isolated workspace (default: temp dir, deleted unless --keep)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON report to this path",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep temp workspace after run",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cleanup_workspace = False
    if args.workspace is not None:
        workspace = args.workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path(tempfile.mkdtemp(prefix="smm_deploy_smoke_"))
        cleanup_workspace = not args.keep

    try:
        _ensure_qapp()
        report = run_smoke(workspace)
        # Let async post-deploy conflict scan finish before closing DB.
        time.sleep(0.5)
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            for _ in range(5):
                app.processEvents()
                time.sleep(0.05)
    finally:
        DatabaseManager.reset_instance()
        if cleanup_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        elif args.keep or args.workspace is None:
            print(f"[smoke] workspace: {workspace}", file=sys.stderr)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"[smoke] wrote {args.out}", file=sys.stderr)
    print(text)

    # Human audit footer on stderr
    print("\n=== Deploy Smoke Audit Report ===", file=sys.stderr)
    for case in report["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        print(
            f"  [{mark}] {case['case']}: status={case['status']} "
            f"elapsed_ms={case['elapsed_ms']} files={case['files_before']}→{case['files_after']}",
            file=sys.stderr,
        )
        for a in case.get("assertions") or []:
            print(f"         {a}", file=sys.stderr)
    print(f"  final_status = {report['final_status']}", file=sys.stderr)

    return 0 if report["closed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
