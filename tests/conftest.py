"""Pytest isolation: never touch production ``data/`` or ``mod/``.

Autouse fixtures redirect the SQLite singleton and path helpers to a per-test
temporary tree. Individual tests may still override with their own ``tmp_path``
DB / monkeypatches; those run after this fixture and take precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager


def _playwright_chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            path = str(pw.chromium.executable_path or "")
            return bool(path and Path(path).is_file())
    except Exception:  # noqa: BLE001
        return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "network: requires outbound network and/or Playwright browser"
    )
    config.addinivalue_line(
        "markers", "playwright: requires Playwright Chromium installed"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if _playwright_chromium_available():
        return
    skip = pytest.mark.skip(
        reason="Playwright Chromium not installed (playwright install)"
    )
    for item in items:
        if "playwright" in item.keywords or "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_production_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "_smm_isolate_data"
    library = tmp_path / "_smm_isolate_mod"
    data.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    db_file = data / "mod_manager.db"

    monkeypatch.setenv("SMM_TEST_DB", str(db_file))

    def _data_dir() -> Path:
        return data

    def _default_mod_library() -> Path:
        library.mkdir(parents=True, exist_ok=True)
        return library

    def _database_path() -> Path:
        return db_file

    # Patch both the defining module and common re-import sites.
    monkeypatch.setattr("core.paths.data_dir", _data_dir)
    monkeypatch.setattr("core.paths.default_mod_library", _default_mod_library)
    monkeypatch.setattr("core.paths.database_path", _database_path)
    monkeypatch.setattr("core.db_manager.database_path", _database_path)
    monkeypatch.setattr("services.metadata_backup.data_dir", _data_dir)
    monkeypatch.setattr(
        "services.metadata_backup_sync.default_mod_library",
        _default_mod_library,
        raising=False,
    )
    monkeypatch.setattr(
        "services.library_reconcile.default_mod_library",
        _default_mod_library,
        raising=False,
    )
    monkeypatch.setattr(
        "services.library_maintenance.data_dir",
        _data_dir,
        raising=False,
    )
    monkeypatch.setattr(
        "services.library_maintenance.default_mod_library",
        _default_mod_library,
        raising=False,
    )
    monkeypatch.setattr(
        "services.importers.archive.data_dir",
        _data_dir,
        raising=False,
    )

    DatabaseManager.reset_instance()
    DatabaseManager.instance(db_file)
    try:
        from services.identity_service import _ALLOW_INTERNAL_CREATE, _LIFECYCLE

        _LIFECYCLE.set("")
        _ALLOW_INTERNAL_CREATE.set(False)
    except Exception:  # noqa: BLE001
        pass
    yield
    # Drain cover-loader pool before processEvents — avoids Qt native heap
    # corruption (0xc0000374) when late cover callbacks touch freed QObjects.
    try:
        from services.cover_loader import CoverLoaderManager

        CoverLoaderManager.reset_instance()
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            # Bounded, non-aggressive flush — do not spin dozens of times.
            for _ in range(3):
                app.processEvents(QCoreApplication.ProcessEventsFlag.AllEvents, 50)
    except Exception:  # noqa: BLE001
        pass
    DatabaseManager.reset_instance()
    monkeypatch.delenv("SMM_TEST_DB", raising=False)
