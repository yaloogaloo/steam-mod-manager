"""Virtualized Qt item model and cover delegate with bounded image workers."""
from collections import OrderedDict
from uuid import uuid4
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRunnable, QSize, Qt, QThreadPool, Signal, QRectF, QPointF
from PySide6.QtGui import QColor, QImage, QPen, QLinearGradient, QFont, QFontMetrics, QPainterPath, QTextLayout, QTextOption
from PySide6.QtWidgets import QStyledItemDelegate, QStyle
from services.cover_cache import get_cover_image, put_cover_image
from .theme import COLORS


class _Signals(QObject):
    done = Signal(str, str, object)


class _Cover(QRunnable):
    def __init__(self, generation, identity, adapter, asset, signals):
        super().__init__()
        self.generation, self.identity, self.adapter, self.asset, self.signals = generation, identity, adapter, asset, signals

    def run(self):
        try:
            self.path = self.adapter.cover_file(self.asset)
            if self.path is None:
                self.signals.done.emit(self.generation, self.identity, (None, "missing"))
                return
            image = get_cover_image(self.path, 320, 180)
            if image is None:
                image = QImage(str(self.path))
                if not image.isNull():
                    image = image.scaled(320, 180, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    put_cover_image(self.path, 320, 180, image)
            state = "ready" if image is not None and not image.isNull() else "invalid"
        except Exception:
            image, state = None, "error"
        self.signals.done.emit(self.generation, self.identity, (image, state))


class AssetModel(QAbstractListModel):
    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.rows = []
        self.generation = uuid4().hex
        self.images = OrderedDict()
        self.pending = set()
        self.cover_states = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.signals = _Signals(self)
        self.signals.done.connect(self._ready, Qt.QueuedConnection)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role in (Qt.DisplayRole, Qt.ToolTipRole, Qt.AccessibleTextRole):
            a = self.rows[index.row()]
            return f'{a.name}\n{a.platform} · {"已部署" if a.deployed else "未部署"} {a.status}'

    def replace(self, rows):
        self.beginResetModel()
        self.generation = uuid4().hex
        self.pool.clear()
        self.pending.clear()
        self.images.clear()
        self.cover_states.clear()
        self.rows = list(rows)
        self.endResetModel()

    def image(self, asset):
        key = (asset.internal_id, asset.cover)
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        if asset.internal_id not in self.pending:
            self.pending.add(asset.internal_id)
            self.pool.start(_Cover(self.generation, asset.internal_id, self.adapter, asset, self.signals))
        return None

    def _ready(self, generation, identity, image):
        if generation != self.generation:
            return
        self.pending.discard(identity)
        state = "ready"
        if isinstance(image, tuple):
            image, state = image
        self.cover_states[identity] = state
        for row, asset in enumerate(self.rows):
            if asset.internal_id == identity:
                self.images[(identity, asset.cover)] = image
                while len(self.images) > 150:
                    evicted, _ = self.images.popitem(last=False)
                    self.cover_states.pop(evicted[0], None)
                index = self.index(row)
                self.dataChanged.emit(index, index)
                break

    def shutdown(self):
        self.generation = uuid4().hex
        self.pool.clear()
        return self.pool.waitForDone(5000)


def card_body_height(font):
    meta = QFont(font)
    meta.setPixelSize(12)
    return 2 * QFontMetrics(font).height() + 2 * QFontMetrics(meta).height() + 18


def card_rects(cell, font, compact):
    card = cell.adjusted(4, 4, -4, -4)
    if card.width() > 280:
        extra = (card.width()-280)//2
        card.adjust(extra, 0, -extra, 0)
    cover = card.adjusted(1, 1, -1, 0)
    cover.setHeight(round(cover.width() / (1.78 if compact else 1.6)))
    name = card.adjusted(11, cover.height()+7, -11, 0)
    name.setHeight(2*QFontMetrics(font).height())
    return card, cover, name


class AssetDelegate(QStyledItemDelegate):
    compact = False
    cell_size = QSize(214, 240)

    def sizeHint(self, option, index):
        return self.cell_size

    def paint(self, painter, option, index):
        a = index.model().rows[index.row()]
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect, cover, name = card_rects(option.rect, option.font, self.compact)
        checked = bool(option.state & QStyle.State_Selected)
        current = self.parent().currentIndex() == index
        focused = current and self.parent().hasFocus()
        hover = bool(option.state & QStyle.State_MouseOver)
        painter.setBrush(QColor(COLORS['elevated' if hover else 'surface']))
        painter.setPen(QPen(QColor(COLORS['focus' if focused else 'accent' if current else 'border']), 2 if focused else 1))
        painter.drawRoundedRect(rect, 8, 8)
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 7, 7)
        painter.setClipPath(clip)
        image = index.model().image(a)
        if image is not None and not image.isNull():
            target = image.size().scaled(cover.size(), Qt.KeepAspectRatioByExpanding)
            painter.setClipRect(cover, Qt.IntersectClip)
            from PySide6.QtCore import QRect
            painter.drawImage(QRect(cover.center().x()-target.width()//2,
                                    cover.center().y()-target.height()//2,
                                    target.width(), target.height()), image)
        else:
            gradient = QLinearGradient(cover.topLeft(), cover.bottomRight())
            gradient.setColorAt(0, QColor(COLORS['nav-background']))
            gradient.setColorAt(1, QColor(COLORS['elevated']))
            painter.fillRect(cover, gradient)
            painter.setPen(QPen(QColor(COLORS['border']), 1))
            center = cover.center()
            for radius in (22, 36, 50):
                painter.drawEllipse(center, radius, radius)
            painter.setPen(QColor(COLORS['text-secondary']))
            font = QFont(option.font); font.setPixelSize(18); font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.drawText(cover.adjusted(0, -12, 0, -12), Qt.AlignCenter, 'SMM')
            font.setPixelSize(11); painter.setFont(font)
            state = index.model().cover_states.get(a.internal_id, 'loading')
            caption = {'missing':'默认预览', 'invalid':'图片无法解码', 'error':'封面读取失败', 'loading':'正在读取封面'}.get(state, '默认预览')
            painter.drawText(cover.adjusted(0, 30, 0, 0), Qt.AlignCenter, caption)
        painter.restore()
        # Explicit checkmark collection; current inspection has a separate single border.
        check = rect.adjusted(8, 8, 0, 0)
        check.setSize(QSize(20, 20))
        painter.setBrush(QColor(COLORS['accent'] if checked else COLORS['surface']))
        painter.setPen(QPen(QColor(COLORS['accent'] if checked else COLORS['text-secondary']), 1))
        painter.drawRoundedRect(check, 4, 4)
        if checked:
            painter.setPen(QPen(QColor(COLORS['on-accent']), 2))
            painter.drawLine(check.left()+5, check.top()+10, check.left()+9, check.top()+14)
            painter.drawLine(check.left()+9, check.top()+14, check.left()+16, check.top()+6)
        if a.favorite:
            painter.setPen(QColor(COLORS['warning']))
            painter.setFont(option.font)
            painter.drawText(cover.adjusted(0, 6, -8, 0), Qt.AlignRight | Qt.AlignTop, '★')
        # Badge uses text as well as colour. No competing full-card green selection fill.
        meta_font = QFont(option.font); meta_font.setPixelSize(12)
        painter.setFont(meta_font)
        fm = painter.fontMetrics()
        status = '已部署' if a.deployed else '未部署'
        badge = cover.adjusted(8, cover.height()-25, 0, -5)
        badge.setWidth(fm.horizontalAdvance(status)+16)
        painter.setBrush(QColor(COLORS['surface']))
        painter.setPen(Qt.NoPen); painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(QColor(COLORS['success' if a.deployed else 'text-secondary']))
        painter.drawText(badge, Qt.AlignCenter, status)
        painter.setFont(option.font)
        painter.setPen(QColor(COLORS['text-primary']))
        layout = QTextLayout(a.name, option.font)
        opt = QTextOption(); opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt); layout.beginLayout()
        y = 0
        for number in range(2):
            line = layout.createLine()
            if not line.isValid(): break
            line.setLineWidth(name.width())
            if number == 1 and line.textStart()+line.textLength() < len(a.name):
                text = QFontMetrics(option.font).elidedText(a.name[line.textStart():], Qt.ElideRight, name.width())
                painter.drawText(name.left(), name.top()+y+QFontMetrics(option.font).ascent(), text)
            else:
                line.draw(painter, QPointF(name.left(), name.top()+y))
            y += QFontMetrics(option.font).height()
        layout.endLayout()
        painter.setFont(meta_font); painter.setPen(QColor(COLORS['text-muted']))
        line_rect = name.translated(0, name.height()+3); line_rect.setHeight(fm.height())
        painter.drawText(line_rect, Qt.AlignLeft, fm.elidedText(a.platform+' · '+(a.category or '未分类'), Qt.ElideRight, line_rect.width()))
        line_rect.translate(0, fm.height()+2)
        painter.setPen(QColor(COLORS['warning' if a.status else 'text-muted']))
        painter.drawText(line_rect, Qt.AlignLeft, fm.elidedText(a.status or ('正在查看' if current else ''), Qt.ElideRight, line_rect.width()))
        painter.restore()
