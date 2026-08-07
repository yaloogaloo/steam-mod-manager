"""Preview card showing resolved Steam game metadata."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.game_info import GameInfo

COVER_W = 184
COVER_H = 86


class GamePreviewCard(QFrame):
    """Shows game header image, title, AppID and short description."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("gamePreviewCard")
        self.setMinimumHeight(110)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_W, COVER_H)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.cover_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        self.title_label = QLabel("尚未识别游戏")
        self.title_label.setObjectName("gamePreviewTitle")
        self.title_label.setWordWrap(True)
        text_col.addWidget(self.title_label)

        self.meta_label = QLabel("请选择以 AppID 结尾的创意工坊目录，例如 …/content/1623730")
        self.meta_label.setObjectName("gamePreviewMeta")
        self.meta_label.setWordWrap(True)
        text_col.addWidget(self.meta_label)

        self.desc_label = QLabel("")
        self.desc_label.setObjectName("gamePreviewDesc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        text_col.addWidget(self.desc_label, stretch=1)

        layout.addLayout(text_col, stretch=1)
        self.clear()

    def clear(self) -> None:
        self.title_label.setText("尚未识别游戏")
        self.meta_label.setText(
            "请选择以 AppID 结尾的创意工坊目录，例如 …/content/1623730"
        )
        self.desc_label.setText("")
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H, "Game"))

    def show_loading(self, app_id: int) -> None:
        self.title_label.setText("正在获取游戏信息…")
        self.meta_label.setText(f"AppID {app_id}")
        self.desc_label.setText("")
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H, "…"))

    def show_info(self, info: GameInfo, cover: QPixmap | None = None) -> None:
        self.title_label.setText(info.display_name)
        err = f"  ·  {info.fetch_error}" if info.fetch_error else ""
        self.meta_label.setText(f"AppID {info.app_id}{err}")
        desc = (info.short_description or "").strip()
        if len(desc) > 280:
            desc = desc[:277] + "…"
        self.desc_label.setText(desc)

        if cover is not None and not cover.isNull():
            self.cover_label.setPixmap(
                cover.scaled(
                    COVER_W,
                    COVER_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.cover_label.setPixmap(
                _placeholder(COVER_W, COVER_H, info.display_name[:12] or "Game")
            )

    def show_message(self, title: str, detail: str) -> None:
        self.title_label.setText(title)
        self.meta_label.setText(detail)
        self.desc_label.setText("")
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H, "?"))


def _placeholder(width: int, height: int, text: str) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#1b2838"))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#3d5a73"))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return pixmap
