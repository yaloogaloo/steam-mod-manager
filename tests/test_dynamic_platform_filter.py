"""Library no longer exposes a dedicated 来源 / platform filter row."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_library_source_filter_row_removed(qapp: QApplication) -> None:
    view = ModLibraryView()
    assert not hasattr(view, "_source_row")
    assert not hasattr(view, "_source_caption")
    assert not hasattr(view, "_platform_bar")
    assert not hasattr(view, "_platform_buttons")
    assert not hasattr(view, "_rebuild_platform_filter_bar")
    center = view.findChild(QWidget, "libraryCenter")
    assert center is not None
    captions = [str(w.text() or "").strip() for w in center.findChildren(QLabel)]
    assert "来源" not in captions
    view.deleteLater()
