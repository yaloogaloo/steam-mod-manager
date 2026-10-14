"""Shared native widgets. Callers supply presentation content explicitly."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


def label(text: str, role: str = "", parent: QWidget | None = None) -> QLabel:
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    return widget


class StatusBadge(QLabel):
    """Text and color together convey state; accepts no HTML content."""
    def __init__(self, text: str, tone: str = "info", parent=None):
        super().__init__(text, parent)
        if tone not in {"info", "success", "warning", "error"}:
            raise ValueError("Unknown status tone")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setProperty("role", "badge")
        self.setProperty("tone", tone)


class PageHeader(QWidget):
    """Same title, summary and action geometry in every workspace."""

    def __init__(self, title: str, summary: str, action_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("PageHeader")
        self.title = label(title, "heading", self)
        self.summary = label(summary, "muted", self)
        self.help_button = QPushButton("工作区帮助", self)
        self.primary_action = QPushButton(action_text, self)
        self.primary_action.setProperty("role", "primary")
        self.primary_action.setEnabled(False)
        self.primary_action.setToolTip("业务尚未接入；此入口不会执行真实操作")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        layout.addWidget(self.title)
        layout.addWidget(self.summary)
        layout.addStretch()
        layout.addWidget(self.help_button)
        layout.addWidget(self.primary_action)
        self.setMinimumHeight(40)
