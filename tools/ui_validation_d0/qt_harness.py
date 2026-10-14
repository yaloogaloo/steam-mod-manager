"""Pure Qt visual harness. NOT an SMM page runner or native OS sandbox."""
from __future__ import annotations

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ui_validation_d0.safety import Boundary, IsolationError
    from ui_validation_d0.capture import geometry
else:
    from .safety import Boundary, IsolationError
    from .capture import geometry

SCENARIOS = ("default", "minimum", "long_text", "many_controls", "scroll")
QSS = """
QWidget { background: #20242b; color: #eef1f5; font-size: 14px; }
QLabel#heading { font-size: 22px; font-weight: 600; padding: 8px; }
QPushButton { background: #365d86; border: 1px solid #6289ad;
              border-radius: 5px; padding: 10px; }
QScrollArea { border: 1px solid #66717e; }
"""


class ImportFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        first = fullname.split(".")[0]
        if first in {"ui", "core", "services", "main", "tests"}:
            raise IsolationError("SMM module import refused: " + fullname)
        if first not in sys.stdlib_module_names | {"PySide6", "shiboken6", "ui_validation_d0"}:
            raise IsolationError("Non-harness dependency refused: " + fullname)
        if first == "ui_validation_d0" and fullname not in {
            "ui_validation_d0", "ui_validation_d0.qt_harness", "ui_validation_d0.safety", "ui_validation_d0.capture"
        }:
            raise IsolationError("Unapproved infrastructure import refused: " + fullname)
        return None


