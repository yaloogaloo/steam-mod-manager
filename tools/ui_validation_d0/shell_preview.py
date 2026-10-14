"""Admitted pure P3 Shell capture. No production application or business imports.

Launch with -I -S -B and three fresh stable samples from the external baseline.
Native Qt is not an OS sandbox; this closed component graph supplies no user
resource paths and imports no native network, SQL, process or business APIs.
"""
import argparse
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--platform", choices=("windows", "offscreen"), default="windows")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("Use -I -S -B")
    if os.name != "nt":
        raise RuntimeError("This process preflight is Windows-only")
    # Do not execute the application to discover whether it is running.
    command = ("@(Get-CimInstance Win32_Process | Where-Object { "
               "$_.Name -match '^(pythonw?|smm).*\\.exe$' -and "
               "$_.CommandLine -match 'steam-mod-manager' -and "
               "$_.CommandLine -notmatch 'shell_preview.py' "
               "} | Select-Object ProcessId,Name) | ConvertTo-Json -Compress")
    running = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True)
    if running.strip() not in {"", "[]", "null"}:
        raise RuntimeError("Production/unknown SMM process remains; refuse Qt startup")
    samples = [json.loads((args.baseline / f"stable_{i}.json").read_text("utf-8")) for i in (1, 2, 3)]
    times = [datetime.fromisoformat(x["time"]) for x in samples]
    if any((b - a).total_seconds() < 30 for a, b in zip(times, times[1:])):
        raise RuntimeError("Baseline requires >=30 seconds between samples")
    if not 0 <= (datetime.now().astimezone() - times[-1]).total_seconds() <= 900:
        raise RuntimeError("Baseline is stale")
    if any(x["persistent"] != samples[0]["persistent"] for x in samples[1:]):
        raise RuntimeError("Unstable production baseline")
    for name, record in samples[-1]["persistent"].items():
        path = repository / name
        if path.exists() != record["exists"]:
            raise RuntimeError("Existence changed after baseline")
        if "sha256" in record:
            stat = path.stat()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if (digest != record["sha256"] or stat.st_size != record["size"]
                    or stat.st_mtime_ns != record["mtime_ns"] or record["unstable_read"]):
                raise RuntimeError("Production file changed after baseline")

    sys.path.insert(0, str(repository / "tools"))
    from ui_validation_d0.safety import Boundary, IsolationError
    from ui_validation_d0.capture import geometry

    class ShellBoundary(Boundary):
        def read(self, path):
            resolved = Path(path).resolve()
            allowed = (self.root, repository / "frontend", repository / "tools/ui_validation_d0",
                       repository / ".venv/Lib/site-packages/PySide6",
                       repository / ".venv/Lib/site-packages/shiboken6",
                       Path(sys.prefix), Path(sys.base_prefix))
            if any(resolved.is_relative_to(p.resolve()) for p in allowed):
                return resolved
            raise IsolationError("Read outside closed presentation/runtime graph")

        def audit(self, event, data):
            if event == "sqlite3.connect":
                raise IsolationError("No SQLite in Shell preview")
            super().audit(event, data)

    class Fence(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            base = fullname.split(".")[0]
            if base == "org":
                # copy.py probes Jython and explicitly handles ImportError.
                # Refuse this optional dependency without permitting its load.
                raise ModuleNotFoundError("Jython is unavailable in this CPython preview")
            allowed = sys.stdlib_module_names | {"frontend", "ui_validation_d0", "PySide6", "shiboken6"}
            if base not in allowed or fullname.startswith(("PySide6.QtNetwork", "PySide6.QtSql", "PySide6.QtWeb")):
                raise IsolationError("Unadmitted import: " + fullname)
            if base == "frontend" and fullname not in {"frontend", "frontend.models", "frontend.components", "frontend.theme", "frontend.shell"}:
                raise IsolationError("Unknown presentation module")
            return None

    boundary = ShellBoundary(args.root, repository)
    if boundary.root.exists():
        raise RuntimeError("Use a new capture directory")
    boundary.root.mkdir(parents=True)
    for folder in ("captures", "settings", "temp", "appdata", "localappdata", "cache"):
        boundary.target(boundary.root / folder).mkdir()
    for variable, folder in {"APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "TEMP": "temp",
                             "TMP": "temp", "XDG_CACHE_HOME": "cache", "XDG_CONFIG_HOME": "settings"}.items():
        os.environ[variable] = str(boundary.root / folder)
    for key in ("SMM_TEST_DB", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
        os.environ.pop(key, None)
    os.environ["QT_QPA_PLATFORM"] = args.platform
    sys.path[:0] = [str(repository), str(repository / ".venv/Lib/site-packages")]
    sys.meta_path.insert(0, Fence())
    boundary.install()
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QScrollArea, QMenu
    from PySide6.QtTest import QTest
    from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
    from frontend.models import GameContext
    from frontend.shell import WorkbenchWindow
    from frontend.theme import stylesheet
    import shiboken6

    app = QApplication(["SMM-P3-Presentation-Preview"])
    app.setOrganizationName("SMM_P3_SYNTHETIC_ONLY")
    app.setApplicationName("WorkbenchPreview")
    app.setQuitOnLastWindowClosed(False)
    loaded_font_families = []
    if args.platform == "offscreen":
        # Offscreen on Windows does not discover the system font catalog.
        # Native Qt reads only this explicit OS font, never an SMM resource.
        font_id = QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\msyh.ttc")
        if font_id < 0:
            raise RuntimeError("System Chinese font unavailable; refuse unreadable capture")
        loaded_font_families = QFontDatabase.applicationFontFamilies(font_id)
        QFont.insertSubstitutions("Segoe UI", loaded_font_families)
        QFont.insertSubstitutions("Microsoft YaHei UI", loaded_font_families)
        app.setFont(QFont(loaded_font_families[0], 10))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(boundary.root / "settings"))
    # No QSettings instance is constructed by the product graph or this runner.
    window = WorkbenchWindow([GameContext("synthetic-a", "群星 / 合成游戏", "SYNTHETIC-001"),
                              GameContext("synthetic-b", "战锤 III / 合成游戏", "SYNTHETIC-002")])
    records, failures = [], []
    scenes = [(w, h, key) for w, h in ((1920, 1080), (1520, 900)) for key in window.workspaces]

    def advance():
        if not scenes:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if shiboken6.isValid(window):
                failures.append("Window not deleted")
            app.quit()
            return
        width, height, key = scenes.pop(0)
        window.resize(width, height)
        window.show()
        QTest.mouseClick(window.nav_buttons[key], Qt.MouseButton.LeftButton)

        def capture():
            try:
                page = window.workspaces[key]
                assert window.stack.currentWidget() is page and window.nav_buttons[key].isChecked(), "navigation"
                assert not page.header.primary_action.isEnabled(), "unbound business action"
                assert window.styleSheet() == stylesheet(), "window stylesheet"
                assert not window.findChildren(QScrollArea), "outer scroll area"
                QTest.mouseClick(page.header.help_button, Qt.MouseButton.LeftButton)
                assert page.help_text.isVisible(), "help opened"
                QTest.mouseClick(page.header.help_button, Qt.MouseButton.LeftButton)
                assert not page.help_text.isVisible(), "help closed"
                page.tabs.setCurrentIndex(1)
                assert page.section_title.text() == page.spec.tabs[1], "workspace tab"
                page.tabs.setCurrentIndex(0)
                window.game_selector.setCurrentIndex(1)
                assert "SYNTHETIC-002" in window.context_info.text(), "game context"
                window.game_selector.setCurrentIndex(0)
                page.header.help_button.setFocus()
                assert page.header.help_button.hasFocus(), "keyboard focus"
                assert QFontMetrics(page.header.title.font()).inFontUcs4(ord("资")), "Chinese glyph missing"
                if key == "library":
                    menu = window.findChildren(QMenu)[0]
                    menu.popup(window.mapToGlobal(window.rect().center()))
                    QTest.qWait(60)
                    assert menu.isVisible() and all(not item.isEnabled() for item in menu.actions()), "preview menu"
                    assert menu.grab().save(str(boundary.root / "captures" / f"menu_{width}.png"))
                    menu.close()
                    page.search.setText("合成查询")
                    window.navigate("deploy")
                    window.navigate("library")
                    assert page.search.text() == "合成查询", "retained workspace state"
                    window.game_selector.setCurrentIndex(1)
                    assert not page.search.text(), "game-scoped query reset"
                    window.game_selector.setCurrentIndex(0)
                    window.game_selector.showPopup()
                    QTest.qWait(60)
                    assert window.game_selector.view().isVisible(), "native game popup"
                    window.game_selector.hidePopup()
                    page.header.help_button.setFocus()
                image = window.grab()
                name = f"{key}_{width}x{height}"
                assert image.save(str(boundary.target(boundary.root / "captures" / (name + ".png"))))
                record = {"scene": name, "requested": [width, height], "actual": [window.width(), window.height()],
                          "pixels": [image.width(), image.height()], "dpr": window.devicePixelRatioF(),
                          "platform": app.platformName(), "screen":
                          [window.screen().size().width(), window.screen().size().height()],
                          "header_geometry": [page.header.x(), page.header.y(), page.header.width(), page.header.height()],
                          "geometry": geometry(window)}
                records.append(record)
                assert record["actual"] == record["requested"], f"actual {record['actual']} != requested {record['requested']}"
            except Exception as error:
                failures.append(repr(error))
                window.grab().save(str(boundary.root / "failure.png"))
                scenes.clear()
            QTimer.singleShot(60, advance)
        QTimer.singleShot(350, capture)

    QTimer.singleShot(0, advance)
    QTimer.singleShot(20000, app.quit)
    app.exec()
    forbidden = [m for m in sys.modules if m.split(".")[0] in {"ui", "main", "core", "services"}]
    report = {"records": records, "failures": failures, "forbidden_modules": forbidden,
              "namespace": [app.organizationName(), app.applicationName()], "settings_instances": 0,
              "visible_windows_after": sum(w.isVisible() for w in app.topLevelWidgets()), "pid": os.getpid(),
              "explicit_system_font_families": loaded_font_families,
              "native_boundary": "Closed reviewed pure Qt graph, NOT OS sandbox", "baseline_times": [s["time"] for s in samples]}
    for width in (1920, 1520):
        headers = [tuple(r["header_geometry"]) for r in records if r["requested"][0] == width]
        if len(headers) != 5 or len(set(headers)) != 1:
            failures.append("Header geometry differs across workspaces")
    (boundary.root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"captures": len(records), "failures": failures, "forbidden": forbidden}))
    return 0 if len(records) == 10 and not failures and not forbidden else 1


if __name__ == "__main__":
    raise SystemExit(main())
