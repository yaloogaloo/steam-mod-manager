"""Regression: parentless QWidget.Show must not flash control widgets."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.mod_platform import PLATFORM_NEXUS, ModFileEntry
from ui.mod_detail_panel import ModDetailPanel
from ui.widget_show_trace import install_widget_show_trace


def test_parentless_show_is_flagged_as_suspect() -> None:
    app = QApplication.instance() or QApplication([])
    state = install_widget_show_trace(app)
    before = state.suspect_count

    orphan = QPushButton("✎")
    orphan.setObjectName("orphanProbe")
    orphan.setFixedSize(24, 24)
    orphan.show()  # illegal: show before parent/layout
    app.processEvents()

    assert state.suspect_count > before
    orphan.hide()
    orphan.deleteLater()


def test_file_row_controls_never_toplevel(tmp_path: Path) -> None:
    """Import → show_mod rebuilds file rows; controls must stay parented."""
    app = QApplication.instance() or QApplication([])
    state = install_widget_show_trace(app)

    host = QWidget()
    layout = QVBoxLayout(host)
    panel = ModDetailPanel()
    layout.addWidget(panel)
    host.resize(480, 800)
    host.show()
    app.processEvents()

    # Baseline after intentional top-level host.show() (not a control flash).
    before = state.suspect_count

    entries = [
        ModFileEntry(
            id=f"f{i}",
            name=f"File {i}",
            path=f"a{i}.pak",
            selected_for_deploy=True,
            metadata={"category": "Main" if i == 0 else "Optional"},
        )
        for i in range(3)
    ]
    panel._current_platform = PLATFORM_NEXUS
    assert hasattr(panel, "mod_files_host")
    if hasattr(panel, "_files_section_frame"):
        panel._files_section_frame.show()
    panel.mod_files_host.show()

    for entry in entries:
        row = panel._add_mod_file_row(entry)
        assert row.parent() is not None
        assert not row.isWindow()
        for child in row.findChildren(QWidget):
            if isinstance(child, (QPushButton, QLabel, QCheckBox)):
                assert child.parent() is not None, type(child).__name__
                assert not child.isWindow(), type(child).__name__

    edits = panel.mod_files_host.findChildren(QPushButton, "detailFilesEditButton")
    assert edits
    for btn in edits:
        assert btn.parent() is not None
        assert not btn.isWindow()

    # Building rows must not create new parentless Show flashes.
    assert state.suspect_count == before

    host.close()
