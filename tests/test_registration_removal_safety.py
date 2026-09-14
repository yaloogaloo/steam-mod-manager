"""Registration removal must never delete filesystem content."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.paths import project_root
from services.mod_library_cache import build_library_snapshot
from services.mod_path_validation import (
    FORBIDDEN_MOD_ROOT_REASON,
    is_forbidden_mod_root,
    validate_import_source_root,
    validate_managed_mod_path,
    InvalidModRootError,
)
from services.importers.directory_batch import discover_mod_directories
from services.path_lifecycle import commit_path_change
from services.registration_removal import ModRegistrationRemovalService
from tests.helpers.identity import write_info_sidecar


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    from services.mod_library_cache import reset_library_cache

    reset_library_cache()
    manager = DatabaseManager.instance(tmp_path / "registration_removal.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()
    reset_library_cache()


def _ensure_game(db: DatabaseManager, app_id: int, name: str) -> None:
    if app_id <= 0:
        # Schema seeds app_id=0 as unknown-game sentinel.
        return
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))


def _insert_pollution_mod(
    db: DatabaseManager,
    *,
    mod_id: int,
    app_id: int,
    title: str,
    last_known_path: str,
    workspace_id: str = "",
) -> str:
    """Intentional forensic INSERT (bypass IdentityService) for removal-safety cases.

    Returns the Entity ``internal_id`` UUID written on the row (never numeric PK).
    """
    if app_id > 0:
        _ensure_game(db, app_id, f"Game_{app_id}")
    entity = str(uuid.uuid4())
    with db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, workspace_id, internal_id, last_known_path,
                folder_present, platform, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'other', datetime('now'))
            """,
            (
                mod_id,
                app_id,
                title,
                workspace_id or str(mod_id),
                entity,
                last_known_path,
            ),
        )
        db._conn.commit()
    return entity


