"""Collection list cards — visual cousin of Mod Card, distinct semantics.

Cover is Collection-owned (``data/collection_covers/``), not a Mod ``.info`` cover.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDrag,
    QDropEvent,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ui.mod_card import CARD_WIDTH, COVER_HEIGHT, COVER_WIDTH, TITLE_LINES, _elide_to_lines
from ui.styles import BACKGROUND_BUTTON_PRESSED, BORDER_STRONG

COLLECTION_MIME = "application/x-smm-collection-id"
COLLECTION_CREATE_CACHE_KEY = "collection:create"

CARD_MARGIN_TOP = 10
CARD_MARGIN_BOTTOM = 8
CARD_LAYOUT_SPACING = 6
ACTION_ICON_SIZE = 24
ACTION_STRIP_HEIGHT = 24
COUNT_OVERLAY_MARGIN = 4


def _title_font() -> QFont:
    font = QFont()
    font.setBold(True)
    return font


def collection_card_title_height(font: QFont | None = None) -> int:
    metrics = QFontMetrics(font or _title_font())
    return max(metrics.height(), metrics.lineSpacing()) * TITLE_LINES


def collection_card_height(title_font: QFont | None = None) -> int:
    """Shared outer height for Create Card and Collection Card.

    Call only after a QApplication exists — QFontMetrics is not import-safe.
    """
    return (
        CARD_MARGIN_TOP
        + CARD_MARGIN_BOTTOM
        + COVER_HEIGHT
        + collection_card_title_height(title_font)
        + ACTION_STRIP_HEIGHT
        + CARD_LAYOUT_SPACING * 2
    )


def collection_cache_key(collection_id: int | str) -> str:
    return f"collection:{int(collection_id)}"


def _placeholder_collection_cover(width: int, height: int) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(BACKGROUND_BUTTON_PRESSED))
    painter = QPainter(pixmap)
    painter.setPen(QColor(BORDER_STRONG))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "合集")
    painter.end()
    return pixmap


@dataclass(frozen=True)
class CollectionCardData:
    """UI bind DTO — Collection only. Not ``ModCardData``."""

    collection_id: int
    name: str
    cover: str = ""
    mod_count: int = 0
    sort_order: int = 0
    app_id: int = 0


class CollectionCreateCard(QFrame):
    """Always-first ``[+]`` slot. Same size as Collection / Mod cards. Not draggable."""

    create_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collectionCreateCard")
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, CARD_MARGIN_TOP, 10, CARD_MARGIN_BOTTOM)
        layout.setSpacing(CARD_LAYOUT_SPACING)
        title_font = _title_font()
        title_h = collection_card_title_height(title_font)
        self.plus_label = QLabel("+")
        self.plus_label.setObjectName("collectionCreatePlus")
        self.plus_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus_font = self.plus_label.font()
        plus_font.setPointSize(36)
        plus_font.setBold(True)
        self.plus_label.setFont(plus_font)
        self.plus_label.setFixedSize(COVER_WIDTH, COVER_HEIGHT)
        self.plus_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.plus_label)
        self.caption = QLabel("创建合集")
        self.caption.setFont(title_font)
        self.caption.setFixedHeight(title_h)
        self.caption.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.caption)
        layout.addStretch(1)
        self.setFixedHeight(collection_card_height(title_font))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.create_requested.emit()
        super().mouseReleaseEvent(event)


class CollectionCardWidget(QFrame):
    """Collection card: cover slot + name + count overlay + rename/cover/delete."""

    rename_requested = Signal(int)
    cover_requested = Signal(int)
    delete_requested = Signal(int)
    sort_drop_requested = Signal(str, str)
    open_requested = Signal(int)

    def __init__(
        self,
        data: CollectionCardData,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collectionCard")
        self._data = data
        self._drag_start = None
        self._cover_token = ""
        self._cover_applied_token = ""
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, CARD_MARGIN_TOP, 10, CARD_MARGIN_BOTTOM)
        layout.setSpacing(CARD_LAYOUT_SPACING)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_WIDTH, COVER_HEIGHT)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.cover_label.setPixmap(_placeholder_collection_cover(COVER_WIDTH, COVER_HEIGHT))
        layout.addWidget(self.cover_label)

        self.count_overlay = QLabel(self.cover_label)
        self.count_overlay.setObjectName("collectionCountOverlay")
        self.count_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        title_font = _title_font()
        title_h = collection_card_title_height(title_font)
        self.title_label = QLabel()
        self.title_label.setFont(title_font)
        self.title_label.setFixedHeight(title_h)
        self.title_label.setWordWrap(True)
        self.title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title_label)

        self.action_row = QWidget()
        self.action_row.setObjectName("collectionActionRow")
        self.action_row.setFixedHeight(ACTION_STRIP_HEIGHT)
        row = QHBoxLayout(self.action_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.btn_rename = self._make_icon_button(
            "编辑合集名称", QStyle.StandardPixmap.SP_FileDialogDetailedView
        )
        self.btn_cover = self._make_icon_button(
            "设置合集封面", QStyle.StandardPixmap.SP_FileDialogContentsView
        )
        self.btn_delete = self._make_icon_button(
            "删除合集",
            QStyle.StandardPixmap.SP_TrashIcon,
            object_name="collectionIconDangerButton",
        )
        self.btn_rename.clicked.connect(
            lambda: self.rename_requested.emit(self.collection_id())
        )
        self.btn_cover.clicked.connect(
            lambda: self.cover_requested.emit(self.collection_id())
        )
        self.btn_delete.clicked.connect(
            lambda: self.delete_requested.emit(self.collection_id())
        )
        row.addWidget(self.btn_rename)
        row.addWidget(self.btn_cover)
        row.addWidget(self.btn_delete)
        row.addStretch(1)
        layout.addWidget(self.action_row)
        self.setFixedHeight(collection_card_height(title_font))
        # Cover delivery via request(on_ready=) — never broadcast image_ready.
        self.destroyed.connect(self._on_card_destroyed)
        self.bind(data)

    def _make_icon_button(
        self,
        tooltip: str,
        icon: QStyle.StandardPixmap,
        *,
        object_name: str = "collectionIconButton",
    ) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName(object_name)
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setIcon(self.style().standardIcon(icon))
        btn.setIconSize(QSize(16, 16))
        btn.setFixedSize(ACTION_ICON_SIZE, ACTION_ICON_SIZE)
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return btn

    def collection_id(self) -> int:
        return int(self._data.collection_id)

    def bind(self, data: CollectionCardData) -> None:
        self.rebind(data)

    def rebind(self, data: CollectionCardData) -> None:
        self._data = data
        name = str(data.name or "").strip() or "—"
        elided = _elide_to_lines(
            name, self.title_label.font(), max(1, COVER_WIDTH), TITLE_LINES
        )
        self.title_label.setText(elided)
        self.title_label.setToolTip(name)
        count = max(0, int(data.mod_count or 0))
        self.count_overlay.setText(f"{count} Mods")
        self.count_overlay.setToolTip(f"{count} Mods")
        self._sync_cover()
        self._position_count_overlay()

    def _cover_token_for(self, cover_ref: str) -> str:
        return f"collection:{id(self)}:{self.collection_id()}:{cover_ref}"

    def _cancel_cover(self, *, keep_pixmap: bool = False) -> None:
        tok = getattr(self, "_cover_token", "") or ""
        if tok:
            from services.cover_loader import CoverLoaderManager

            CoverLoaderManager.instance().cancel(tok)
        self._cover_token = ""
        if not keep_pixmap:
            self._cover_applied_token = ""
            self.cover_label.setPixmap(
                _placeholder_collection_cover(COVER_WIDTH, COVER_HEIGHT)
            )

    def _sync_cover(self) -> None:
        from services.collection_cover import stored_cover_file
        from services.cover_loader import CoverLoaderManager

        cover_ref = str(self._data.cover or "").strip()
        if not cover_ref:
            self._cancel_cover(keep_pixmap=False)
            return
        abs_file = stored_cover_file(cover_ref)
        token = self._cover_token_for(cover_ref)
        if abs_file is None:
            self._cancel_cover(keep_pixmap=False)
            return
        if getattr(self, "_cover_applied_token", "") == token:
            self._cover_token = token
            return
        try:
            from services.cover_cache import get_cover_image
            from services.cover_loader import note_cover_cache_hit

            cached = get_cover_image(abs_file, COVER_WIDTH, COVER_HEIGHT)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            note_cover_cache_hit()
            self._cancel_cover(keep_pixmap=True)
            self._cover_token = token
            self._cover_applied_token = token
            self._apply_cover_image(cached)
            return
        prev = self._cover_token
        self._cover_token = token
        mgr = CoverLoaderManager.instance()
        if prev and prev != token:
            mgr.cancel(prev)
        from core.paths import collection_covers_dir

        mgr.request(
            token,
            collection_covers_dir(),
            cover_ref=str(abs_file),
            width=COVER_WIDTH,
            height=COVER_HEIGHT,
            on_ready=self._on_cover_image_ready,
        )

    def _on_cover_image_ready(self, token: str, image: object) -> None:
        if str(token) != getattr(self, "_cover_token", ""):
            return
        if not isinstance(image, QImage) or image.isNull():
            return
        self._cover_applied_token = str(token)
        self._apply_cover_image(image)

    def _apply_cover_image(self, qimage: QImage) -> None:
        target_w = COVER_WIDTH
        target_h = COVER_HEIGHT
        x = max(0, (qimage.width() - target_w) // 2)
        y = max(0, (qimage.height() - target_h) // 2)
        try:
            pixmap = QPixmap.fromImage(qimage).copy(x, y, target_w, target_h)
            self.cover_label.setPixmap(pixmap)
        except RuntimeError:
            return
        self._position_count_overlay()

    def _on_card_destroyed(self) -> None:
        tok = getattr(self, "_cover_token", "") or ""
        if not tok:
            return
        try:
            from services.cover_loader import CoverLoaderManager

            CoverLoaderManager.instance().cancel(tok)
        except Exception:  # noqa: BLE001
            pass

    def _hit_action_row(self, pos) -> bool:
        child = self.childAt(pos.toPoint() if hasattr(pos, "toPoint") else pos)
        while child is not None and child is not self:
            if child is self.action_row:
                return True
            child = child.parentWidget()
        return False

    def _position_count_overlay(self) -> None:
        self.count_overlay.adjustSize()
        cover_w = self.cover_label.width() or COVER_WIDTH
        cover_h = self.cover_label.height() or COVER_HEIGHT
        x = max(COUNT_OVERLAY_MARGIN, cover_w - self.count_overlay.width() - COUNT_OVERLAY_MARGIN)
        y = max(COUNT_OVERLAY_MARGIN, cover_h - self.count_overlay.height() - COUNT_OVERLAY_MARGIN)
        self.count_overlay.move(x, y)
        self.count_overlay.show()
        self.count_overlay.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_count_overlay()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._hit_action_row(event.position()):
                self._drag_start = None
                super().mousePressEvent(event)
                return
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._hit_action_row(event.position()):
            self._drag_start = None
            super().mouseReleaseEvent(event)
            return
        was_click = (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_start is not None
        )
        self._drag_start = None
        if was_click:
            self.open_requested.emit(self.collection_id())
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._drag_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            from PySide6.QtWidgets import QApplication

            dist = (event.position().toPoint() - self._drag_start).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._start_sort_drag()
                self._drag_start = None
                return
        super().mouseMoveEvent(event)

    def _start_sort_drag(self) -> None:
        cid = str(self.collection_id())
        mime = QMimeData()
        mime.setData(COLLECTION_MIME, cid.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def _drop_source_id(self, event: QDropEvent) -> str:
        mime = event.mimeData()
        if mime is None or not mime.hasFormat(COLLECTION_MIME):
            return ""
        raw = bytes(mime.data(COLLECTION_MIME)).decode("utf-8", errors="replace")
        return str(raw or "").strip()

    def dragEnterEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self._drop_source_id(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self._drop_source_id(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        source = self._drop_source_id(event)
        target = str(self.collection_id())
        if source and target and source != target:
            self.sort_drop_requested.emit(source, target)
            event.acceptProposedAction()
            return
        event.ignore()
