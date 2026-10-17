"""Test repository governance — forbid pollution naming in tests/.

New tests must name a business contract or regression, not debug/trace/tmp noise.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Basename patterns forbidden for NEW test modules under tests/ (recursive).
_FORBIDDEN_NAME_RE = re.compile(
    r"(?:^|[_-])(debug|trace|tmp)(?:[_-]|$)|(?:^|[_-])final(?:[_-]|$)|phase\d+",
    re.IGNORECASE,
)

# Frozen exceptions that predate this contract and remain runnable.
_LEGACY_ALLOWLIST = frozenset(
    {
        "test_debug_config.py",
        "test_deploy_path_final_safety.py",
        "test_deploy_phase4_contracts.py",
        "test_detail_ux_phase114.py",
        "test_final_status_boundary_contract.py",
        "test_identity_authority_final.py",
        "test_library_phase3_polish.py",
        "test_library_projection_final_audit.py",
        "test_widget_show_trace.py",
        "test_test_repository_governance.py",
    }
)


def test_no_forbidden_test_module_names() -> None:
    offenders: list[str] = []
    for path in TESTS.rglob("test_*.py"):
        name = path.name
        if name in _LEGACY_ALLOWLIST:
            continue
        if _FORBIDDEN_NAME_RE.search(name):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        "tests/ modules must not use debug/trace/tmp/final/phaseN naming; "
        f"rename to a business contract: {sorted(offenders)}"
    )


def test_archived_historical_tests_not_collected() -> None:
    archive = ROOT / "_tmp" / "archive" / "tests"
    if not archive.is_dir():
        return
    archived = {p.name for p in archive.glob("test_*.py")}
    live = {p.name for p in TESTS.rglob("test_*.py")}
    overlap = sorted(archived & live)
    assert not overlap, f"archived tests still present under tests/: {overlap}"


def test_pytest_ini_scopes_collection_to_tests() -> None:
    text = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths" in text
    assert re.search(r"(?m)^\s*testpaths\s*=\s*tests\s*$", text), (
        "pytest.ini must set testpaths = tests to avoid collecting _tmp dumps"
    )
