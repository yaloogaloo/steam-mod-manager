"""Library Projection lifecycle — Mutation → refresh_projection → Card.

Contract
--------
After DB commit, ``notify_mod_changed(mod_id)`` re-reads the full Layer-1 row
into warm ``ModCardData``. No field-level notify. No full Library refresh.
Card paint reads Projection only.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.mod_platform import OFFLINE_STATUS_ARCHIVED
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import (
    get_library_cache,
    reset_library_cache,
)
from services.mod_projection_events import (
    notify_mod_changed,
    reset_mod_changed_listeners,
)
from tests.helpers.identity import bind_managed_path, create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    manager = DatabaseManager.instance(tmp_path / "proj_life.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()


def _seed(
    library: Path,
    db: DatabaseManager,
    mid: str,
    *,
    title: str = "LifeMod",
    game: str = "LifeGame",
    app_id: int = 880021,
) -> Path:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    folder = library / game / f"{title}_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": mid,
                "published_file_id": mid,
                "title": title,
                "app_id": app_id,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    (folder / "mod.pak").write_bytes(b"payload")
    create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game
    )
    bind_managed_path(db, mid, folder, game_name=game, title=title)
    return folder


def _assert_aligned(mid: str, **expect) -> None:
    db = DatabaseManager.instance()
    row = db.list_mod_list_items(mod_id=mid)[0]
    cache = get_library_cache()
    card = cache.get_card_data(mid)
    assert card is not None
    item = None
    snap = cache._snapshot
    assert snap is not None
    for candidate in snap.list_items or []:
        if str(candidate.internal_id) == mid:
            item = candidate
            break
    assert item is not None

    if "name" in expect:
        assert row["name"] == expect["name"] == item.name == card.title
    if "cover" in expect:
        assert row["cover_path"] == expect["cover"] == item.cover_path == card.cover
    if "deploy_status" in expect:
        assert row["deploy_status"] == expect["deploy_status"]
        assert item.deploy_status == expect["deploy_status"]
        assert card.deploy_status == expect["deploy_status"]
    if "has_offline" in expect:
        assert bool(row["has_offline"]) is expect["has_offline"]
        assert item.has_offline is expect["has_offline"]
        assert card.has_offline is expect["has_offline"]


def test_name_change_updates_projection_immediately(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "881001"
    _seed(library, db, mid, title="OldLife")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert cache.get_card_data(mid).title == "OldLife"

    db.update_mod_user_metadata(
        mid,
        {
            "display_name": "NewLifeName",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
        },
    )
    notify_mod_changed(mid)
    _assert_aligned(mid, name="NewLifeName")


def test_cover_change_updates_projection_immediately(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "881002"
    folder = _seed(library, db, mid)
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)

    rel = f"{INFO_DIR_NAME}/cover.jpg"
    (folder / INFO_DIR_NAME / "cover.jpg").write_bytes(b"\xff\xd8\xfffake")
    db.update_mod_cover_path(mid, rel)
    notify_mod_changed(mid)
    _assert_aligned(mid, cover=rel)


def test_offline_generation_keeps_badge_projection(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """After offline archive, projection keeps has_offline; card hides attention badge."""
    library = tmp_path / "mod"
    mid = "881003"
    folder = _seed(library, db, mid)
    offline = folder / INFO_DIR_NAME / "offline.html"
    offline.write_text("<html>ok</html>", encoding="utf-8")
    db.update_mod_offline_status(
        mid, status=OFFLINE_STATUS_ARCHIVED, provider="steam"
    )
    db._conn.execute(
        "UPDATE mods SET backup_offline_path = ? WHERE mod_id = ?",
        (str(offline), int(mid)),
    )
    db._conn.commit()

    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert cache.get_card_data(mid).has_offline is True

    db.update_mod_user_metadata(
        mid,
        {
            "display_name": "KeepOffline",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
        },
    )
    notify_mod_changed(mid)
    card = cache.get_card_data(mid)
    assert card is not None
    assert card.title == "KeepOffline"
    assert card.has_offline is True
    assert card.offline_status == OFFLINE_STATUS_ARCHIVED
    _assert_aligned(mid, name="KeepOffline", has_offline=True)


def test_deploy_change_updates_projection(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "881004"
    _seed(library, db, mid)
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert cache.get_card_data(mid).deploy_status == DEPLOY_STATUS_NOT_DEPLOYED

    db.update_mod_deploy_status(mid, deploy_status=DEPLOY_STATUS_DEPLOYED)
    notify_mod_changed(mid)
    _assert_aligned(mid, deploy_status=DEPLOY_STATUS_DEPLOYED)
    assert cache.get_card_data(mid).deployed is True


def test_game_reenter_keeps_projection(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "881005"
    _seed(library, db, mid, title="Before")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)

    db.update_mod_user_metadata(
        mid,
        {
            "display_name": "AfterReenter",
            "custom_description": "",
            "user_notes": "",
            "favorite": True,
        },
    )
    notify_mod_changed(mid)

    again = cache.load_snapshot(library, force=False)
    card = next(c for c in again.cards if c.id == mid)
    assert card.title == "AfterReenter"
    assert card.favorite is True


def test_mutation_forbids_full_library_refresh(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mid = "881006"
    _seed(library, db, mid)
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)

    import services.mod_library_cache as mlc

    calls: list[bool] = []
    real = mlc.build_library_snapshot

    def tracked(root):
        calls.append(True)
        return real(root)

    monkeypatch.setattr(mlc, "build_library_snapshot", tracked)

    db.update_mod_cover_path(mid, f"{INFO_DIR_NAME}/c.png")
    notify_mod_changed(mid)
    db.update_mod_deploy_status(mid, deploy_status=DEPLOY_STATUS_DEPLOYED)
    notify_mod_changed(mid)

    assert calls == []
    assert cache.peek_snapshot(library) is not None


def test_card_has_no_business_inference_for_display() -> None:
    """Architecture guard: ModCard display paths must not call live DB / offline FS."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "ui" / "mod_card.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {
        "get_db",
        "get_mod_display_info",
        "get_mod_deploy_info",
        "get_category_tags",
        "get_relationship_counts",
        "resolve_offline_page",
        "resolve_cover_path",
        "is_missing_mod_content",
        "read_is_missing_content",
        "is_mod_folder_absent",
    }
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in forbidden_calls:
                hits.append(name)
    assert hits == [], f"ModCard still infers display via {hits}"


def test_library_view_does_not_import_offline_service_for_display() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "services.offline" not in source
    assert "from services.offline" not in source
