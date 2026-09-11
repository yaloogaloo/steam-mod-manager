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
from tests.helpers.identity import bind_managed_path, create_steam_test_mod

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
) -> Path:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    folder = library / game / f"{title}_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": mid,
        "title": title,
        "app_id": app_id,
        "game_name": game,
        "internal_id": mid,
    }
    if workspace_id:
        payload["workspace_id"] = workspace_id
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (folder / "mod.pak").write_bytes(b"payload")
    create_steam_test_mod(db, external_id=mid, title=title, app_id=app_id, game_name=game)
    bind_managed_path(db, mid, folder, title=title)

    fields: dict = {
        "folder_present": True,
        "last_known_path": str(folder),
        "app_id": app_id,
        "internal_id": mid,
    }
    if workspace_id:
        fields["workspace_id"] = workspace_id
    db.update_mod_identity_fields(mid, **fields)
    return folder


def test_same_workspace_id_yields_two_independent_projections(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    shared = "1333"
    a = "882001"
    b = "882002"
    _seed(library, db, a, title="ModA", workspace_id=shared)
    _seed(library, db, b, title="ModB", workspace_id=shared)

    # Pollution case: same digits on workspace_id must not collapse entities.
    db._conn.execute(
        "UPDATE mods SET workspace_id = ? WHERE mod_id IN (?, ?)",
        (shared, int(a), int(b)),
    )
    db._conn.commit()

    cache = get_library_cache()
    snap = cache.load_snapshot(library, force=True)
    ids = sorted(c.id for c in snap.cards)
    assert ids == [a, b]
    by_id = {c.id: c for c in snap.cards}
    assert by_id[a].workspace_id == shared
    assert by_id[b].workspace_id == shared
    assert by_id[a].title != by_id[b].title
    assert cache.get_card_data(a) is not cache.get_card_data(b)
    assert ModLibraryView._card_cache_key(Path("x"), mod_id=a) != (
        ModLibraryView._card_cache_key(Path("x"), mod_id=b)
    )
    assert ModLibraryView._card_cache_key(Path("x"), mod_id=a) == f"entity:{a}"


def test_path_rebind_updates_projection_managed_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "882010"
    old = _seed(library, db, mid, title="MoveMe")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert Path(cache.get_card_data(mid).managed_path) == old

    new = library / "AuditGame" / "MoveMe_reborn"
    shutil.move(str(old), str(new))
    info = new / INFO_DIR_NAME / METADATA_FILENAME
    payload = json.loads(info.read_text(encoding="utf-8"))
    payload["internal_id"] = mid
    info.write_text(json.dumps(payload), encoding="utf-8")

    result = reconcile_library(library)
    assert mid in result.rebound_ids or any(
        mid in n for n in result.notes if "PATH_REBOUND" in n or "renamed" in n.lower()
    )
    # Projection must bind the new path (via refresh_projection after rebind).
    card = cache.get_card_data(mid)
    assert card is not None
    assert Path(card.managed_path).resolve() == new.resolve()
    assert not old.exists()


def test_mutation_notify_rebinds_without_full_library_refresh(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mid = "882020"
    _seed(library, db, mid, title="Mut")
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
        mid,
        {
            "display_name": "MutatedName",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
        },
    )
    notify_mod_changed(mid)

    db.update_mod_cover_path(mid, f"{INFO_DIR_NAME}/cover.png")
    notify_mod_changed(mid)

    db.update_mod_deploy_status(mid, deploy_status=DEPLOY_STATUS_DEPLOYED)
    notify_mod_changed(mid)

    assert builds == []
    assert seen == [mid, mid, mid]
    card = cache.get_card_data(mid)
    assert card is not None
    assert card.title == "MutatedName"
    assert card.cover == f"{INFO_DIR_NAME}/cover.png"
    assert card.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert card.deployed is True
    # Warm snapshot object preserved (single-row replace, not rebuild).
    assert cache.peek_snapshot(library) is not None


def test_relocate_completed_forbids_full_refresh() -> None:
    """Architecture guard: relocate sink must notify, never LibraryView.refresh()."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "ui" / "library_view.py").read_text(encoding="utf-8")
    part = src.split("def _on_relocate_completed", 1)[1].split(
        "def _focus_mod_after_relocate", 1
    )[0]
    assert "notify_mod_changed" in part
    assert "self.refresh()" not in part
    assert "self.refresh(" not in part


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