class HarnessGuard(Boundary):
    """Python I/O guard, complementing closed-source-path Qt construction.

    Native Qt reads aren't sandboxed. Harness code supplies no external native
    resource paths, URLs, DB connections, workers or default/native QSettings.
    """
    def read(self, path):
        candidate = Path(path).resolve()
        permitted = (self.root, self.repository / "tools" / "ui_validation_d0",
                     self.repository / ".venv" / "Lib" / "site-packages" / "PySide6",
                     self.repository / ".venv" / "Lib" / "site-packages" / "shiboken6",
                     Path(sys.prefix), Path(sys.base_prefix))
        if any(candidate.is_relative_to(root.resolve()) for root in permitted):
            return candidate
        raise IsolationError("Non-runtime/non-harness read refused")

    def audit(self, event, args):
        if event == "sqlite3.connect":
            raise IsolationError("No database connections in pure Qt harness")
        super().audit(event, args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--width", default=1600, type=int)
    parser.add_argument("--height", default=900, type=int)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    options = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[2]
    if not sys.flags.isolated or not sys.flags.no_site:
        raise IsolationError("Launch with -I -S -B: no site/.pth/user-site/PYTHONPATH startup")
    # Python 3.11 -S skips venv site configuration. Explicitly expose only the
    # known installed dependency location; ImportFence restricts its packages.
    sys.path.insert(0, str(repository / ".venv" / "Lib" / "site-packages"))
    guard = HarnessGuard(options.root, repository)
    if options.width < 1 or options.height < 1:
        raise IsolationError("Positive sizes required")
    if guard.root.exists():
        raise IsolationError("Use a new external capture root; never overwrite evidence")
    guard.root.mkdir(parents=True)
    for folder in ("captures", "settings", "temp", "appdata", "localappdata", "cache"):
        guard.target(guard.root / folder).mkdir()
    for name, folder in {"APPDATA": "appdata", "LOCALAPPDATA": "localappdata",
                         "TEMP": "temp", "TMP": "temp", "XDG_CACHE_HOME": "cache",
                         "XDG_CONFIG_HOME": "settings"}.items():
        os.environ[name] = str(guard.root / folder)
    os.environ.pop("SMM_TEST_DB", None)
    for variable in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH",
                     "QML_IMPORT_PATH", "QTWEBENGINEPROCESS_PATH"):
        os.environ.pop(variable, None)
    os.environ["QT_QPA_PLATFORM"] = "windows" if os.name == "nt" else "xcb"
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, ImportFence())
    guard.install()
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer, qVersion
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea

    app = QApplication(["SMM-Pure-Qt-Harness"])
    app.setOrganizationName("SMM_UI_VALIDATION_D03")
    app.setApplicationName("PureQtHarness")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(QSS)
    settings_path = guard.target(guard.root / "settings" / "harness.ini")
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    settings.setFallbacksEnabled(False)
    settings.setValue("synthetic/check", "harness-only")
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        raise IsolationError("Explicit harness INI sync failed")

    records = []
    failures = []
    windows = []
    scenes = list(SCENARIOS if options.scenario == "all" else [options.scenario])
    index = 0

    def rect(value):
        return [value.x(), value.y(), value.width(), value.height()]

    def advance():
        nonlocal index
        if index >= len(scenes):
            app.quit()
            return
        scenario = scenes[index]
        index += 1
        window = QWidget()
        windows.append(window)
        window.setObjectName("pureQtRoot")
        window.setWindowTitle("Synthetic Qt validation / " + scenario)
        window.setMinimumSize(480, 320)
        layout = QVBoxLayout(window)
        title = QLabel("真实 Qt 基础验证 · " + scenario, window)
        title.setObjectName("heading")
        layout.addWidget(title)
        scroll = QScrollArea(window)
        scroll.setObjectName("syntheticScroll")
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("syntheticContent")
        body = QVBoxLayout(host)
        count = 65 if scenario in {"many_controls", "scroll"} else 3
        for number in range(count):
            text = "合成文本 Synthetic content " + str(number + 1)
            if scenario == "long_text":
                text += " · 这是用于验证换行的合成长文本。" * 65
            label = QLabel(text, host)
            label.setObjectName("syntheticLabel" + str(number))
            label.setWordWrap(True)
            body.addWidget(label)
            if scenario == "many_controls":
                button = QPushButton("Synthetic button " + str(number), host)
                body.addWidget(button)
        body.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        button = QPushButton("合成操作 · No SMM action", window)
        button.setObjectName("syntheticButton")
        layout.addWidget(button)
        requested = [480, 320] if scenario == "minimum" else [options.width, options.height]
        window.resize(*requested)
        window.show()

        def capture_scene():
            try:
                if scenario == "scroll":
                    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum() // 2)
                image = window.grab()
                destination = guard.target(guard.root / "captures" / (scenario + ".png"))
                if image.isNull() or not image.save(str(destination), "PNG"):
                    raise IsolationError("Real QWidget screenshot failed")
                screen = window.screen()
                record = {
                    "scenario": scenario, "requested_window_size": requested,
                    "actual_logical_size": [window.width(), window.height()],
                    "screenshot_pixel_size": [image.width(), image.height()],
                    "device_pixel_ratio": window.devicePixelRatioF(),
                    "screenshot_device_pixel_ratio": image.devicePixelRatioF(),
                    "qt_platform_plugin": app.platformName(), "qt_version": qVersion(),
                    "screen_geometry": rect(screen.geometry()),
                    "screen_available_geometry": rect(screen.availableGeometry()),
                    "screen_logical_dpi": screen.logicalDotsPerInch(),
                    "screen_physical_dpi_reported": screen.physicalDotsPerInch(),
                    "scroll_area_geometry": rect(scroll.geometry()),
                    "scroll_range": [scroll.verticalScrollBar().minimum(), scroll.verticalScrollBar().maximum()],
                    "scroll_value": scroll.verticalScrollBar().value(),
                    "widget_geometry": geometry(window), "visible_before_close": window.isVisible(),
                    "qss_equals_expected": app.styleSheet() == QSS,
                    "button_palette_background": button.palette().button().color().name(),
                    "button_clicked": False, "screenshot": str(destination),
                    "dpi_validation": "NOT CLOSED: observed environment only; no system DPI change",
                }
                def clicked():
                    record["button_clicked"] = True
                button.clicked.connect(clicked)
                button.click()
                window.close()
                record["visible_after_close"] = window.isVisible()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                import shiboken6
                record["deleted_after_close"] = not shiboken6.isValid(window)
                records.append(record)
                (guard.root / "captures" / (scenario + ".geometry.json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"scenario": scenario, "captured": True, "actual_size": record["actual_logical_size"], "dpr": record["device_pixel_ratio"]}), flush=True)
            except Exception as exc:
                failures.append(str(exc))
                app.quit()
                return
            QTimer.singleShot(80, advance)
        QTimer.singleShot(500, capture_scene)

    QTimer.singleShot(0, advance)
    QTimer.singleShot(20000, app.quit)
    exit_code = app.exec()
    forbidden = [name for name in sys.modules if name.split(".")[0] in {"ui", "core", "services", "main"}]
    report = {"pid": os.getpid(), "qt_exit_code": exit_code, "records": records,
              "failures": failures, "forbidden_modules": forbidden,
              "organization": app.organizationName(), "application": app.applicationName(),
              "settings_path": str(settings_path), "settings_fallbacks": settings.fallbacksEnabled(),
              "all_windows_closed": not any(item.isVisible() for item in app.topLevelWidgets()),
              "requested_scenarios": scenes, "qss": QSS,
              "isolated_interpreter": bool(sys.flags.isolated), "no_site": bool(sys.flags.no_site),
              "qt_library_paths": app.libraryPaths(),
              "scale_environment": {key: os.environ.get(key) for key in ("QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_FONT_DPI")},
              "scope": "Pure synthetic Qt only; not native sandbox/SMM launch evidence"}
    (guard.root / "harness_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failures and not forbidden and len(records) == len(scenes) and report["all_windows_closed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
