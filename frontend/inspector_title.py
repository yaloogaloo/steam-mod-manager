"""Bounded overview heading; full title remains selectable in the detail dialog."""
from PySide6.QtCore import QSize, QPointF, Qt
from PySide6.QtGui import QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import QLabel, QSizePolicy


class InspectorTitle(QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setProperty('role', 'inspectorTitle')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(0)

    def sizeHint(self):
        return QSize(180, self.fontMetrics().height()*3)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height()*3)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().windowText().color())
        layout = QTextLayout(self.text(), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        height = self.fontMetrics().height()
        count = max(1, min(3, self.height() // height))
        for n in range(count):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(self.width())
            if n == count-1 and line.textStart()+line.textLength() < len(self.text()):
                text = self.fontMetrics().elidedText(self.text()[line.textStart():], Qt.ElideRight, self.width())
                painter.drawText(0, n*height+self.fontMetrics().ascent(), text)
            else:
                line.draw(painter, QPointF(0, n*height))
        layout.endLayout()
