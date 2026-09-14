"""Unified metadata resolver priority: .info > backup > SQLite."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, reconcile_folder_presence, sync_metadata_backup
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder, write_info_sidecar
from services.mod_metadata_resolver import (
    ModMetadataResolver,
    resolve_cover_path,
    resolve_mod_metadata,
    resolve_offline_page,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "resolver.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _write_backup_files(
    mod_id: str,
    payload: dict,
    *,
    cover: bool = False,
    offline: bool = False,
) -> Path:
    dest = backup_root(mod_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if cover:
        (dest / "cover.jpg").write_bytes(b"cover-bytes")
    if offline:
        off = dest / "offline"
        off.mkdir(exist_ok=True)
        (off / "index.html").write_text("<html>backup</html>", encoding="utf-8")
    return dest


def _seed_folder(
    db: DatabaseManager,
    folder: Path,
    *,
    workshop: str,
    title: str,
    extra: dict | None = None,
) -> tuple[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(db, external_id=workshop, title=title)
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title=title,
        extra=extra or {},
    )
    return pk, frozen


def test_existing_folder_prefers_info_over_backup_and_sqlite(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModA"
    pk, frozen = _seed_folder(
        db,
        folder,
        workshop="910101",
        title="A",
        extra={"display_name": "A", "description": "info-desc"},
    )

    db.update_mod_user_metadata(pk, {"display_name": "C"})
    _write_backup_files(
        pk,
        {
            "internal_id": frozen,
            "published_file_id": "910101",
            "title": "B",
            "display_name": "B",
        },
    )

    resolved = resolve_mod_metadata(pk, folder)
    assert resolved is not None
    assert resolved.display_name == "A"
    assert resolved.folder_present is True


def test_missing_folder_prefers_backup_over_sqlite(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModB"
    pk, frozen = _seed_folder(
        db,
        folder,
        workshop="910102",
        title="FromInfo",
        extra={"display_name": "FromInfo"},
    )

    db.update_mod_user_metadata(pk, {"display_name": "C"})
    sync_metadata_backup(folder)
    _write_backup_files(
        pk,
        {
            "internal_id": frozen,
            "published_file_id": "910102",
            "title": "B",
            "display_name": "B",
            "description": "backup-desc",
        },
    )
    shutil.rmtree(folder)
    db.set_mod_folder_present(pk, present=False)

    resolved = resolve_mod_metadata(pk, folder)
    assert resolved is not None
    assert resolved.folder_present is False
    assert resolved.display_name == "B"
    assert resolved.description == "backup-desc"


def test_restored_folder_info_wins_without_resolver_write(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """Resolver prefers .info and must not rewrite backup (Phase 3-B)."""
    folder = tmp_path / "mod" / "Game" / "ModC"
    pk, frozen = _seed_folder(
        db,
        folder,
        workshop="910103",
        title="A",
        extra={"display_name": "A"},
    )

    sync_metadata_backup(folder)
    _write_backup_files(
        pk,
        {
            "internal_id": frozen,
            "published_file_id": "910103",
            "title": "B",
            "display_name": "B",
        },
    )

    resolved = resolve_mod_metadata(pk, folder)
    assert resolved is not None
    assert resolved.display_name == "A"
    saved = json.loads(
        (backup_root(pk) / "metadata.json").read_text(encoding="utf-8")
    )
    # Pure-read: polluted backup remains until an explicit write-path sync.
    assert saved.get("title") == "B" or saved.get("display_name") == "B"

    from services.metadata_backup_sync import sync_after_metadata_change

    sync_after_metadata_change(pk, folder, "repair")
    saved2 = json.loads(
        (backup_root(pk) / "metadata.json").read_text(encoding="utf-8")
    )
    assert saved2.get("title") == "A" or saved2.get("display_name") == "A"


def test_missing_folder_uses_backup_cover_not_sqlite_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModD"
    pk, frozen = _seed_folder(
        db,
        folder,
        workshop="910104",
        title="CoverMod",
        extra={"display_name": "CoverMod", "cover_path": ".info/cover.jpg"},
    )
    (folder / INFO_DIR_NAME / "cover.jpg").write_bytes(b"info-cover")

    db.update_mod_cover_path(pk, str(folder / INFO_DIR_NAME / "cover.jpg"))
    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    db.set_mod_folder_present(pk, present=False)

    cover = resolve_cover_path(pk, folder)
    assert cover is not None
    assert cover.is_file()
    assert "mod_backup" in str(cover).replace("\\", "/")


def test_missing_folder_opens_backup_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModE"
    info = folder / INFO_DIR_NAME / "offline"
    info.mkdir(parents=True)
    created = create_steam_test_mod(db, external_id="910105", title="OffMod")
    pk = str(created.mod_id)
    write_info_sidecar(
        folder,
        internal_id=str(created.internal_id),
        title="OffMod",
        external_id="910105",
        workspace_id=str(created.workspace_id or "910105"),
        extra={"display_name": "OffMod"},
    )
    from tests.helpers.identity import bind_managed_path

    bind_managed_path(db, pk, folder, title="OffMod")
    (info / "index.html").write_text("<html>info</html>", encoding="utf-8")
    from core.paths import asset_store_dir
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas

    assert finalize_live_offline_to_cas(
        folder, store=AssetStore(root=asset_store_dir())
    ).ok

    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    db.set_mod_folder_present(pk, present=False)

    page = resolve_offline_page(pk, folder)
    assert page is not None
    assert page.is_file()
    assert "offline_view" in str(page).replace("\\", "/")
    assert "mod_backup" not in str(page).replace("\\", "/")
    assert page.read_text(encoding="utf-8") == "<html>info</html>"


def test_resolver_does_not_read_missing_info_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "Gone"
    created = create_steam_test_mod(db, external_id="910106", title="C")
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    from tests.helpers.identity import bind_managed_path

    bind_managed_path(db, pk, folder, title="C")

    db.update_mod_user_metadata(pk, {"display_name": "C"})
    _write_backup_files(
        pk,
        {
            "internal_id": frozen,
            "published_file_id": "910106",
            "title": "B",
            "display_name": "B",
        },
        cover=True,
        offline=True,
    )
    db.update_mod_backup_snapshot(
        pk,
        last_known_path=str(folder),
        folder_present=False,
        backup_metadata_json=json.dumps(
            {
                "internal_id": frozen,
                "published_file_id": "910106",
                "title": "B",
                "display_name": "B",
            }
        ),
        backup_cover_path=str(backup_root(pk) / "cover.jpg"),
        backup_offline_path=str(backup_root(pk) / "offline" / "index.html"),
    )
    assert not folder.exists()
    resolved = ModMetadataResolver().resolve_missing_folder(pk, folder)
    assert resolved is not None
    assert resolved.display_name == "B"
    assert resolved.cover_path
    assert Path(resolved.cover_path).is_file()
    assert resolved.offline_path
    assert Path(resolved.offline_path).is_file()


def test_reconcile_marks_deleted_folder_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModF"
    pk, _frozen = _seed_folder(
        db,
        folder,
        workshop="910107",
        title="F",
        extra={"display_name": "F"},
    )

    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    reconcile_folder_presence(tmp_path / "mod")
    row = db.get_mod_backup_row(pk)
    assert row is not None
    assert int(row["folder_present"]) == 0
