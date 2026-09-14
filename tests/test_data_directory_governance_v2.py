"""Data directory governance v2 — freeze runtime tree; reject pollution dirs.

Inspects paths only. Does not delete or move anything.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TMP = ROOT / "_tmp"

# Frozen runtime allowlist (Phase 3 Task 5). Sidecar/runtime scratch allowed.
ALLOWED_DATA_TOP = frozenset(
    {
        "mod_manager.db",
        "mod_manager.db-wal",
        "mod_manager.db-shm",
        "mod_backup",
        "deploy_backup",
        "asset_store",
        "identity_repair_quarantine",
        "collection_covers",
        ".gitkeep",
        "app_instance.lock",
        "import_crash_trace.log",
        "wh3",
        "mod_types.json",
        "mod_types.legacy_migrated",
    }
)

# Retired under data/ — regenerable caches live under cache/ only.
FORBIDDEN_RETIRED_DATA_DIRS = frozenset(
    {"offline_view", "asset_cache", "import_cache", "headers"}
)
# Exact directory names forbidden outside ``_tmp/`` (and not in ALLOWED_DATA_TOP).
FORBIDDEN_DIR_NAMES = frozenset(
    {
        "backup",
        "repair",
        "audit",
        "report",
        "snapshot",
    }
) | FORBIDDEN_RETIRED_DATA_DIRS

# First-level trees scanned for forbidden dir names (not recursive into huge trees).
_SCAN_TOP_LEVEL = (
    ".",
    "core",
    "services",
    "ui",
    "scripts",
    "tools",
    "docs",
    "config",
    "tests",
    "data",
)


def test_data_runtime_tree_frozen() -> None:
    assert DATA.is_dir(), f"missing data/: {DATA}"
    unexpected = sorted(
        child.name for child in DATA.iterdir() if child.name not in ALLOWED_DATA_TOP
    )
    assert not unexpected, (
        "data/ runtime tree is frozen; unexpected top-level entries "
        f"(use _tmp/ for audits/reports): {unexpected}"
    )


def test_forbidden_governance_dirs_outside_tmp() -> None:
    """Reject new backup/repair/audit/report/snapshot dirs outside ``_tmp/``."""
    offenders: list[str] = []
    for rel in _SCAN_TOP_LEVEL:
        base = ROOT if rel == "." else ROOT / rel
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            name = child.name.lower()
            if name not in FORBIDDEN_DIR_NAMES:
                continue
            # Never flag paths under _tmp/
            try:
                child.relative_to(TMP)
                continue
            except ValueError:
                pass
            offenders.append(child.relative_to(ROOT).as_posix())

    assert not offenders, (
        "forbidden governance directories must live under _tmp/ only: "
        f"{sorted(offenders)}"
    )


def test_tmp_is_designated_non_runtime_home() -> None:
    assert TMP.is_dir(), "_tmp/ must exist for reports/audits/dumps/probes/archive"
    assert (TMP / "reports").is_dir(), "_tmp/reports/ must exist for governance docs"
