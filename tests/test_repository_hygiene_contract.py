"""Repository hygiene contract — prevent root / docs / data pollution.

Inspects paths only. Does not delete or move anything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = Path(__file__).resolve().parent / "repository_report_allowlist.json"

# Root filenames matching these patterns fail unless explicitly allowlisted.
_ROOT_FORBIDDEN_GLOBS = (
    "*_audit.json",
    "p0_*.json",
    "_phase*.json",
    "*.log",
)
_ROOT_REPORT_RE = re.compile(r".+_report\.md$", re.IGNORECASE)

_ALLOWED_TMP_CHILDREN = frozenset(
    {
        "reports",
        "audits",
        "dumps",
        "probes",
        "archive",
        "README.md",
    }
)


def _load_report_allowlist() -> set[str]:
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {str(name).strip() for name in data.get("allowed_root_reports", [])}


def test_report_allowlist_file_exists() -> None:
    assert ALLOWLIST_PATH.is_file(), f"missing allowlist: {ALLOWLIST_PATH}"
    names = _load_report_allowlist()
    assert names, "allowed_root_reports must not be empty"


def test_root_forbids_audit_phase_and_log_artifacts() -> None:
    offenders: list[str] = []
    for pattern in _ROOT_FORBIDDEN_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file():
                offenders.append(path.name)
    assert not offenders, (
        "repo root must not contain audit/phase/log dumps; "
        f"move to _tmp/: {sorted(offenders)}"
    )


def test_root_report_md_must_be_allowlisted() -> None:
    allowed = _load_report_allowlist()
    offenders: list[str] = []
    for path in ROOT.iterdir():
        if not path.is_file():
            continue
        if not _ROOT_REPORT_RE.match(path.name):
            continue
        if path.name not in allowed:
            offenders.append(path.name)
    assert not offenders, (
        "root *_report.md must be allowlisted in "
        "tests/repository_report_allowlist.json "
        f"or moved to _tmp/reports/: {sorted(offenders)}"
    )


def test_docs_must_not_contain_json_dumps() -> None:
    docs = ROOT / "docs"
    if not docs.is_dir():
        return
    offenders = sorted(p.as_posix() for p in docs.rglob("*.json") if p.is_file())
    assert not offenders, (
        "docs/*.json forensic dumps are forbidden; use _tmp/audits/ or "
        f"_tmp/dumps/: {offenders}"
    )


def test_data_must_not_contain_forensic_json() -> None:
    data = ROOT / "data"
    if not data.is_dir():
        return
    offenders = sorted(
        p.relative_to(ROOT).as_posix()
        for p in data.glob("*.json")
        if p.is_file()
    )
    assert not offenders, (
        "data/*.json forensic output is forbidden; use _tmp/: "
        f"{offenders}"
    )


def test_temporary_artifacts_live_under_tmp() -> None:
    """_tmp/ exists and only hosts the approved subtrees."""
    tmp = ROOT / "_tmp"
    assert tmp.is_dir(), "_tmp/ must exist for temporary artifacts"
    unexpected = sorted(
        child.name
        for child in tmp.iterdir()
        if child.name not in _ALLOWED_TMP_CHILDREN
        and not child.name.startswith(".")
    )
    assert not unexpected, (
        "_tmp/ children must be reports/audits/dumps/probes/archive "
        f"(+ README.md); unexpected: {unexpected}"
    )


def test_gitignore_covers_tmp_data_logs_and_audit_dumps() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    required_fragments = (
        "_tmp/",
        "data/",
        "*.log",
        "/p0_*.json",
        "/*_audit*.json",
        "/_phase*.json",
        "docs/*.json",
    )
    missing = [frag for frag in required_fragments if frag not in text]
    assert not missing, f".gitignore missing required rules: {missing}"

    # Runtime DB sidecars / accidental root DBs
    for frag in ("*.db-wal", "*.db-shm"):
        assert frag in text, f".gitignore must ignore {frag}"
