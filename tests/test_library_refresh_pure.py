"""Library refresh must stay UI-thread safe: read + render only."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from services.file_ops import INFO_DIR_NAME
from ui.library_view import ModLibraryView
from ui.mod_card import OFFLINE_MISSING_LABEL, ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_refresh_source_has_no_side_effect_calls() -> None:
    """Guard: refresh() body must not call archive / migrate helpers."""
    src = inspect.getsource(ModLibraryView.refresh)
    forbidden = (
        "backfill_offline_pages",
        "migrate_numeric_mod_folders",
        "OfflinePageArchiver",
        "services.archive",
    )
    for name in forbidden:
        assert name not in src, f"refresh() must not reference {name}"


def test_render_mod_cards_does_not_call_apply_sidecar_to_db() -> None:
    """Library render path is read-only — no per-mod DB sidecar sync."""
    src = inspect.getsource(ModLibraryView._render_mod_cards)
    assert "apply_sidecar_to_db" not in src
    assert "load_info_sidecar" in src  # read-only overlay still allowed


def test_refresh_does_not_invoke_apply_sidecar_to_db(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from core.db_manager import DatabaseManager
    from core.models import ModMetadata
    from services.file_ops import METADATA_FILENAME

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "refresh_sidecar.db")
    lib = tmp_path / "library"
    folder = lib / "Game" / "ModA"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "91001",
                "title": "ModA",
                "game_name": "Game",
            }
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="91001",
            title="ModA",
            managed_path=str(folder),
            game_name="Game",
        )
    )

    calls: list[str] = []

    def tracking_apply(*_a, **_k):
        calls.append("apply")
        return True

    monkeypatch.setattr(
        "services.info_sidecar.apply_sidecar_to_db", tracking_apply
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    assert calls == []
    assert len(view._cards) == 1

    # Game switch must also stay read-only.
    view._set_current_game_context("Game")
    from services.file_ops import ModFileManager

    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert calls == []

    DatabaseManager.reset_instance()


def test_refresh_does_not_invoke_backfill_or_migrate(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_backfill(*_a, **_k):
        calls.append("backfill")
        return 0

    def fake_migrate(self):
        calls.append("migrate")
        return []

    monkeypatch.setattr(
        "services.archive.backfill_offline_pages", fake_backfill, raising=False
    )
    monkeypatch.setattr(
        "services.file_ops.ModFileManager.migrate_numeric_mod_folders",
        fake_migrate,
    )

    view = ModLibraryView()
    view._target_root = str(tmp_path / "library")
    (tmp_path / "library").mkdir()
    view.refresh()

    assert calls == []


def test_mod_card_shows_offline_missing_when_index_absent(
    qapp: QApplication, tmp_path: Path
) -> None:
    mod = tmp_path / "SomeGame" / "SomeMod"
    mod.mkdir(parents=True)
    (mod / INFO_DIR_NAME).mkdir()

    card = ModCardWidget(mod, metadata=None)
    assert not card.offline_badge.isHidden()
    assert card.offline_badge.text() == OFFLINE_MISSING_LABEL


def test_mod_card_hides_offline_badge_when_index_present(
    qapp: QApplication, tmp_path: Path
) -> None:
    mod = tmp_path / "SomeGame" / "SomeMod"
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "index.html").write_text("<html></html>", encoding="utf-8")

    card = ModCardWidget(mod, metadata=None)
    assert card.offline_badge.isHidden()
    assert "Saved" in card.toolTip() or "Offline" in card.toolTip()
