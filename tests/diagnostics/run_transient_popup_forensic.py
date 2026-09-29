"""Diagnostic UI capture against an explicit temporary library only.

Refuses to start unless FORENSIC_FIXTURE_ROOT and FORENSIC_DATA_ROOT are set.
It does not read the production library, does not open the production
database, and does not pick a card from the user's real library.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SMM_UI_TRACE"] = "1"

from PySide6.QtCore import QEventLoop, QSettings, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_STEAM, normalize_platform
from services.mutation_context import (
    MutationBoundaryError,
    activate_test_context,
    reset_mutation_context,
)
from tests.diagnostics.transient_window_capture import (
    begin_capture,
    dump_summary,
    end_capture,
    install_recorder,
    mark,
    set_phase,
)
from ui.library_view import ALL_GAMES_LABEL, GAME_ID_ROLE
from ui.main_window import APP_NAME, ORG_NAME, PAGE_LIBRARY, SETTING_TARGET, MainWindow
from ui.styles import APP_STYLE, apply_dark_palette
from ui.window_chrome import TITLE_BAR_STYLE, DarkTitleBarFilter, apply_application_icon
from ui.widget_show_trace import install_widget_show_trace
from ui.window_lifecycle import install_window_ownership_guard

OUT = ROOT / "_tmp" / "transient_popup_forensic"


def _pump(app: QApplication, ms: int = 50) -> None:
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()


def _wait_signal(app: QApplication, signal, timeout_ms: int) -> bool:
    loop = QEventLoop()
    got = {"ok": False}

    def _ok(*_a) -> None:
        got["ok"] = True
        loop.quit()

    signal.connect(_ok)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    try:
        signal.disconnect(_ok)
    except Exception:  # noqa: BLE001
        pass
    app.processEvents()
    return bool(got["ok"])


def _wait_until(app: QApplication, pred, timeout_ms: int) -> bool:
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        if pred():
            return True
        app.processEvents()
    return pred()


def _write_cover_png(path: Path) -> None:
    img = QImage(16, 16, QImage.Format.Format_RGB32)
    img.fill(0xFF2A6FDB)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG")


def _select_any_game(view) -> bool:
    if view.game_list.count() <= 0:
        return False
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        name = item.text() if item is not None else ""
        if name and name != ALL_GAMES_LABEL:
            view.game_list.setCurrentItem(item)
            return True
    view.game_list.setCurrentRow(0)
    return True


def _visible_cards(view):
    cards = []
    for card in getattr(view, "_cards", []) or []:
        try:
            if card is not None and not card.isHidden():
                cards.append(card)
        except RuntimeError:
            continue
    return cards


def require_temporary_roots() -> tuple[Path, Path]:
    """Fail closed. Never fall back to the current library or the first card."""
    fixture = os.environ.get("FORENSIC_FIXTURE_ROOT", "").strip()
    data = os.environ.get("FORENSIC_DATA_ROOT", "").strip()
    if not fixture or not data:
        raise MutationBoundaryError(
            "REFUSE: forensic mutation requires FORENSIC_FIXTURE_ROOT and "
            "FORENSIC_DATA_ROOT. There is no fallback to the current library."
        )
    return Path(fixture), Path(data)


def _stub_file_dialog(png: Path):
    orig = QFileDialog.getOpenFileName

    def _open(*_a, **_k):
        mark("QFileDialog.getOpenFileName.STUB", path=str(png))
        return str(png), "Images (*.png *.jpg *.jpeg *.jfif *.webp)"

    QFileDialog.getOpenFileName = staticmethod(_open)  # type: ignore[method-assign]
    import ui.mod_detail_panel as mdp

    mdp.QFileDialog.getOpenFileName = staticmethod(_open)  # type: ignore[method-assign]
    return orig


_PATH_REBIND = (
    ("core.paths", "data_dir", "data"),
    ("core.paths", "database_path", "db"),
    ("core.paths", "default_mod_library", "library"),
    ("core.db_manager", "database_path", "db"),
    ("services.metadata_backup", "data_dir", "data"),
    ("services.mod_path_validation", "data_dir", "data"),
    ("services.mod_path_validation", "default_mod_library", "library"),
    ("services.metadata_backup_sync", "default_mod_library", "library"),
    ("services.library_reconcile", "default_mod_library", "library"),
    ("services.library_maintenance", "data_dir", "data"),
    ("services.library_maintenance", "default_mod_library", "library"),
    ("services.importers.archive", "data_dir", "data"),
)


def _bind_temporary_paths(fixture: Path, data: Path):
    """Point path helpers at the fixture. Do not mkdir the production library."""
    import importlib

    data.mkdir(parents=True, exist_ok=True)
    fixture.mkdir(parents=True, exist_ok=True)
    db_file = data / "mod_manager.db"

    def _data_dir() -> Path:
        return data

    def _database_path() -> Path:
        return db_file

    def _default_library() -> Path:
        return fixture

    impl = {"data": _data_dir, "db": _database_path, "library": _default_library}
    saved: list[tuple[object, str, object]] = []
    for module_name, attr, kind in _PATH_REBIND:
        module = importlib.import_module(module_name)
        if not hasattr(module, attr):
            continue
        saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, impl[kind])
    token = activate_test_context([fixture, data])
    DatabaseManager.reset_instance()
    DatabaseManager.instance(db_file)
    return token, saved


def _restore_temporary_paths(saved) -> None:
    for module, attr, previous in reversed(saved):
        setattr(module, attr, previous)


def _pin_settings_library(fixture: Path):
    """Keep QSettings from reading or saving the production library root."""
    orig_value = QSettings.value
    orig_set = QSettings.setValue

    def _value(self, key, default=None, *args, **kwargs):  # noqa: ANN001
        if str(key) == SETTING_TARGET:
            return str(fixture)
        return orig_value(self, key, default, *args, **kwargs)

    def _set(self, key, value):  # noqa: ANN001
        if str(key) == SETTING_TARGET:
            return None
        return orig_set(self, key, value)

    QSettings.value = _value  # type: ignore[method-assign]
    QSettings.setValue = _set  # type: ignore[method-assign]
    return orig_value, orig_set


def _seed_fixture_mod(db, library: Path) -> str:
    from core.game_info import GameInfo
    from tests.helpers.identity import seed_steam_managed_mod

    db.upsert_game(GameInfo(app_id=1, name="FixtureGame", folder_name="FixtureGame"))
    seeded = seed_steam_managed_mod(
        db,
        library,
        external_id="910099001",
        title="Forensic Fixture Mod",
        game_folder="FixtureGame",
        app_id=1,
        game_name="FixtureGame",
        files={"payload.txt": b"fixture"},
    )
    return str(seeded.entity_internal_id)


def main() -> int:
    try:
        fixture, data = require_temporary_roots()
    except MutationBoundaryError as exc:
        print(str(exc))
        return 2
    token, saved_paths = _bind_temporary_paths(fixture, data)
    try:
        return _main_in_fixture(fixture, data)
    finally:
        _restore_temporary_paths(saved_paths)
        DatabaseManager.reset_instance()
        reset_mutation_context(token)


def _main_in_fixture(fixture: Path, data: Path) -> int:
    del data
    db = DatabaseManager.instance()
    target = fixture
    os.environ["FORENSIC_INTERNAL_ID"] = _seed_fixture_mod(db, target)
    settings_orig = _pin_settings_library(target)
    try:
        return _run_fixture_window(target)
    finally:
        QSettings.value, QSettings.setValue = settings_orig


def _run_fixture_window(target: Path) -> int:
    report = {
        "library": str(target),
        "scenario_a": {},
        "scenario_b": {},
    }

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    apply_application_icon(app)
    install_window_ownership_guard(app)
    install_widget_show_trace(app)
    app.setStyleSheet(APP_STYLE + "\n" + TITLE_BAR_STYLE)
    chrome = DarkTitleBarFilter(app)
    app.installEventFilter(chrome)
    app.setProperty("_dark_titlebar_filter", chrome)

    rec = install_recorder(app, OUT)
    rec.capturing = True
    set_phase("BOOT")

    window = MainWindow()
    window.show()
    _pump(app, 200)
    window._goto_page(PAGE_LIBRARY)
    _pump(app, 200)
    view = window.library_view
    view.set_target_root(str(target))
    view.refresh(force=False)
    _pump(app, 400)
    _select_any_game(view)
    _pump(app, 400)
    forced = os.environ.get("FORENSIC_INTERNAL_ID", "").strip()
    if not forced:
        print("REFUSE: fixture mod was not created")
        return 2
    def _card_internal(card) -> str:
        fn = getattr(card, "_entity_internal_id", None)
        if not callable(fn):
            return ""
        try:
            return str(fn() or "")
        except Exception:  # noqa: BLE001
            return ""

    ok_cards = _wait_until(
        app,
        lambda: any(_card_internal(card) == forced for card in _visible_cards(view)),
        25000,
    )
    folder = target / "FixtureGame" / "Forensic Fixture Mod"
    mid = forced if ok_cards else ""
    if not mid or not folder.is_dir():
        end_capture()
        dump_summary()
        report["error"] = "fixture mod was not visible; refusing to pick another card"
        (OUT / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("FORENSIC FAIL: fixture card missing; no production fallback")
        return 2
    panel = view.detail_panel
    view.on_mod_selected(mid)
    _pump(app, 250)
    plat = PLATFORM_STEAM
    report["mod"] = {
        "internal_id": mid,
        "folder": str(folder),
        "platform": plat,
        "panel_platform_after_select": str(getattr(panel, "_current_platform", "")),
        "temporary": True,
    }
    mark(
        "MOD_SELECTED",
        internal_id=mid,
        folder=str(folder),
        panel_platform=str(getattr(panel, "_current_platform", "")),
    )

    png = OUT / "forensic_cover.png"
    _write_cover_png(png)
    orig_file_dialog = _stub_file_dialog(png)
    orig_warning = QMessageBox.warning

    def _warning(*args, **kwargs):  # noqa: ANN001
        mark("QMessageBox.warning.STUB", detail=str(args)[:240])
        return QMessageBox.StandardButton.Ok

    QMessageBox.warning = staticmethod(_warning)  # type: ignore[method-assign]
    import ui.mod_detail_panel as mdp

    mdp.QMessageBox.warning = staticmethod(_warning)  # type: ignore[method-assign]
    skip_a = os.environ.get("FORENSIC_SKIP_A", "").strip() in {"1", "true", "yes"}

    # ---- Scenario A: cover then refresh ----
    if not skip_a:
        begin_capture("A_COVER")
        t_a0 = time.time()
        mark("A_COVER_START")
        panel.btn_change_cover.click()
        _pump(app, 1500)
        mark("A_COVER_SLOT_RETURNED")
        report["scenario_a"]["cover_slot_returned"] = True
        report["scenario_a"]["cover_start"] = t_a0

        set_phase("A_REFRESH")
        mark("A_REFRESH_START")
        finished = {"ok": False, "fail": False, "result": None}

        def _fin(result=None) -> None:
            finished["ok"] = True
            finished["result"] = result
            mark("A_REFRESH_FINISHED_SIGNAL")

        def _fail(err="") -> None:
            finished["fail"] = True
            finished["result"] = err
            mark("A_REFRESH_FAILED_SIGNAL", error=str(err))

        panel._on_refresh_mod()
        _pump(app, 200)
        worker = getattr(panel, "_metadata_worker", None)
        if worker is not None:
            worker.refresh_finished.connect(_fin)
            worker.refresh_failed.connect(_fail)
            refresh_timeout = int(os.environ.get("FORENSIC_REFRESH_TIMEOUT_MS", "90000") or "90000")
            _wait_signal(app, worker.finished, refresh_timeout)
            _pump(app, 2200)
        else:
            _pump(app, 2200)
        mark("A_REFRESH_WAIT_DONE", ok=finished["ok"], fail=finished["fail"])
        report["scenario_a"]["refresh_success"] = bool(finished["ok"]) and not finished["fail"]
        report["scenario_a"]["refresh_failed"] = bool(finished["fail"])
        report["scenario_a"]["refresh_detail"] = str(finished["result"] or "")[:300]
        report["scenario_a"]["end"] = time.time()
        end_capture()
    else:
        report["scenario_a"]["skipped"] = True
    QFileDialog.getOpenFileName = orig_file_dialog
    import ui.mod_detail_panel as mdp

    mdp.QFileDialog.getOpenFileName = orig_file_dialog
    QMessageBox.warning = orig_warning  # type: ignore[method-assign]
    mdp.QMessageBox.warning = orig_warning  # type: ignore[method-assign]
    _pump(app, 200)
    if not skip_a:
        view.on_mod_selected(mid)
        _pump(app, 400)

    # ---- Scenario B: save webpage (Steam worker, no file dialog) ----
    skip_b = os.environ.get("FORENSIC_SKIP_B", "").strip() in {"1", "true", "yes"}
    if skip_b:
        report["scenario_b"]["skipped"] = True
        summary = dump_summary()
        report["summary"] = {
            "event_count": summary.get("event_count"),
            "transient_count": summary.get("transient_count"),
            "by_class": summary.get("by_class"),
            "unexpected_n": len(summary.get("unexpected") or []),
            "unclassified_n": len(summary.get("unclassified") or []),
        }
        (OUT / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("FORENSIC REPORT", json.dumps(report, ensure_ascii=False, indent=2))
        window.close()
        _pump(app, 100)
        return 0
    begin_capture("B_OFFLINE")
    t_b0 = time.time()
    plat_now = str(getattr(panel, "_current_platform", "") or "")
    btn = panel.btn_download_offline
    mark(
        "B_OFFLINE_START",
        platform=plat_now,
        button_text=btn.text(),
        button_enabled=bool(btn.isEnabled()),
        managed=str(getattr(panel, "_managed_path", "")),
    )
    b_done = {"ok": False, "fail": False, "skip": False, "payload": ""}

    def _b_ok(path="") -> None:
        b_done["ok"] = True
        b_done["payload"] = str(path)
        mark("B_OFFLINE_FINISHED_SIGNAL", path=str(path))

    def _b_skip(reason="") -> None:
        b_done["skip"] = True
        b_done["payload"] = str(reason)
        mark("B_OFFLINE_SKIPPED_SIGNAL", reason=str(reason))

    def _b_fail(err="") -> None:
        b_done["fail"] = True
        b_done["payload"] = str(err)
        mark("B_OFFLINE_FAILED_SIGNAL", error=str(err))

    panel._download_offline_page()
    _pump(app, 300)
    off_worker = getattr(panel, "_offline_worker", None)
    report["scenario_b"]["worker_started"] = off_worker is not None
    report["scenario_b"]["platform"] = plat_now
    report["scenario_b"]["button_text"] = btn.text()
    if off_worker is not None:
        off_worker.archive_finished.connect(_b_ok)
        off_worker.archive_skipped.connect(_b_skip)
        off_worker.archive_failed.connect(_b_fail)
        _wait_signal(app, off_worker.finished, 120000)
        _pump(app, 2200)
    else:
        mark("B_OFFLINE_NO_WORKER")
        _pump(app, 2200)
    mark(
        "B_OFFLINE_WAIT_DONE",
        ok=b_done["ok"],
        fail=b_done["fail"],
        skip=b_done["skip"],
    )
    report["scenario_b"]["start"] = t_b0
    report["scenario_b"]["success"] = bool(b_done["ok"])
    report["scenario_b"]["skipped"] = bool(b_done["skip"])
    report["scenario_b"]["failed"] = bool(b_done["fail"])
    report["scenario_b"]["payload"] = str(b_done["payload"])[:400]
    report["scenario_b"]["end"] = time.time()
    end_capture()

    summary = dump_summary()
    report["summary"] = {
        "event_count": summary.get("event_count"),
        "transient_count": summary.get("transient_count"),
        "by_class": summary.get("by_class"),
        "unexpected_n": len(summary.get("unexpected") or []),
        "unclassified_n": len(summary.get("unclassified") or []),
    }
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("FORENSIC REPORT", json.dumps(report, ensure_ascii=False, indent=2))
    window.close()
    _pump(app, 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
