"""Library Projection final audit — identity key + mutation rebind contract.

Rules
-----
- Sole Projection / viewport entity key: internal_id (ModCardData.id)
- workspace_id is display-only (same digits may appear on two entities)
- Path rebind updates Projection.managed_path without inventing identity
- Mutation → DB commit → notify_mod_changed → refresh_projection → rebind
  (never full Library snapshot rebuild)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library, take_projection_touch_ids
from services.mod_library_cache import (
    get_library_cache,
    reset_library_cache,
)
from services.mod_projection_events import (
    notify_mod_changed,
    reset_mod_changed_listeners,
    subscribe_mod_changed,
)
from ui.library_view import ModLibraryView


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    take_projection_touch_ids()  # drain any leftover
    manager = DatabaseManager.instance(tmp_path / "proj_audit.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    take_projection_touch_ids()


def _seed(
    library: Path,
    db: DatabaseManager,
    mid: str,
    *,
    title: str,
    game: str = "AuditGame",
    app_id: int = 880099,
    workspace_id: str = "",
) -> tuple[Path, str]:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    folder = library / game / f"{title}_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game
    )
    pk = str(created.mod_id)
    entity_uuid = str(created.internal_id or "")
    payload = {
        "published_file_id": mid,
        "title": title,
        "app_id": app_id,
        "game_name": game,
        "internal_id": entity_uuid,
    }
    if workspace_id:
        payload["workspace_id"] = workspace_id
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (folder / "mod.pak").write_bytes(b"payload")
    write_info_sidecar(
        folder,
        internal_id=entity_uuid,
        title=title,
        external_id=mid,
        workspace_id=workspace_id or mid,
        app_id=app_id,
        game_name=game,
    )
    bind_managed_path(db, pk, folder, title=title)

    fields: dict = {
        "folder_present": True,
        "last_known_path": str(folder),
        "app_id": app_id,
    }
    if workspace_id:
        fields["workspace_id"] = workspace_id
    db.update_mod_identity_fields(pk, **fields)
    return folder, pk


def test_same_workspace_id_yields_two_independent_projections(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    shared = "1333"
    ws_a = "882001"
    ws_b = "882002"
    _folder_a, pk_a = _seed(library, db, ws_a, title="ModA", workspace_id=shared)
    _folder_b, pk_b = _seed(library, db, ws_b, title="ModB", workspace_id=shared)

    db._conn.execute(
        "UPDATE mods SET workspace_id = ? WHERE mod_id IN (?, ?)",
        (shared, int(pk_a), int(pk_b)),
    )
    db._conn.commit()

    cache = get_library_cache()
    snap = cache.load_snapshot(library, force=True)
    ids = sorted(c.id for c in snap.cards)
    assert ids == sorted([pk_a, pk_b])
    by_id = {c.id: c for c in snap.cards}
    assert by_id[pk_a].workspace_id == shared
    assert by_id[pk_b].workspace_id == shared
    assert by_id[pk_a].title != by_id[pk_b].title
    assert cache.get_card_data(pk_a) is not cache.get_card_data(pk_b)
    assert ModLibraryView._card_cache_key(Path("x"), mod_id=pk_a) != (
        ModLibraryView._card_cache_key(Path("x"), mod_id=pk_b)
    )
    assert ModLibraryView._card_cache_key(Path("x"), mod_id=pk_a) == f"entity:{pk_a}"


def test_path_rebind_updates_projection_managed_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    workshop = "882010"
    old, pk = _seed(library, db, workshop, title="MoveMe")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert Path(cache.get_card_data(pk).managed_path) == old

    new = library / "AuditGame" / "MoveMe_reborn"
    shutil.move(str(old), str(new))
    info = new / INFO_DIR_NAME / METADATA_FILENAME
    payload = json.loads(info.read_text(encoding="utf-8"))
    payload["internal_id"] = payload.get("internal_id") or pk
    info.write_text(json.dumps(payload), encoding="utf-8")

    result = reconcile_library(library)
    assert pk in result.rebound_ids or any(
        pk in n for n in result.notes if "PATH_REBOUND" in n or "renamed" in n.lower()
    )
    card = cache.get_card_data(pk)
    assert card is not None
    assert Path(card.managed_path).resolve() == new.resolve()
    assert not old.exists()


def test_mutation_notify_rebinds_without_full_library_refresh(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    workshop = "882020"
    _folder, pk = _seed(library, db, workshop, title="Mut")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)

    import services.mod_library_cache as mlc

    builds: list[bool] = []
    real_build = mlc.build_library_snapshot

    def tracked(root):
        builds.append(True)
        return real_build(root)

    monkeypatch.setattr(mlc, "build_library_snapshot", tracked)

    seen: list[str] = []
    subscribe_mod_changed(lambda m: seen.append(str(m)))

    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "MutatedName",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
        },
    )
    notify_mod_changed(pk)

    db.update_mod_cover_path(pk, f"{INFO_DIR_NAME}/cover.png")
    notify_mod_changed(pk)

    db.update_mod_deploy_status(pk, deploy_status=DEPLOY_STATUS_DEPLOYED)
    notify_mod_changed(pk)

    assert builds == []
    assert seen == [pk, pk, pk]
    card = cache.get_card_data(pk)
    assert card is not None
    assert card.title == "MutatedName"
    assert card.cover == f"{INFO_DIR_NAME}/cover.png"
    assert card.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert card.deployed is True
    # Warm snapshot object preserved (single-row replace, not rebuild).
    assert cache.peek_snapshot(library) is not None


def test_relocation_workflow_removed() -> None:
    """Architecture guard: relocate UI / handlers must not remain callable."""
    root = Path(__file__).resolve().parents[1]
    lib = (root / "ui" / "library_view.py").read_text(encoding="utf-8")
    panel = (root / "ui" / "mod_detail_panel.py").read_text(encoding="utf-8")
    assert "relocate_completed" not in lib
    assert "_on_relocate_completed" not in lib
    assert "_focus_mod_after_relocate" not in lib
    assert "btn_relocate" not in panel
    assert "_relocate_mod_folder" not in panel
    assert "重新定位目录" not in panel
    assert not (root / "services" / "mod_relocate.py").is_file()


def test_card_cache_key_is_entity_internal_id_only() -> None:
    assert (
        ModLibraryView._card_cache_key(Path(r"D:\old\folder"), mod_id="99")
        == "entity:99"
    )
    assert (
        ModLibraryView._card_cache_key(Path(r"D:\new\folder"), mod_id="99")
        == "entity:99"
    )
    assert ModLibraryView._card_cache_key(Path("x"), mod_id="") == ""
