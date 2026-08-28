"""UI: only conflict_status=conflict counts as file-conflict display."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
    ModStatus,
)
from core.models import ModMetadata
from services.library_status import CONTENT_HEALTHY
from ui.library_query import (
    FILTER_CONFLICT,
    ModFilterIndex,
    index_is_anomaly,
    matches_status_filter,
)
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _idx(*, conflict_status: str, conflict: bool | None = None) -> ModFilterIndex:
    if conflict is None:
        conflict = conflict_status == "conflict"
    return ModFilterIndex(
        mod_id="1",
        display_name="T",
        steam_name="T",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="T",
        conflict_status=conflict_status,
        conflict=conflict,
        content_status=CONTENT_HEALTHY,
        enabled=True,
        platform="steam",
    )


def _data(**kwargs) -> SimpleNamespace:
    base = dict(
        folder_absent=False,
        missing_content=False,
        content_status=CONTENT_HEALTHY,
        library_status="healthy",
        cover="",
        source_type="steam",
        steam_name="",
        display_name="Card",
        title="Card",
        json_display_name="",
        favorite=False,
        abandoned=False,
        offline_status="",
        deploy_status="",
        game_status="",
        platform="steam",
        conflict=False,
        invalid=False,
        is_invalid=False,
        enabled=True,
        category_tags="",
        relation_conflicts=0,
        relation_deps=0,
        has_offline=False,
        id="9",
        conflict_status="none",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _card(tmp_path: Path, *, data: SimpleNamespace) -> ModCardWidget:
    folder = tmp_path / "G" / "M"
    folder.mkdir(parents=True, exist_ok=True)
    meta = ModMetadata(
        published_file_id=str(data.id),
        title="Card",
        managed_path=str(folder),
        source_type="steam",
    )
    return ModCardWidget(folder, meta, card_data=data)


def test_conflict_status_shows_as_conflict_filter() -> None:
    idx = _idx(conflict_status="conflict")
    assert matches_status_filter(idx, FILTER_CONFLICT)
    assert index_is_anomaly(idx)


def test_warning_does_not_show_as_conflict_filter() -> None:
    idx = _idx(conflict_status="warning", conflict=False)
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert not index_is_anomaly(idx)


def test_none_is_normal() -> None:
    idx = _idx(conflict_status="none", conflict=False)
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert not index_is_anomaly(idx)


def test_card_conflict_badge_only_for_conflict_flag(
    qapp: QApplication, tmp_path: Path
) -> None:
    conflict_card = _card(
        tmp_path, data=_data(conflict=True, conflict_status="conflict")
    )
    c, *_ = conflict_card._overlay_user_flags()
    assert c is True

    warn_card = _card(
        tmp_path, data=_data(conflict=False, conflict_status="warning", id="10")
    )
    c2, *_ = warn_card._overlay_user_flags()
    assert c2 is False

    none_card = _card(
        tmp_path, data=_data(conflict=False, conflict_status="none", id="11")
    )
    c3, *_ = none_card._overlay_user_flags()
    assert c3 is False


def test_card_db_fallback_warning_not_conflict(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "G" / "M2"
    folder.mkdir(parents=True)
    meta = ModMetadata(
        published_file_id="55",
        title="Card",
        managed_path=str(folder),
    )
    card = ModCardWidget(folder, meta, card_data=None)

    class _FakeDb:
        def get_mod_status(self, mid):  # noqa: ANN001
            return ModStatus(
                conflict_status=CONFLICT_STATUS_WARNING,
                conflict_note="legacy soft note",
            )

        def is_mod_enabled(self, mid):  # noqa: ANN001
            return True

        def get_mods_tag_flags(self, mids):  # noqa: ANN001
            return {}

    monkeypatch.setattr("ui.mod_card.get_db", lambda: _FakeDb())
    conflict, _invalid, _disabled, tips = card._overlay_user_flags()
    assert conflict is False
    assert any("警告" in t for t in tips)

    class _FakeConflictDb(_FakeDb):
        def get_mod_status(self, mid):  # noqa: ANN001
            return ModStatus(conflict_status=CONFLICT_STATUS_CONFLICT)

    monkeypatch.setattr("ui.mod_card.get_db", lambda: _FakeConflictDb())
    conflict2, *_ = card._overlay_user_flags()
    assert conflict2 is True


def test_mod_status_run_label_semantics() -> None:
    assert ModStatus(conflict_status=CONFLICT_STATUS_CONFLICT).run_label == "冲突"
    assert ModStatus(conflict_status=CONFLICT_STATUS_WARNING).run_label == "警告"
    assert ModStatus(conflict_status=CONFLICT_STATUS_NONE).run_label == "正常"
