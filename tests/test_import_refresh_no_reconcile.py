"""Import success must refresh Library UI without full-library reconcile."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from ui.library_view import ModLibraryView

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "import_no_reconcile.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    DatabaseManager.reset_instance()


def _mod_src(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "main.pak").write_bytes(b"pak")
    return folder


def test_import_after_refresh_does_not_call_library_reconcile(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    lib = tmp_path / "lib"
    lib.mkdir()

    reconcile = MagicMock(return_value=True)
    monkeypatch.setattr(
        "services.library_reconcile.start_reconcile_library_async",
        reconcile,
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))

    # Simulate import-success UI path (same kwargs as _after_import).
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    reconcile.assert_not_called()

    # Library is a read projection — Refresh must never schedule Reconcile.
    view.refresh(force=True)
    qapp.processEvents()
    reconcile.assert_not_called()


def test_single_mod_import_still_runs_backup_sync_reason_import(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    src = _mod_src(tmp_path / "src", "ImportBackupMod")

    sync = MagicMock(return_value=True)
    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change",
        sync,
    )

    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="Import Backup Mod",
        nexus_url="https://www.nexusmods.com/palworld/mods/55501",
        nexus_id="55501",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success
    assert sync.called
    reasons = [c.args[2] for c in sync.call_args_list if len(c.args) >= 3]
    assert "import" in reasons
    # materialize passes (mod_id, dest, "import")
    assert any(
        str(c.args[0]) == str(result.mod_id) and c.args[2] == "import"
        for c in sync.call_args_list
        if len(c.args) >= 3
    )


def test_refresh_ui_shows_imported_mod_without_reconcile(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    lib = tmp_path / "lib"
    src = _mod_src(tmp_path / "src", "VisibleAfterImport")

    reconcile = MagicMock(return_value=True)
    monkeypatch.setattr(
        "services.library_reconcile.start_reconcile_library_async",
        reconcile,
    )

    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="Visible After Import",
        nexus_url="https://www.nexusmods.com/palworld/mods/55502",
        nexus_id="55502",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success
    assert result.managed_path
    assert Path(result.managed_path).is_dir()

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()

    reconcile.assert_not_called()
    assert len(view._cards) >= 1
    titles = " ".join(
        (c.title_label.text() if hasattr(c, "title_label") else "")
        for c in view._cards
    )
    assert "Visible" in titles or any(
        str(result.mod_id) in str(getattr(c, "_mod_id", lambda: "")())
        or str(result.mod_id) in str(getattr(c, "mod_id", ""))
        for c in view._cards
    )


def test_import_callbacks_pass_reconcile_false() -> None:
    src = inspect.getsource(ModLibraryView._on_import_single_mod)
    assert "reconcile=False" in src
    src_batch = inspect.getsource(ModLibraryView._on_import_batch_directory)
    assert "reconcile=False" in src_batch
    src_html = inspect.getsource(ModLibraryView._on_import_batch_offline_html)
    assert "reconcile=False" in src_html


def test_materialize_still_calls_import_backup_sync() -> None:
    from services.importers import materialize as mat

    src = inspect.getsource(mat.materialize_imported_mod)
    assert 'sync_after_metadata_change(mid, dest, "import")' in src