def test_remove_mod_registration_drops_db_keeps_project_files(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete erroneous Mod registration; project root + code files remain."""
    app_root = tmp_path / "steam-mod-manager"
    app_root.mkdir()
    code_file = app_root / "main.py"
    code_file.write_text("# keep me\n", encoding="utf-8")
    (app_root / "core").mkdir()
    (app_root / "core" / "db_manager.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr("core.paths.project_root", lambda: app_root)
    monkeypatch.setattr(
        "services.registration_removal.project_root", lambda: app_root
    )

    library = tmp_path / "library"
    library.mkdir()
    _ensure_game(db, 916440, "Anno 1800")
    # Forensic pollution: registration points at application root (no sidecar).
    _insert_pollution_mod(
        db,
        mod_id=9000000000000344,
        app_id=916440,
        title="仓库运输更快",
        last_known_path=str(app_root),
        workspace_id="17863417826515655",
    )

    # Sentinel test Game (app_id=0 is schema-seeded) + pytest "Game" phantom.
    game_folder = library / "Game" / "ManualNew"
    game_folder.mkdir(parents=True)
    archive_entity = _insert_pollution_mod(
        db,
        mod_id=9000000000000000,
        app_id=0,
        title="ArchiveMod",
        last_known_path=str(game_folder),
        workspace_id="17879973851919389",
    )
    write_info_sidecar(
        game_folder,
        internal_id=archive_entity,
        title="ArchiveMod",
        external_id="17879973851919389",
        workspace_id="17879973851919389",
        app_id=0,
        game_name="Game",
        platform="other",
    )

    # Extra bogus Game row (not the schema sentinel app_id=0).
    _ensure_game(db, 999001, "TestBogusGame")

    fs_calls: list[tuple[str, tuple[Any, ...]]] = []

    def _trap_rmtree(*args: Any, **kwargs: Any) -> None:
        fs_calls.append(("rmtree", args))
        raise AssertionError("shutil.rmtree must not be called")

    def _trap_remove(*args: Any, **kwargs: Any) -> None:
        fs_calls.append(("remove", args))
        raise AssertionError("os.remove must not be called")

    monkeypatch.setattr(shutil, "rmtree", _trap_rmtree)
    monkeypatch.setattr("os.remove", _trap_remove)

    service = ModRegistrationRemovalService(db=db)

    mod_result = service.remove_mod_registration(9000000000000344)
    assert mod_result.success
    assert mod_result.db_removed
    assert mod_result.filesystem_touched is False
    assert mod_result.path_was_application_root is True
    assert db.get_mod_backup_row("9000000000000344") is None

    game_mod = service.remove_mod_registration(9000000000000000)
    assert game_mod.success

    game_result = service.remove_game_registration(999001, detach_mods=False)
    assert game_result.success
    assert game_result.filesystem_touched is False
    assert db.get_game(999001) is None

    # Filesystem intact
    assert app_root.is_dir()
    assert code_file.is_file()
    assert code_file.read_text(encoding="utf-8") == "# keep me\n"
    assert (app_root / "core" / "db_manager.py").is_file()
    assert game_folder.is_dir()
    assert fs_calls == []

    snap = build_library_snapshot(library)
    folders = {g.folder.casefold() for g in snap.games}
    assert "project" not in folders
    assert "game" not in folders
    assert "testbogusgame" not in folders
    assert all(c.id != "9000000000000344" for c in snap.cards)


def test_removal_service_never_imports_mod_remover(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard gate: registration removal must not reach ModRemover / rmtree."""
    library = tmp_path / "mod"
    managed = library / "Anno 1800" / "SafeMod"
    managed.mkdir(parents=True)
    (managed / "payload.txt").write_text("safe", encoding="utf-8")
    entity = _insert_pollution_mod(
        db,
        mod_id=42,
        app_id=916440,
        title="SafeMod",
        last_known_path=str(managed),
    )
    write_info_sidecar(
        managed,
        internal_id=entity,
        title="SafeMod",
        external_id="42",
        workspace_id="42",
        app_id=916440,
        game_name="Anno 1800",
        platform="other",
    )

    called = {"remover": False}

    class _Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            called["remover"] = True
            raise AssertionError("ModRemover must not be constructed")

    monkeypatch.setattr("services.mod_remove.ModRemover", _Boom)
    monkeypatch.setattr(
        shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rmtree"))
    )

    result = ModRegistrationRemovalService(db=db).remove_mod_registration(42)
    assert result.success
    assert called["remover"] is False
    assert managed.is_dir()
    assert (managed / "payload.txt").read_text(encoding="utf-8") == "safe"


def test_project_root_rejected_as_mod_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    lib = app / "mod"
    data = app / "data"
    app.mkdir()
    lib.mkdir()
    data.mkdir()
    monkeypatch.setattr("core.paths.project_root", lambda: app)
    monkeypatch.setattr("core.paths.default_mod_library", lambda: lib)
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.mod_path_validation.project_root", lambda: app)
    monkeypatch.setattr("services.mod_path_validation.default_mod_library", lambda: lib)
    monkeypatch.setattr("services.mod_path_validation.data_dir", lambda: data)

    assert is_forbidden_mod_root(app)
    assert is_forbidden_mod_root(lib)
    assert is_forbidden_mod_root(data)
    assert discover_mod_directories(app) == []

    with pytest.raises(InvalidModRootError) as exc:
        validate_import_source_root(app)
    assert FORBIDDEN_MOD_ROOT_REASON in str(exc.value)

    with pytest.raises(InvalidModRootError):
        validate_managed_mod_path(app)

    # Valid managed shape still accepted
    good = lib / "Anno 1800" / "Warehouse"
    good.mkdir(parents=True)
    assert validate_managed_mod_path(good, library_root=lib) == good.resolve()


def test_commit_path_change_rejects_application_root(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "steam-mod-manager"
    lib = app / "mod"
    app.mkdir()
    lib.mkdir()
    monkeypatch.setattr("core.paths.project_root", lambda: app)
    monkeypatch.setattr("core.paths.default_mod_library", lambda: lib)
    monkeypatch.setattr("core.paths.data_dir", lambda: app / "data")
    (app / "data").mkdir()
    monkeypatch.setattr("services.mod_path_validation.project_root", lambda: app)
    monkeypatch.setattr("services.mod_path_validation.default_mod_library", lambda: lib)
    monkeypatch.setattr("services.mod_path_validation.data_dir", lambda: app / "data")

    managed = lib / "G" / "X"
    managed.mkdir(parents=True)
    entity = _insert_pollution_mod(
        db,
        mod_id=7,
        app_id=1,
        title="X",
        last_known_path=str(managed),
    )
    write_info_sidecar(
        managed,
        internal_id=entity,
        title="X",
        external_id="7",
        workspace_id="7",
        app_id=1,
        game_name="G",
        platform="other",
    )

    result = commit_path_change(7, old_path=managed, new_path=app, db=db)
    assert result.success is False
    assert "Mod root" in result.error or "工程目录" in result.error
    row = db.get_mod_backup_row("7") or {}
    # Path must remain the managed folder — not rebound to application root.
    assert Path(str(row.get("last_known_path") or "")).resolve() == managed.resolve()


def test_real_project_root_still_exists_after_path_helpers() -> None:
    """Sanity: production project root and main.py remain on disk."""
    root = project_root()
    assert root.is_dir()
    assert (root / "main.py").is_file()
    assert (root / "core" / "db_manager.py").is_file()
