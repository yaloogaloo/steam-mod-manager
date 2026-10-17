"""DIAGNOSTIC ONLY — capture transient top-level windows (do not import from production).

Records Qt Show/Hide/Activate plus popup API stacks and Win32 HWND diffs.
Never changes production behavior besides wrapping APIs to log then forward.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QToolTip,
    QWidget,
)

IGNORE_CLASSES = frozenset(
    {
        "QMenuBar",
        "QRubberBand",
        "QSizeGrip",
        "QScrollBar",
    }
)


def _now() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def _stack(limit: int = 18) -> list[str]:
    frames = traceback.extract_stack(limit=limit + 10)
    lines: list[str] = []
    skip = (
        "transient_window_capture.py",
        "traceback.py",
    )
    for fr in frames:
        path = (fr.filename or "").replace("\\", "/")
        if any(s in path for s in skip):
            continue
        lines.append(f"  {fr.filename}:{fr.lineno} in {fr.name}")
    return lines[-limit:]


def _widget_info(obj: QObject | None) -> dict[str, Any]:
    if obj is None or not isinstance(obj, QWidget):
        return {
            "class": type(obj).__name__ if obj is not None else None,
            "qt": False,
        }
    title = ""
    text = ""
    try:
        title = str(obj.windowTitle() or "")
    except Exception:  # noqa: BLE001
        title = ""
    try:
        if hasattr(obj, "text") and callable(obj.text):
            text = str(obj.text() or "")[:160]
    except Exception:  # noqa: BLE001
        text = ""
    parent = obj.parent()
    geom = obj.geometry()
    try:
        modal = bool(obj.isModal())
    except Exception:  # noqa: BLE001
        modal = False
    try:
        modality = str(obj.windowModality())
    except Exception:  # noqa: BLE001
        modality = ""
    try:
        win_type = str(obj.windowType())
    except Exception:  # noqa: BLE001
        win_type = ""
    return {
        "class": type(obj).__name__,
        "objectName": obj.objectName() or "",
        "windowTitle": title,
        "text": text,
        "parent": type(parent).__name__ if parent is not None else None,
        "parentObjectName": parent.objectName() if isinstance(parent, QWidget) else "",
        "isWindow": bool(obj.isWindow()),
        "isVisible": bool(obj.isVisible()),
        "isModal": modal,
        "windowModality": modality,
        "windowType": win_type,
        "topLevel": bool(obj.isWindow() and obj.parent() is None),
        "geometry": f"{geom.x()},{geom.y()} {geom.width()}x{geom.height()}",
        "qt": True,
    }


def classify_record(rec: dict[str, Any]) -> str:
    cls = str(rec.get("class") or "")
    title = str(rec.get("windowTitle") or rec.get("title") or "")
    api = str(rec.get("api") or "")
    text = str(rec.get("text") or "")
    if rec.get("kind") == "MARKER":
        return "EXPECTED"
    if rec.get("kind") == "SETPARENT_NONE_WHILE_VISIBLE":
        return "UNEXPECTED"
    if cls in {"DependencyItem"} and rec.get("topLevel"):
        return "UNEXPECTED"
    if cls in {"QMainWindow", "MainWindow"}:
        return "EXPECTED"
    if "QFileDialog" in cls or "FileDialog" in cls or api.startswith("QFileDialog"):
        return "EXPECTED"
    if cls in {"QTipLabel", "QToolTip"} or api == "QToolTip.showText":
        return "UNCLASSIFIED"
    if cls in {"QComboBoxPrivateContainer", "QMenu"}:
        return "UNCLASSIFIED"
    if rec.get("topLevel") and cls in {
        "QPushButton",
        "QRadioButton",
        "QLabel",
        "QCheckBox",
        "QToolButton",
        "QFrame",
        "QWidget",
    }:
        return "UNEXPECTED"
    if api.startswith("QMessageBox") or cls == "QMessageBox":
        if any(k in (title + text) for k in ("失败", "错误", "确认", "无法", "缺少")):
            return "UNCLASSIFIED"
        return "UNEXPECTED"
    if cls in {"QDialog", "QProgressDialog"} or api.startswith("QDialog") or api.startswith(
        "QProgressDialog"
    ):
        return "UNEXPECTED"
    if rec.get("hwnd_new"):
        if cls == "CabinetWClass" or "文件资源管理器" in title or "File Explorer" in title:
            return "EXTERNAL_SHELL"
        return "UNCLASSIFIED"
    if rec.get("isWindow") or rec.get("topLevel"):
        return "UNCLASSIFIED"
    return "UNCLASSIFIED"


@dataclass
class CaptureState:
    out_dir: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    capturing: bool = False
    phase: str = ""
    orig_notify: Callable[..., Any] | None = None
    origs: dict[str, Any] = field(default_factory=dict)
    hwnd_baseline: set[int] = field(default_factory=set)

    def emit(self, rec: dict[str, Any]) -> None:
        rec.setdefault("ts", _now())
        rec.setdefault("epoch", time.time())
        rec.setdefault("phase", self.phase)
        rec.setdefault("pid", os.getpid())
        rec["classification"] = classify_record(rec)
        with self.lock:
            self.events.append(rec)
            path = self.out_dir / "events.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        line = (
            f"{rec['ts']} {rec.get('kind', '?')} class={rec.get('class', rec.get('api', ''))} "
            f"title={rec.get('windowTitle', rec.get('title', ''))!r} "
            f"objectName={rec.get('objectName', '')!r} "
            f"modal={rec.get('isModal', '')} phase={self.phase} "
            f"classif={rec['classification']}"
        )
        print(line, flush=True)


_STATE: CaptureState | None = None
_IN_NOTIFY = False


def current_state() -> CaptureState | None:
    return _STATE


def mark(name: str, **extra: Any) -> None:
    state = _STATE
    if state is None:
        return
    rec = {"kind": "MARKER", "marker": name, **extra}
    state.emit(rec)


def set_phase(name: str) -> None:
    state = _STATE
    if state is not None:
        state.phase = name
        mark("PHASE", phase=name)


def _enum_win32() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return []

    user32 = ctypes.windll.user32
    results: list[dict[str, Any]] = []
    smm_pid = os.getpid()

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append(
            {
                "hwnd": int(hwnd),
                "title": buf.value,
                "class_name": cls_buf.value,
                "pid": int(pid.value),
                "same_process": int(pid.value) == smm_pid,
                "geometry": (
                    f"{rect.left},{rect.top} "
                    f"{rect.right - rect.left}x{rect.bottom - rect.top}"
                ),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def snapshot_hwnds() -> set[int]:
    return {int(row["hwnd"]) for row in _enum_win32()}


def emit_hwnd_diff(reason: str) -> None:
    state = _STATE
    if state is None:
        return
    rows = _enum_win32()
    now = {int(r["hwnd"]) for r in rows}
    new = now - state.hwnd_baseline
    if not new:
        return
    by_hwnd = {int(r["hwnd"]): r for r in rows}
    for hwnd in sorted(new):
        row = by_hwnd.get(hwnd) or {}
        state.emit(
            {
                "kind": "WIN32_NEW",
                "hwnd_new": True,
                "hwnd": hwnd,
                "windowTitle": row.get("title", ""),
                "class": row.get("class_name", ""),
                "pid": row.get("pid"),
                "same_process": row.get("same_process"),
                "geometry": row.get("geometry", ""),
                "reason": reason,
            }
        )
    state.hwnd_baseline = now


class AppEventFilter(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        state = _STATE
        if state is None or not state.capturing:
            return False
        et = event.type()
        interesting = {
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.Close,
            QEvent.Type.WindowActivate,
            QEvent.Type.WindowDeactivate,
            QEvent.Type.ToolTip,
        }
        if et not in interesting:
            return False
        if not isinstance(watched, QWidget):
            return False
        cls = type(watched).__name__
        if cls in IGNORE_CLASSES:
            return False
        if et in (
            QEvent.Type.Show,
            QEvent.Type.WindowActivate,
            QEvent.Type.Hide,
            QEvent.Type.Close,
        ):
            if not (watched.isWindow() or watched.parent() is None or cls in {
                "QMessageBox",
                "QDialog",
                "QProgressDialog",
                "QTipLabel",
                "QMenu",
            }):
                # Still record parentless widgets — those can flash as HWND.
                if watched.parent() is not None and not watched.isWindow():
                    return False
        info = _widget_info(watched)
        kind = {
            QEvent.Type.Show: "SHOW",
            QEvent.Type.Hide: "HIDE",
            QEvent.Type.Close: "CLOSE",
            QEvent.Type.WindowActivate: "ACTIVATE",
            QEvent.Type.WindowDeactivate: "DEACTIVATE",
            QEvent.Type.ToolTip: "TOOLTIP",
        }[et]
        rec = {"kind": kind, "stack": _stack(), **info}
        state.emit(rec)
        if et == QEvent.Type.Show:
            emit_hwnd_diff("qt_show")
        return False


def _wrap_static(cls: type, name: str, api: str) -> None:
    state = _STATE
    if state is None:
        return
    orig = getattr(cls, name)
    state.origs[api] = orig

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        title = ""
        text = ""
        if len(args) >= 3 and isinstance(args[1], str):
            title = args[1]
            text = args[2] if isinstance(args[2], str) else str(args[2])
        elif len(args) >= 2 and isinstance(args[0], str):
            title = args[0]
            text = args[1] if isinstance(args[1], str) else str(args[1])
        if state is not None and state.capturing:
            state.emit(
                {
                    "kind": "POPUP_TRIGGER",
                    "api": api,
                    "title": title,
                    "text": text[:200],
                    "class": cls.__name__,
                    "stack": _stack(),
                }
            )
        return orig(*args, **kwargs)

    setattr(cls, name, staticmethod(wrapped) if isinstance(orig, staticmethod) else wrapped)


def _wrap_instance(cls: type, name: str, api: str) -> None:
    state = _STATE
    if state is None:
        return
    orig = getattr(cls, name)
    state.origs[api] = orig

    def wrapped(self: QWidget, *args: Any, **kwargs: Any) -> Any:
        if state is not None and state.capturing:
            info = _widget_info(self)
            state.emit(
                {
                    "kind": "POPUP_TRIGGER",
                    "api": api,
                    "title": info.get("windowTitle", ""),
                    "text": info.get("text", ""),
                    "stack": _stack(),
                    **info,
                }
            )
            if name in {"exec", "open", "show"}:
                emit_hwnd_diff(api)
        return orig(self, *args, **kwargs)

    setattr(cls, name, wrapped)


def _wrap_tooltip() -> None:
    state = _STATE
    if state is None:
        return
    orig = QToolTip.showText
    state.origs["QToolTip.showText"] = orig

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        text = ""
        if len(args) >= 2:
            text = str(args[1] or "")
        text = str(kwargs.get("text", text) or "")
        if state is not None and state.capturing:
            state.emit(
                {
                    "kind": "POPUP_TRIGGER",
                    "api": "QToolTip.showText",
                    "class": "QToolTip",
                    "title": "",
                    "text": text[:160],
                    "stack": _stack(),
                }
            )
        return orig(*args, **kwargs)

    QToolTip.showText = wrapped  # type: ignore[method-assign]


def install_recorder(app: QApplication, out_dir: Path) -> CaptureState:
    global _STATE
    out_dir.mkdir(parents=True, exist_ok=True)
    state = CaptureState(out_dir=out_dir)
    _STATE = state
    state.hwnd_baseline = snapshot_hwnds()

    filt = AppEventFilter(app)
    app.installEventFilter(filt)
    app.setProperty("_transient_window_filter", filt)

    orig_notify = app.notify

    def notify(receiver: QObject, event: QEvent) -> bool:  # noqa: N802
        global _IN_NOTIFY
        if not _IN_NOTIFY and state.capturing and event.type() == QEvent.Type.Show:
            if isinstance(receiver, QWidget):
                cls = type(receiver).__name__
                if cls not in IGNORE_CLASSES and (
                    receiver.isWindow()
                    or receiver.parent() is None
                    or cls
                    in {"QMessageBox", "QDialog", "QProgressDialog", "QTipLabel"}
                ):
                    _IN_NOTIFY = True
                    try:
                        info = _widget_info(receiver)
                        state.emit(
                            {
                                "kind": "NOTIFY_SHOW",
                                "stack": _stack(),
                                **info,
                            }
                        )
                    finally:
                        _IN_NOTIFY = False
        return orig_notify(receiver, event)

    app.notify = notify  # type: ignore[method-assign]
    state.orig_notify = orig_notify

    _wrap_static(QMessageBox, "information", "QMessageBox.information")
    _wrap_static(QMessageBox, "warning", "QMessageBox.warning")
    _wrap_static(QMessageBox, "critical", "QMessageBox.critical")
    _wrap_static(QMessageBox, "question", "QMessageBox.question")
    for cls, meth, api in (
        (QMessageBox, "exec", "QMessageBox.exec"),
        (QMessageBox, "open", "QMessageBox.open"),
        (QMessageBox, "show", "QMessageBox.show"),
        (QDialog, "exec", "QDialog.exec"),
        (QDialog, "open", "QDialog.open"),
        (QDialog, "show", "QDialog.show"),
        (QProgressDialog, "show", "QProgressDialog.show"),
        (QMainWindow, "show", "QMainWindow.show"),
    ):
        try:
            _wrap_instance(cls, meth, api)
        except Exception as exc:  # noqa: BLE001
            state.emit(
                {
                    "kind": "HOOK_FAIL",
                    "api": api,
                    "text": str(exc),
                }
            )
    _wrap_tooltip()
    orig_set_parent = QWidget.setParent
    state.origs["QWidget.setParent"] = orig_set_parent

    def _traced_set_parent(self: QWidget, parent: QObject | None) -> None:
        if (
            state.capturing
            and parent is None
            and self.isVisible()
            and self.parent() is not None
        ):
            info = _widget_info(self)
            state.emit(
                {
                    "kind": "SETPARENT_NONE_WHILE_VISIBLE",
                    "api": "QWidget.setParent(None)",
                    "stack": _stack(22),
                    **info,
                }
            )
        return orig_set_parent(self, parent)

    QWidget.setParent = _traced_set_parent  # type: ignore[method-assign]
    QFrame.setParent = _traced_set_parent  # type: ignore[method-assign]
    QDialog.setParent = _traced_set_parent  # type: ignore[method-assign]
    try:
        from ui.dependency_item_widget import DependencyItem, DependencyListHost

        DependencyItem.setParent = _traced_set_parent  # type: ignore[method-assign]
        orig_set_items = DependencyListHost.set_items

        def _traced_set_items(self, items) -> None:  # noqa: ANN001
            if state.capturing:
                state.emit(
                    {
                        "kind": "MARKER",
                        "marker": "DependencyListHost.set_items",
                        "n": len(list(items or [])),
                        "stack": _stack(16),
                    }
                )
            return orig_set_items(self, items)

        DependencyListHost.set_items = _traced_set_items  # type: ignore[method-assign]
    except Exception as exc:  # noqa: BLE001
        state.emit({"kind": "HOOK_FAIL", "api": "DependencyItem.setParent", "text": str(exc)})
    state.emit({"kind": "RECORDER_INSTALLED", "class": "CaptureState"})
    return state


def begin_capture(phase: str) -> None:
    state = _STATE
    if state is None:
        raise RuntimeError("recorder not installed")
    state.capturing = True
    state.hwnd_baseline = snapshot_hwnds()
    set_phase(phase)


def end_capture() -> None:
    state = _STATE
    if state is None:
        return
    emit_hwnd_diff("end_capture")
    set_phase("")
    state.capturing = False


def dump_summary() -> dict[str, Any]:
    state = _STATE
    if state is None:
        return {}
    events = list(state.events)
    transient = [
        e
        for e in events
        if e.get("kind")
        in {
            "SHOW",
            "HIDE",
            "NOTIFY_SHOW",
            "POPUP_TRIGGER",
            "WIN32_NEW",
            "ACTIVATE",
            "SETPARENT_NONE_WHILE_VISIBLE",
        }
        and e.get("kind") != "MARKER"
    ]
    summary = {
        "event_count": len(events),
        "transient_count": len(transient),
        "by_class": {},
        "unexpected": [
            e for e in events if e.get("classification") == "UNEXPECTED"
        ],
        "unclassified": [
            e for e in events if e.get("classification") == "UNCLASSIFIED"
        ],
    }
    counts: dict[str, int] = {}
    for e in transient:
        key = str(e.get("class") or e.get("api") or "?")
        counts[key] = counts.get(key, 0) + 1
    summary["by_class"] = counts
    path = state.out_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
