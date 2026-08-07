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
    assert card.offline_label.text() == OFFLINE_MISSING_LABEL


def test_mod_card_shows_offline_status_when_index_present(
    qapp: QApplication, tmp_path: Path
) -> None:
    mod = tmp_path / "SomeGame" / "SomeMod"
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "index.html").write_text("<html></html>", encoding="utf-8")

    card = ModCardWidget(mod, metadata=None)
    assert card.offline_label.text() == "离线页已同步"
