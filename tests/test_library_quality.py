"""Native presentation contracts with no business provider or production data."""
from types import SimpleNamespace
from uuid import UUID
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication, QListView, QAbstractItemView
from PySide6.QtTest import QTest
from frontend.library_adapter import Asset
from frontend.asset_grid import AssetGrid
from frontend.asset_model import AssetModel, AssetDelegate, card_rects
from frontend.inspector_title import InspectorTitle


def test_current_and_batch_selection_are_independent():
    app = QApplication.instance() or QApplication([])
    view = AssetGrid()
    view.setViewMode(QListView.IconMode)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    view.setMovement(QListView.Static)
    model = AssetModel(SimpleNamespace(cover_file=lambda asset: None), view)
    view.setModel(model)
    delegate = AssetDelegate(view)
    view.setItemDelegate(delegate)
    model.replace([Asset(str(UUID(int=i+1)), str(i), i, '', 'Long title '*20,
                         'Steam', True, False, 'Map', '', '') for i in range(12)])
    view.resize(900, 600)
    view.show()
    QTest.qWait(50)
    view.reflow()
    r0, r1 = view.visualRect(model.index(0)), view.visualRect(model.index(1))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, r0.center())
    assert view.currentIndex().row() == 0 and not view.selectedIndexes()
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, r1.topLeft()+QPoint(20, 20))
    assert view.currentIndex().row() == 0 and [i.row() for i in view.selectedIndexes()] == [1]
    QTest.keyClick(view, Qt.Key_Space)
    assert {i.row() for i in view.selectedIndexes()} == {0, 1}
    QTest.keyClick(view, Qt.Key_Right)
    assert {i.row() for i in view.selectedIndexes()} == {0, 1}
    card, cover, name = card_rects(r0, view.font(), False)
    assert card.contains(cover) and card.contains(name)
    assert name.height() >= 2*view.fontMetrics().height()
    assert view.viewport().width()-view.columns*view.gridSize().width() <= view.columns
    view.close()
    assert model.shutdown()


def test_cover_failures_are_not_reported_as_missing():
    app = QApplication.instance() or QApplication([])
    model = AssetModel(SimpleNamespace(cover_file=lambda asset: None))
    asset = Asset(str(UUID(int=1)), 'external', 1, '', 'Synthetic', 'Local', False, False, '', '', '')
    model.replace([asset])
    for state in ['missing', 'invalid', 'error']:
        model._ready(model.generation, asset.internal_id, (None, state))
        assert model.cover_states[asset.internal_id] == state
    generation = model.generation
    model.replace([asset])
    model._ready(generation, asset.internal_id, (None, 'error'))
    assert not model.cover_states
    assert model.shutdown()


def test_overview_title_is_bounded_without_losing_source_text():
    app = QApplication.instance() or QApplication([])
    text = '多语言长名称 Long title ' * 40
    label = InspectorTitle(text)
    label.resize(170, label.sizeHint().height())
    label.show()
    QTest.qWait(20)
    assert label.text() == text
    assert label.height() == 3*label.fontMetrics().height()
    assert not label.grab().isNull()
    label.close()
