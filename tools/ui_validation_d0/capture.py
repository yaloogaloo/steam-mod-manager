"""Geometry exporter for a future admitted REAL QWidget run, not a fake UI."""
from __future__ import annotations


def geometry(widget):
    # Import Qt only after caller has an admitted widget. No application here.
    from PySide6.QtWidgets import QWidget
    if not isinstance(widget, QWidget):
        raise TypeError("A real QWidget is required")
    result = []
    for item in [widget, *widget.findChildren(QWidget)]:
        rect = item.geometry()
        hint = item.sizeHint()
        result.append({"class": type(item).__name__, "object_name": item.objectName(), "visible": item.isVisible(), "enabled": item.isEnabled(), "geometry": [rect.x(), rect.y(), rect.width(), rect.height()], "size_hint": [hint.width(), hint.height()], "minimum": [item.minimumWidth(), item.minimumHeight()], "maximum": [item.maximumWidth(), item.maximumHeight()], "device_pixel_ratio": item.devicePixelRatioF()})
    return result
