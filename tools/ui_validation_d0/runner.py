"""Preflight / fixture entry. UI capture is deliberately fail-closed for now."""
from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
import subprocess
import sys

# Supports direct invocation without importing tools or ui package initializers.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ui_validation_d0.safety import Boundary, IsolationError, NATIVE_BLOCKERS, launch_checks, require_ui_admission, suspects
    from ui_validation_d0.fixtures import build, SCENARIOS
else:
    from .safety import Boundary, IsolationError, NATIVE_BLOCKERS, launch_checks, require_ui_admission, suspects
    from .fixtures import build, SCENARIOS


def process_snapshot(repository):
    raw = subprocess.check_output(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Depth 3"], encoding="utf-8", errors="strict")
    parsed = json.loads(raw)
    return suspects(parsed if isinstance(parsed, list) else [parsed], repository)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "fixtures", "capture"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--scenario", choices=SCENARIOS, default="small")
    parser.add_argument("--page", choices=["library", "sync", "deploy", "import", "edit"], default="library")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    options = parser.parse_args(argv)
    try:
        boundary = Boundary(options.root, options.repository)
        active = process_snapshot(boundary.repository)
        if options.command == "fixtures":
            if active:
                raise IsolationError("Production SMM running; fixture CLI refused")
            print(json.dumps(build(boundary, options.count), ensure_ascii=False))
            return 0
        checks = launch_checks(boundary, active, dict(os.environ))
        report = {"active_production_processes": active, "SAFE UI LAUNCH": "BLOCKED", "checks": checks, "D002_D005_admission": "NOT CLOSED", "blockers": list(NATIVE_BLOCKERS), "ui_started": False, "requested": {"size": [options.width, options.height], "scenario": options.scenario, "page": options.page}}
        if options.command == "capture":
            # Persist every refusal instead of stopping at the first condition.
            print(json.dumps(report, ensure_ascii=False, indent=2))
            require_ui_admission(active, boundary=boundary, environment=dict(os.environ))
            raise IsolationError("Real page integration not implemented; no capture")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 3  # Not success: mandatory isolation conditions remain open.
    except (IsolationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "NOT CLOSED", "ui_started": False, "reason": str(exc)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
