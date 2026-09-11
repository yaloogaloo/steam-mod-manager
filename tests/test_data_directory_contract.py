"""Data directory contract — runtime paths only under data/.

Inspects first-level (and named forbidden) paths. Does not delete or move.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# Production runtime tree (Phase 3 Task 3).
ALLOWED_TOP_LEVEL = frozenset(
    {
        "mod_manager.db",
        "mod_manager.db-wal",
        "mod_manager.db-shm",
        "mod_backup",
        "deploy_backup",
        "asset_cache",
        "identity_repair_quarantine",
        "headers",
        "import_cache",
        "collection_covers",
        # Benign / gitignored runtime scratch (not user Mod payload).
        ".gitkeep",
        "app_instance.lock",
        "import_crash_trace.log",
        "wh3",
        "mod_types.json",
        "mod_types.legacy_migrated",
    }
)

_FORBIDDEN_DIR_NAMES = frozenset({"browser_profile"})
_FORBIDDEN_TOP_PREFIXES = (
    "p0_",
)
_PRE_IDENTITY_RE = re.compile(r"pre_identity_rebuild", re.IGNORECASE)
_REBUILD_DB_RE = re.compile(r"^mod_manager\.rebuild_.*\.db$", re.IGNORECASE)
_REBUILD_DB_ALT_RE = re.compile(r"^rebuild_.*\.db$", re.IGNORECASE)


def test_data_directory_exists() -> None:
    assert DATA.is_dir(), f"expected runtime data dir: {DATA}"


def test_data_forbids_historical_pollution_paths() -> None:
    if not DATA.is_dir():
        return
    offenders: list[str] = []
    for child in DATA.iterdir():
        name = child.name
        if name in _FORBIDDEN_DIR_NAMES:
            offenders.append(name)
            continue
        if any(name.startswith(pref) for pref in _FORBIDDEN_TOP_PREFIXES):
            offenders.append(name)
            continue
        if _PRE_IDENTITY_RE.search(name):
            offenders.append(name)
            continue
        if _REBUILD_DB_RE.match(name) or _REBUILD_DB_ALT_RE.match(name):
            offenders.append(name)
            continue
    assert not offenders, (
        "data/ contains forbidden historical paths "
        f"(archive under _tmp/archive/): {sorted(offenders)}"
    )


def test_data_top_level_only_runtime_allowed() -> None:
    if not DATA.is_dir():
        return
    unexpected = sorted(
        child.name
        for child in DATA.iterdir()
        if child.name not in ALLOWED_TOP_LEVEL
    )
    assert not unexpected, (
        "data/ top-level must be runtime-only; unexpected entries "
        f"(move forensic dumps to _tmp/): {unexpected}"
    )
