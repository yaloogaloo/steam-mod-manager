"""Level-1 only: execute exact reviewed FlowLayout, never import the ui package."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import types

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_validation_d0.qt_harness import HarnessGuard, ImportFence, QSS
from ui_validation_d0.safety import IsolationError
from ui_validation_d0.capture import geometry

REVIEWED_SHA256 = "9c6998374cc176656d1ccd31dfe4f764aca38fe6a1aac1bc8defcb96b29d87d2"

def review_source(source: bytes, expected: str):
    if expected != REVIEWED_SHA256:
        raise IsolationError("Only the fixed audited source revision is admitted")
    if hashlib.sha256(source).hexdigest() != expected:
        raise IsolationError("Reviewed source changed")
    tree = ast.parse(source.decode("utf-8-sig"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    if any(not isinstance(node, ast.ImportFrom) or node.module not in
           {"__future__", "PySide6.QtCore", "PySide6.QtWidgets"} for node in imports):
        raise IsolationError("Non-Qt source import")
    if any(not isinstance(node, (ast.Expr, ast.ImportFrom, ast.ClassDef, ast.FunctionDef))
           for node in tree.body):
        raise IsolationError("Unreviewed module initializer")
    if any(isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant)
           for node in tree.body):
        raise IsolationError("Module execution refused")
    return tree


class FlowGuard(HarnessGuard):
    def __init__(self, root, repository, source):
        super().__init__(root, repository)
        self.source = source.resolve()

    def read(self, path):
        if Path(path).resolve() == self.source:
            return self.source
        return super().read(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.agent_workspace import require_workspace
    parser.add_argument("--root", type=require_workspace, required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args(argv)
    if not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode:
        raise IsolationError("Required -I -S -B")
    repo = Path(__file__).resolve().parents[2]
    source_path = repo / "ui" / "flow_layout.py"
    guard = FlowGuard(args.root, repo, source_path)
    if guard.root.exists():
        raise IsolationError("Fresh output root required")
    # Source hash is a reviewed capability, not permission to load arbitrary Python.
    source = source_path.read_bytes()
    review_source(source, args.source_sha256)
    guard.root.mkdir(parents=True)
    for folder in ("captures", "settings", "temp", "appdata", "cache"):
        (guard.root / folder).mkdir()
    for key, folder in {"APPDATA": "appdata", "LOCALAPPDATA": "appdata", "TEMP": "temp",
                        "TMP": "temp", "XDG_CONFIG_HOME": "settings", "XDG_CACHE_HOME": "cache"}.items():
        os.environ[key] = str(guard.root / folder)
    for key in ("SMM_TEST_DB", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QML2_IMPORT_PATH", "QML_IMPORT_PATH", "QTWEBENGINEPROCESS_PATH"):
        os.environ.pop(key, None)
    os.environ["QT_QPA_PLATFORM"] = "windows"
    sys.path.insert(0, str(repo / ".venv" / "Lib" / "site-packages"))
    sys.meta_path.insert(0, ImportFence())
    guard.install()
    from PySide6.QtCore import QTimer, QCoreApplication, QEvent, QSettings, qVersion
    from PySide6.QtWidgets import QApplication, QWidget, QLabel, QFrame, QVBoxLayout, QScrollArea
    import shiboken6

    # Execute original bytes with original traceback path; no copied layout algorithm,
    # no sys.modules['ui'] substitution, no package initializer or business imports.
    module = types.ModuleType("_reviewed_smm_flow_layout")
    module.__file__ = str(source_path)
    exec(compile(source, str(source_path), "exec"), module.__dict__)
    app = QApplication(["SMM-Flow-only"])
    app.setOrganizationName("SMM_UI_VALIDATION_D04")
    app.setApplicationName("FlowOnlyHarness")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(QSS)
    settings = QSettings(str(guard.root / "settings" / "flow.ini"), QSettings.Format.IniFormat)
    settings.setFallbacksEnabled(False)
    settings.setValue("synthetic/level", 1)
    settings.sync()
    window = QWidget()
    window.setObjectName("flowOnlyRoot")
    window.setWindowTitle("Real FlowLayout / synthetic children")
    window.setMinimumSize(480, 320)
    outer = QVBoxLayout(window)
    title = QLabel("真实 FlowLayout · 合成子控件 · 无 SMM 业务", window)
    outer.addWidget(title)
    scroll = QScrollArea(window)
    scroll.setWidgetResizable(True)
    host = QWidget()
    flow = module.FlowLayout(host, margin=12, h_spacing=12, v_spacing=12)
    children = []
    for index in range(30):
        child = QFrame(host)
        child.setObjectName("syntheticTile" + str(index))
        child.setFixedSize(200, 100)
        body = QVBoxLayout(child)
        label = QLabel("合成项目 " + str(index + 1), child)
        label.setWordWrap(True)
        body.addWidget(label)
        child.setStyleSheet("QFrame {border: 1px solid #6289ad; background: #365d86;}")
        flow.addWidget(child)
        children.append(child)
    scroll.setWidget(host)
    outer.addWidget(scroll)
    heights = []
    flow.heightChanged.connect(heights.append)
    records = []
    failures = []
    scenes = [("wide", 1200, 700), ("narrow", 700, 550), ("wide_again", 1200, 700),
              ("scrolled", 700, 550)]

    def rect(value):
        return [value.x(), value.y(), value.width(), value.height()]

    def advance():
        if len(records) == len(scenes):
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.quit()
            return
        name, width, height = scenes[len(records)]
        window.resize(width, height)
        window.show()
        if name == "scrolled":
            QTimer.singleShot(300, lambda: scroll.verticalScrollBar().setValue(
                scroll.verticalScrollBar().maximum() // 2))
        QTimer.singleShot(600, capture)

    def capture():
        try:
            name, width, height = scenes[len(records)]
            pixmap = window.grab()
            image = guard.root / "captures" / (name + ".png")
            if not pixmap.save(str(image)):
                raise RuntimeError("Screenshot save failed")
            positions = [rect(child.geometry()) for child in children]
            rows = {}
            for position in positions:
                rows.setdefault(str(position[1]), []).append(position)
            spacing = [row[i + 1][0] - row[i][0] - row[i][2]
                       for row in rows.values() for i in range(len(row) - 1)]
            screen = app.primaryScreen()
            record = {"scenario": name, "requested": [width, height],
                      "actual_logical": [window.width(), window.height()],
                      "screenshot_pixels": [pixmap.width(), pixmap.height()],
                      "dpr": window.devicePixelRatioF(), "platform": app.platformName(),
                      "screen_geometry": rect(screen.geometry()),
                      "screen_available": rect(screen.availableGeometry()),
                      "screen_dpi": [screen.logicalDotsPerInch(), screen.physicalDotsPerInch()],
                      "host": rect(host.geometry()), "scroll": rect(scroll.geometry()),
                      "rows": rows, "columns": max(map(len, rows.values())),
                      "horizontal_spacing": spacing, "height_for_width": flow.heightForWidth(host.width()),
                      "size_hint": [flow.sizeHint().width(), flow.sizeHint().height()],
                      "height_signals": list(heights), "scroll_max": scroll.verticalScrollBar().maximum(),
                      "scroll_value": scroll.verticalScrollBar().value(),
                      "widget_tree": geometry(window)}
            if flow.count() != 30 or not all(value == 12 for value in spacing):
                raise RuntimeError("Item count/spacing invariant")
            if any(a.intersects(b) for i, a in enumerate([c.geometry() for c in children])
                   for b in [c.geometry() for c in children][i + 1:]):
                raise RuntimeError("Overlapping child geometry")
            (guard.root / "captures" / (name + ".json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            records.append(record)
            QTimer.singleShot(50, advance)
        except Exception as exc:
            failures.append(repr(exc))
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.quit()

    QTimer.singleShot(0, advance)
    QTimer.singleShot(15000, app.quit)
    app.exec()
    forbidden = [name for name in sys.modules if name.split(".")[0] in {"ui", "core", "services", "main"}]
    deleted = not shiboken6.isValid(window) and not shiboken6.isValid(host) and not shiboken6.isValid(flow)
    checks = {"four_scenes": len(records) == 4, "deleted": deleted,
              "no_business_modules": not forbidden, "settings_ok": settings.status() == QSettings.Status.NoError,
              "wrap_changed": len(records) == 4 and records[0]["columns"] > records[1]["columns"],
              "resize_reversible": len(records) == 4 and records[0]["rows"] == records[2]["rows"],
              "scroll_moved": len(records) == 4 and records[3]["scroll_value"] > 0}
    result = {"pid": os.getpid(), "source": str(source_path), "source_sha256": args.source_sha256,
              "qt": qVersion(), "checks": checks, "failures": failures, "forbidden_modules": forbidden,
              "records": records, "scope": "Level 1 only; synthetic QSS/children; no OS sandbox"}
    (guard.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pid": os.getpid(), "checks": checks, "failures": failures}))
    return 0 if all(checks.values()) and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
