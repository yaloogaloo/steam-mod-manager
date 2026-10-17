"""Project layout governance — cleanup only; no schema or business-logic changes."""

from __future__ import annotations

from pathlib import Path

from core.db_manager import DatabaseManager
from core.paths import database_path, project_root

ROOT = project_root()
DATA = ROOT / "data"


def test_layout_docs_and_audits_exist() -> None:
    assert (ROOT / "docs" / "project_layout.md").is_file()
    assert (ROOT / "docs" / "cache_layout.md").is_file()
    assert (ROOT / "_tmp" / "db_runtime_file_audit.json").is_file()
    assert (ROOT / "_tmp" / "root_document_audit.json").is_file()
    assert (ROOT / "_tmp" / "data_top_level_audit.json").is_file()


def test_root_markdown_is_readme_only() -> None:
    md = sorted(p.name for p in ROOT.glob("*.md"))
    assert md == ["README.md"]
    assert (ROOT / "main.py").is_file()
    assert (ROOT / "pytest.ini").is_file()
    assert (ROOT / "requirements.txt").is_file()
    assert not (ROOT / "info_asset_usage_inventory.md").exists()
    assert (ROOT / "docs" / "detail_cleanup_report.md").is_file()
    assert (ROOT / "docs" / "architecture" / "identity_repair_architecture_report.md").is_file()
    assert (ROOT / "docs" / "architecture" / "identity_write_boundary_report.md").is_file()


def test_logs_dir_is_not_under_data() -> None:
    import core.paths as paths

    logs = paths.logs_dir().resolve()
    data = paths.data_dir().resolve()
    try:
        logs.relative_to(data)
        raise AssertionError(f"logs_dir resolved under data/: {logs}")
    except ValueError:
        pass


def test_sqlite_starts_in_wal_and_library_loads() -> None:
    db = DatabaseManager.instance()
    assert db.is_closed() is False
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    count, _bindings = db.count_mods_and_type_bindings()
    assert count >= 0
    assert database_path().name == "mod_manager.db"


def test_isolated_sqlite_file_is_not_production() -> None:
    db = DatabaseManager.instance()
    opened = Path(db.db_path).resolve()
    production = (ROOT / "data" / "mod_manager.db").resolve()
    assert opened != production


def test_cache_contract_helpers_still_under_cache() -> None:
    import core.paths as paths

    cache = paths.get_cache_dir().resolve()
    assert paths.offline_view_cache_dir().resolve().parent == cache
    assert paths.asset_cache_dir().resolve().parent == cache


def test_logs_placeholder_exists_on_disk() -> None:
    assert (ROOT / "logs" / ".gitkeep").is_file()


def test_production_data_still_has_no_cache_dirs() -> None:
    present = {p.name for p in DATA.iterdir()}
    leftover = present & {"offline_view", "asset_cache", "import_cache", "headers"}
    assert not leftover
