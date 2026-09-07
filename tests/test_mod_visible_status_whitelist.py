"""Architecture: Mod user-visible status whitelist — no identity leak.

User-visible Mod status sources ONLY::

  - conflict_status   (user annotation)
  - abandoned         (user annotation)
  - invalid           (user annotation)
  - content_status    (content_status_eval → healthy|content_missing)

Forbidden in UI status display::

  - identity_status / identity_conflict / unresolved
  - backup_status
  - offline_status
  - library_status

Deploy overlays (memory only): 记录缺失 / 额外部署 — not DB Mod status columns.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    identity_status_badge_label,
)
from services.status_authority import IDENTITY_STATUS_CONFLICT
from ui.library_query import (
    FILTER_CONFLICT,
    ModFilterIndex,
    matches_status_filter,
)
from ui.mod_card import ModCardWidget

ROOT = Path(__file__).resolve().parents[1]

USER_STATUS_SOURCES = frozenset(
    {
        "conflict_status",
        "abandoned",
        "invalid",
        "is_invalid",
        "content_status",
    }
)

FORBIDDEN_UI_STATUS_SOURCES = frozenset(
    {
        "identity_status",
        "backup_status",
        "offline_status",
        "library_status",
    }
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "status_whitelist.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _idx(**kwargs) -> ModFilterIndex:
    base = dict(
        internal_id="1",
        display_name="A",
        steam_name="A",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="A",
        content_status=CONTENT_HEALTHY,
        identity_status="ok",
        conflict_status="none",
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def test_identity_badge_labels_are_empty() -> None:
    assert identity_status_badge_label(IDENTITY_STATUS_CONFLICT) == ""
    assert identity_status_badge_label("unresolved") == ""
    assert identity_status_badge_label("ok") == ""


def test_identity_filter_never_matches() -> None:
    idx = _idx(identity_status=IDENTITY_STATUS_CONFLICT)
    assert matches_status_filter(idx, "identity_conflict") is False
    assert matches_status_filter(idx, FILTER_CONFLICT) is False


def test_user_conflict_filter_ignores_identity() -> None:
    assert matches_status_filter(
        _idx(identity_status=IDENTITY_STATUS_CONFLICT, conflict_status="none"),
        FILTER_CONFLICT,
    ) is False
    assert matches_status_filter(
        _idx(identity_status="ok", conflict_status="conflict"),
        FILTER_CONFLICT,
    ) is True


def test_card_does_not_render_identity_as_status(
    qapp: QApplication, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    folder = tmp_path / "Game" / "Mod"
    folder.mkdir(parents=True)
    data = SimpleNamespace(
        id="91001",
        folder_absent=False,
        missing_content=False,
        content_status=CONTENT_HEALTHY,
        identity_status=IDENTITY_STATUS_CONFLICT,
        library_status="conflict",
        backup_status="invalid",
        offline_status="archived",
        cover="",
        source_type="steam",
        steam_name="Mod",
        display_name="Mod",
        title="Mod",
        json_display_name="Mod",
        favorite=False,
        abandoned=False,
        deploy_status="",
        platform="steam",
        conflict=False,
        conflict_status="none",
        invalid=False,
        is_invalid=False,
        enabled=True,
        category_tags="",
        relation_conflicts=0,
        relation_deps=0,
        has_offline=True,
    )
    meta = ModMetadata(
        published_file_id="91001", title="Mod", managed_path=str(folder)
    )
    card = ModCardWidget(folder, meta, card_data=data)
    card.refresh_display()
    qapp.processEvents()
    text = (card.missing_badge.text() or "") + (card.state_badge.text() or "")
    assert "身份异常" not in text
    assert "身份" not in text
    assert card.missing_badge.isHidden()
    # Offline attention badge stays hidden when projection has offline page.
    assert card.offline_badge.isHidden()


def test_card_renders_content_missing_only(
    qapp: QApplication, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    folder = tmp_path / "Game" / "Mod"
    folder.mkdir(parents=True)
    data = SimpleNamespace(
        id="91002",
        folder_absent=False,
        missing_content=True,
        content_status=CONTENT_CONTENT_MISSING,
        identity_status=IDENTITY_STATUS_CONFLICT,
        library_status="missing",
        cover="",
        source_type="steam",
        steam_name="Mod",
        display_name="Mod",
        title="Mod",
        json_display_name="Mod",
        favorite=False,
        abandoned=False,
        offline_status="",
        deploy_status="",
        platform="steam",
        conflict=False,
        conflict_status="none",
        invalid=False,
        is_invalid=False,
        enabled=True,
        category_tags="",
        relation_conflicts=0,
        relation_deps=0,
        has_offline=False,
    )
    meta = ModMetadata(
        published_file_id="91002", title="Mod", managed_path=str(folder)
    )
    card = ModCardWidget(folder, meta, card_data=data)
    card.refresh_display()
    qapp.processEvents()
    assert not card.missing_badge.isHidden()
    assert "内容缺失" in (card.missing_badge.text() or "")
    assert "身份" not in (card.missing_badge.text() or "")


def test_repository_status_badge_never_identity(db: DatabaseManager) -> None:
    db.update_game_deploy_config(1, name="Game")
    db.upsert_mod(ModMetadata(published_file_id="92001", title="X", app_id=1))
    db._conn.execute(
        """
        UPDATE mods SET
            content_status = 'healthy',
            identity_status = 'identity_conflict',
            library_status = 'conflict',
            folder_present = 1,
            last_known_path = ?
        WHERE mod_id = 92001
        """,
        (str(Path("G") / "X"),),
    )
    db._conn.commit()
    row = db.list_mod_list_items(game_id=1)[0]
    assert row["status_badge"] != "identity_conflict"
    assert "identity" not in str(row["status_badge"] or "").lower()
    assert row["content_status"] == CONTENT_HEALTHY


def test_historical_cleanup_clears_user_status_pollution(
    tmp_path: Path,
) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(tmp_path / "cleanup.db")
    try:
        db.update_game_deploy_config(1, name="Game")
        db.upsert_mod(ModMetadata(published_file_id="93001", title="P", app_id=1))
        # Force pollution as if an old build wrote it after schema init.
        db._conn.execute(
            "DELETE FROM schema_flags WHERE flag = ?",
            ("cleared_identity_user_status_v1",),
        )
        db._conn.execute(
            """
            UPDATE mods SET
                content_status = 'identity_conflict',
                library_status = 'conflict',
                identity_status = 'identity_conflict',
                conflict_status = 'none'
            WHERE mod_id = 93001
            """
        )
        db._conn.commit()
        db._clear_identity_pollution_from_user_status()
        row = db._conn.execute(
            "SELECT content_status, library_status, identity_status, conflict_status "
            "FROM mods WHERE mod_id = 93001"
        ).fetchone()
        assert str(row["content_status"]) == "healthy"
        assert str(row["library_status"]) == "normal"
        # identity_status column may remain (internal) — not mass-reset by this cleanup
        assert str(row["conflict_status"]) in {"", "none"}
    finally:
        db.close()
        DatabaseManager.reset_instance()


def test_mod_card_source_forbids_identity_status_badge_calls() -> None:
    """Static: ModCard must not call identity badge label helpers."""
    src = (ROOT / "ui" / "mod_card.py").read_text(encoding="utf-8")
    assert "identity_status_badge_label" not in src
    assert "身份异常" not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name != "identity_status_badge_label"
            assert name != "identity_status_badge_tip"


def test_list_mod_list_items_source_forbids_identity_status_badge() -> None:
    src = inspect.getsource(DatabaseManager.list_mod_list_items)
    assert 'status_badge = identity_status' not in src
    assert 'if identity_status == "identity_conflict"' not in src


def test_game_status_source_forbids_identity_anomaly_copy() -> None:
    src = (ROOT / "services" / "game_status.py").read_text(encoding="utf-8")
    assert "身份异常" not in src
    assert "IDENTITY_STATUS_CONFLICT" not in src
