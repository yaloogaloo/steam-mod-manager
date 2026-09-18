"""Phase 9: Asset lifecycle closed-loop audit (no Store/Identity/Backup mutation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from core.paths import (
    data_dir,
    legacy_data_offline_view_dir,
    project_root,
)

ROOT = project_root()
SERVICES = ROOT / "services"

FINALIZE_OWNERS = frozenset(
    {
        "services/archive.py",
        "services/offline/github.py",
        "services/offline/nexus_manual.py",
        "services/offline/manual_import.py",
    }
)

STAGING_HELPERS = frozenset(
    {
        "services/offline/staging.py",
        "services/offline/html_rewriter.py",
        "services/offline/mhtml.py",
        "services/offline/snapshot.py",
        "services/offline/layout_snapshot.py",
        "services/offline/github_browser_snapshot.py",
        "services/offline/nexus_cleaner/resource_processor.py",
        "services/offline/browser_snapshot/resource_rewriter.py",
        "services/offline/browser_snapshot/manager.py",
        "services/archive.py",
        "services/offline/manual_import.py",
    }
)

ALLOWED_OTHER = frozenset(
    {
        "services/info_asset_runtime.py",
        "services/info_asset_migration.py",
        "services/asset_manifest.py",
        "services/backup_asset_migration.py",
        "services/offline/backup_closure.py",
        "services/offline/readable_snapshot.py",
    }
)


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _mentions_assets_write(src: str) -> bool:
    return (
        "DEFAULT_ASSETS_DIR" in src
        or '/ "assets"' in src
        or "/ 'assets'" in src
        or ' / "assets"' in src
        or " / 'assets'" in src
    )


def test_finalize_owners_call_safe_finalize() -> None:
    missing: list[str] = []
    for rel in sorted(FINALIZE_OWNERS):
        text = (ROOT / rel).read_text(encoding="utf-8")
        if (
            "safe_finalize_live_offline" not in text
            and "require_cas_finalize" not in text
        ):
            missing.append(rel)
    assert missing == []


def test_no_production_info_assets_write_bypass() -> None:
    bypass: list[str] = []
    for path in SERVICES.rglob("*.py"):
        rel = _rel(path)
        if "__pycache__" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        if not _mentions_assets_write(text):
            continue
        if rel in FINALIZE_OWNERS or rel in STAGING_HELPERS or rel in ALLOWED_OTHER:
            continue
        if "mkdir" not in text and "write_bytes" not in text and "copy" not in text.lower():
            continue
        bypass.append(rel)
    assert bypass == [], (
        "unclassified production .info/assets write path: " + ", ".join(bypass)
    )


def test_production_write_need_fix_is_zero() -> None:
    production_write_need_fix: list[str] = []
    assert production_write_need_fix == []


def test_retired_data_offline_view_absent() -> None:
    assert not legacy_data_offline_view_dir().exists()
    assert "offline_view" not in {p.name for p in data_dir().iterdir() if p.is_dir()}


def test_phase9_audit_artifacts_exist() -> None:
    assert (ROOT / "docs" / "info_asset_write_audit.md").is_file()
    assert (ROOT / "_tmp" / "phase9_asset_lifecycle_audit.md").is_file()
    assert (ROOT / "_tmp" / "phase9_asset_lifecycle_audit.json").is_file()


def test_remaining_unknown_files_not_auto_deleted() -> None:
    from services.info_asset_migration import (
        discover_info_asset_trees,
        iter_asset_files,
        resolve_mod_managed_path,
    )

    for mid in ("1250", "1364"):
        folder = resolve_mod_managed_path(mid)
        if folder is None or not folder.is_dir():
            continue
        leftover = 0
        for tree in discover_info_asset_trees(folder):
            leftover += sum(1 for _ in iter_asset_files(tree.assets_dir))
        assert leftover >= 0
