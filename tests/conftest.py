"""Pytest isolation: never touch production ``data/`` or ``mod/``.

Autouse fixtures redirect the SQLite singleton and path helpers to a per-test
temporary tree. Individual tests may still override with their own ``tmp_path``
DB / monkeypatches; those run after this fixture and take precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager

# Cache Playwright probe — filesystem only (never start the driver at collection).
_PLAYWRIGHT_OK: bool | None = None


def _playwright_chromium_available() -> bool:
    """True when Playwright package + Chromium browser files are present.

    Avoids ``sync_playwright()`` at collection time — starting the driver
    leaks asyncio tasks (``TargetClosedError``) and slows every pytest run.
    """
    global _PLAYWRIGHT_OK
    if _PLAYWRIGHT_OK is not None:
        return _PLAYWRIGHT_OK
    try:
        import playwright  # noqa: F401
    except ImportError:
        _PLAYWRIGHT_OK = False
        return False

    import os

    roots: list[Path] = []
    env_root = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env_root:
        roots.append(Path(env_root))
    local_app = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app:
        roots.append(Path(local_app) / "ms-playwright")
    roots.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")

    for root in roots:
        if not root.is_dir():
            continue
        for chromium_dir in root.glob("chromium-*"):
            for rel in (
                Path("chrome-win") / "chrome.exe",
                Path("chrome-win64") / "chrome.exe",
                Path("chrome-linux") / "chrome",
                Path("chrome-mac") / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            ):
                if (chromium_dir / rel).is_file():
                    _PLAYWRIGHT_OK = True
                    return True
    _PLAYWRIGHT_OK = False
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


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Breadcrumb for native crashes — last nodeid before an AV."""
    try:
        path = Path(__file__).resolve().parents[1] / "_tmp" / "dumps" / "qt_crash" / "last_test.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.nodeid, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _stub_blocking_qt_modals(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Prevent indefinite hangs from modal Qt dialogs in headless pytest.

    Production UI may call ``QMessageBox.warning`` / ``QFileDialog`` when a
    precondition fails. In CI those dialogs block the main thread forever.
    Tests that need dialog behaviour can re-monkeypatch locally.
    """
    try:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
    except Exception:  # noqa: BLE001
        return

    def _msgbox_noop(*_a, **_k):  # noqa: ANN001
        return QMessageBox.StandardButton.Ok

    for name in ("warning", "information", "critical", "question", "about"):
        if hasattr(QMessageBox, name):
            monkeypatch.setattr(QMessageBox, name, staticmethod(_msgbox_noop))

    def _dialog_reject(*_a, **_k):  # noqa: ANN001
        return ("", "")

    def _dialog_reject_dir(*_a, **_k):  # noqa: ANN001
        return ""

    def _dialog_reject_many(*_a, **_k):  # noqa: ANN001
        return ([], "")

    if hasattr(QFileDialog, "getOpenFileName"):
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName", staticmethod(_dialog_reject)
        )
    if hasattr(QFileDialog, "getSaveFileName"):
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(_dialog_reject)
        )
    if hasattr(QFileDialog, "getOpenFileNames"):
        monkeypatch.setattr(
            QFileDialog, "getOpenFileNames", staticmethod(_dialog_reject_many)
        )
    if hasattr(QFileDialog, "getExistingDirectory"):
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(_dialog_reject_dir)
        )


@pytest.fixture(autouse=True)
def _qt_test_lifecycle(request: pytest.FixtureRequest) -> None:
    """Snapshot → test → destroy top-level widgets / timers / cover pool."""
    before = None
    try:
        from tests.qt_test_lifecycle import snapshot_qt_resources, qt_teardown_pass

        before = snapshot_qt_resources()
    except Exception:  # noqa: BLE001
        before = None
    yield
    if before is None:
        return
    try:
        from tests.qt_test_lifecycle import qt_teardown_pass

        nodeid = getattr(request.node, "nodeid", "") or request.node.name
        qt_teardown_pass(nodeid=nodeid, before=before, report_leaks=True)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _isolate_production_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "_smm_isolate_data"
    library = tmp_path / "_smm_isolate_mod"
    config = tmp_path / "_smm_isolate_config"
    cache = tmp_path / "_smm_isolate_cache"
    logs = tmp_path / "_smm_isolate_logs"
    data.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    db_file = data / "mod_manager.db"

    monkeypatch.setenv("SMM_TEST_DB", str(db_file))

    def _data_dir() -> Path:
        return data

    def _cache_dir() -> Path:
        cache.mkdir(parents=True, exist_ok=True)
        return cache

    def _logs_dir() -> Path:
        logs.mkdir(parents=True, exist_ok=True)
        return logs

    def _config_dir() -> Path:
        return config

    def _load_order_dir() -> Path:
        path = config / "load_order"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _default_mod_library() -> Path:
        library.mkdir(parents=True, exist_ok=True)
        return library

    def _database_path() -> Path:
        return db_file

    # Patch both the defining module and common re-import sites.
    monkeypatch.setattr("core.paths.data_dir", _data_dir)
    monkeypatch.setattr("core.paths.get_cache_dir", _cache_dir)
    monkeypatch.setattr("core.paths.logs_dir", _logs_dir)
    monkeypatch.setattr("core.paths.config_dir", _config_dir)
    monkeypatch.setattr("core.paths.load_order_dir", _load_order_dir)
    monkeypatch.setattr("core.paths.default_mod_library", _default_mod_library)
    monkeypatch.setattr("core.paths.database_path", _database_path)
    monkeypatch.setattr("core.db_manager.database_path", _database_path)
    monkeypatch.setattr("services.metadata_backup.data_dir", _data_dir)
    monkeypatch.setattr(
        "services.mod_path_validation.default_mod_library",
        _default_mod_library,
        raising=False,
    )
    monkeypatch.setattr(
        "services.mod_path_validation.data_dir",
        _data_dir,
        raising=False,
    )
    monkeypatch.setattr(
        "tools.archive.legacy_workspace_backup.data_dir",
        _data_dir,
        raising=False,
    )
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
        from services.mod_type_catalog import reset_mod_type_catalog

        reset_mod_type_catalog()
    except Exception:  # noqa: BLE001
        pass
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
        from services.library_reconcile import (
            join_reconcile_thread,
            request_reconcile_shutdown,
            reset_reconcile_async_state,
        )

        request_reconcile_shutdown()
        join_reconcile_thread(0.5)
        reset_reconcile_async_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.metadata_backup_sync import drain_backup_queue

        drain_backup_queue(timeout=0.5)
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


@pytest.fixture(autouse=True)
def _resolve_workspace_handles_to_mod_pk(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test-only lookup remapping for mutating/deploy APIs that still pass Workshop digits.

    Production ``resolve_mod_pk`` never resolves ``workspace_id``. This wrapper is
    lookup-only in the pytest process — it never invents rows.

    Does **not** soft-resolve read APIs (``get_mod`` / ``get_mod_display_info`` /
    ``get_mod_backup_row`` / ``get_mod_status``) so identity-contract tests keep
    observing ``workspace_id != mods.mod_id``.

    Skipped for dedicated identity-architecture / steam-refresh-identity tests.
    """
    nodeid = getattr(request.node, "nodeid", "") or ""
    if any(
        key in nodeid
        for key in (
            "test_steam_refresh_identity",
            "test_id_architecture_contract",
            "test_identity_governance",
            "test_identity_write_boundary",
            "test_identity_lifecycle",
            "test_identity_minimal",
            "test_identity_boundary",
            "test_backup_identity",
            "test_lifecycle_boundary",
            "test_published_id_index",
        )
    ):
        return

    from tests.helpers.identity import soft_resolve_test_mod_pk

    def _wrap_db_method(name: str) -> None:
        if not hasattr(DatabaseManager, name):
            return
        original = getattr(DatabaseManager, name)

        def _bound(self, handle, *args, **kwargs):  # noqa: ANN001
            pk = soft_resolve_test_mod_pk(self, handle)
            return original(self, pk, *args, **kwargs)

        monkeypatch.setattr(DatabaseManager, name, _bound)

    # Mutating APIs only — never wrap get_mod / get_mod_display_info / get_mod_status.
    for method in (
        "update_mod_identity_fields",
        "update_mod_user_metadata",
        "set_mod_files",
        "get_mod_files",
        "update_mod_backup_snapshot",
        "update_mod_status",
        "update_mod_content_status",
        "update_mod_identity_status",
        "touch_mod_updated_at",
        "update_mod_deploy_status",
        "set_official_metadata_synced",
        "update_mod_platform_info",
        "enable_mod",
        "disable_mod",
        "update_mod_version",
        "add_mod_tag",
        "add_category_tag",
        "remove_category_tag",
        "update_mod_conflict_annotation",
    ):
        _wrap_db_method(method)

    if hasattr(DatabaseManager, "batch_update_platform"):
        _orig_batch_plat = DatabaseManager.batch_update_platform

        def _batch_update_platform(self, mod_ids, platform, *args, **kwargs):  # noqa: ANN001
            resolved = [soft_resolve_test_mod_pk(self, mid) for mid in mod_ids]
            return _orig_batch_plat(self, resolved, platform, *args, **kwargs)

        monkeypatch.setattr(DatabaseManager, "batch_update_platform", _batch_update_platform)

    try:
        from services.deploy import ModDeployer
    except Exception:  # noqa: BLE001
        ModDeployer = None  # type: ignore[misc, assignment]

    if ModDeployer is not None:

        def _wrap_deploy(method_name: str) -> None:
            if not hasattr(ModDeployer, method_name):
                return
            original = getattr(ModDeployer, method_name)

            def _bound(self, handle, *args, **kwargs):  # noqa: ANN001
                from services.deploy_identity import (
                    frozen_internal_id_for_pk,
                    is_frozen_internal_uuid,
                )

                token = str(handle or "").strip()
                if is_frozen_internal_uuid(token):
                    return original(self, token, *args, **kwargs)
                db = getattr(self, "db", None) or getattr(self, "_db", None)
                if db is None:
                    try:
                        from core.db_manager import get_db

                        db = get_db()
                    except Exception:  # noqa: BLE001
                        db = None
                if db is None:
                    return original(self, token, *args, **kwargs)
                from tests.helpers.identity import _mod_pk_exists

                # Keep digit PK so tests can still assert deploy_mod(PK) is rejected.
                if _mod_pk_exists(db, token):
                    return original(self, token, *args, **kwargs)
                pk = soft_resolve_test_mod_pk(db, handle)
                frozen = frozen_internal_id_for_pk(pk, db=db) if str(pk).isdigit() else ""
                if frozen:
                    return original(self, frozen, *args, **kwargs)
                return original(self, token, *args, **kwargs)

            monkeypatch.setattr(ModDeployer, method_name, _bound)

        for method in ("deploy_mod", "undeploy_mod", "redeploy_mod"):
            _wrap_deploy(method)

    try:
        import services.metadata_backup as mb
    except Exception:  # noqa: BLE001
        return

    if hasattr(mb, "backup_root"):
        _orig_backup_root = mb.backup_root

        def _backup_root(handle, *args, **kwargs):  # noqa: ANN001
            from services.backup_identity import (
                frozen_uuid_for_mod_pk,
                is_frozen_backup_uuid,
            )

            token = str(handle or "").strip()
            if is_frozen_backup_uuid(token):
                return _orig_backup_root(token, *args, **kwargs)
            try:
                from core.db_manager import get_db

                pk = soft_resolve_test_mod_pk(get_db(), handle)
                frozen = frozen_uuid_for_mod_pk(pk)
                if frozen:
                    return _orig_backup_root(frozen, *args, **kwargs)
                return _orig_backup_root(pk, *args, **kwargs)
            except Exception:  # noqa: BLE001
                return _orig_backup_root(handle, *args, **kwargs)

        monkeypatch.setattr(mb, "backup_root", _backup_root)

    if hasattr(mb, "load_backup"):
        _orig_load = mb.load_backup

        def _load_backup(handle, *args, **kwargs):  # noqa: ANN001
            from services.backup_identity import is_frozen_backup_uuid

            token = str(handle or "").strip()
            if is_frozen_backup_uuid(token):
                return _orig_load(token, *args, **kwargs)
            try:
                from core.db_manager import get_db

                pk = soft_resolve_test_mod_pk(get_db(), handle)
            except Exception:  # noqa: BLE001
                pk = handle
            return _orig_load(pk, *args, **kwargs)

        monkeypatch.setattr(mb, "load_backup", _load_backup)

    if hasattr(mb, "mark_missing"):
        _orig_missing = mb.mark_missing

        def _mark_missing(handle, *args, **kwargs):  # noqa: ANN001
            try:
                from core.db_manager import get_db

                pk = soft_resolve_test_mod_pk(get_db(), handle)
            except Exception:  # noqa: BLE001
                pk = handle
            return _orig_missing(pk, *args, **kwargs)

        monkeypatch.setattr(mb, "mark_missing", _mark_missing)

    if hasattr(mb, "sync_metadata_backup"):
        _orig_sync = mb.sync_metadata_backup

        def _sync_metadata_backup(folder, *args, mod_id="", **kwargs):  # noqa: ANN001
            try:
                from core.db_manager import get_db

                pk = soft_resolve_test_mod_pk(get_db(), mod_id) if mod_id else mod_id
            except Exception:  # noqa: BLE001
                pk = mod_id
            return _orig_sync(folder, *args, mod_id=pk, **kwargs)

        monkeypatch.setattr(mb, "sync_metadata_backup", _sync_metadata_backup)
