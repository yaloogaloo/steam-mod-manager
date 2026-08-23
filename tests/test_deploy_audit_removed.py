"""Phase 11.2: deploy consistency audit is removed; Phase 8 Deploy remains."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from ui.library_query import (
    FILTER_PLATFORM_GITHUB,
    FILTER_PLATFORM_STEAM,
    collect_category_labels,
    collect_source_keys,
)


def test_case1_library_open_does_not_start_deploy_audit() -> None:
    import ui.main_window as mw
    import ui.library_view as lv

    main_src = inspect.getsource(mw.MainWindow)
    view_src = inspect.getsource(lv.ModLibraryView)
    assert "run_deploy_audit" not in main_src
    assert "_run_startup_deploy_audit" not in main_src
    assert "singleShot(750" not in main_src
    assert "run_deploy_audit" not in view_src
    assert "scan_deployed_mods" not in view_src
    assert not hasattr(lv.ModLibraryView, "run_deploy_audit")


def test_case2_refresh_source_has_no_audit() -> None:
    import ui.library_view as lv

    src = inspect.getsource(lv.ModLibraryView.refresh)
    assert "deploy_audit" not in src
    assert "scan_deployed_mods" not in src
    assert "run_deploy_audit" not in src


def test_case3_game_switch_has_no_audit() -> None:
    import ui.library_view as lv

    src = inspect.getsource(lv.ModLibraryView._on_game_item_changed)
    assert "deploy_audit" not in src
    assert "run_deploy_audit" not in src
    assert "scan_deployed_mods" not in src


def test_case4_game_row_has_no_audit_badge() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.library_view import _GameFilterRow

    app = QApplication.instance() or QApplication([])
    row = _GameFilterRow(
        "Anno 1800", 70, kind=_GameFilterRow.KIND_GAME, overall_status="warning"
    )
    assert not hasattr(row, "status_label")
    assert row.icon_label.text() == "🎮"
    assert "⚠" not in row.name_label.text()
    assert row.name_label.text() == "Anno 1800"
    del app


def test_case5_game_name_not_squeezed_by_status_slot() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from ui.library_view import _GameFilterRow

    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.setFixedWidth(180)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    row = _GameFilterRow("Anno 1800", 70)
    lay.addWidget(row)
    host.show()
    QApplication.processEvents()
    assert row.name_label.text() == "Anno 1800"
    assert row.count_label.text() == "70"
    assert row.layout().count() == 3  # icon, name, count — no status slot
    host.close()
    del app


def test_case8_library_content_filters_still_work() -> None:
    from ui.library_query import (
        FILTER_BACKUP_INVALID,
        FILTER_CONTENT_MISSING,
        FILTER_FOLDER_MISSING,
        FILTER_IDENTITY_CONFLICT,
        ModFilterIndex,
        matches_status_filter,
    )

    def idx(status: str) -> ModFilterIndex:
        return ModFilterIndex(
            mod_id="1",
            display_name="X",
            steam_name="",
            notes="",
            game_name="G",
            favorite=False,
            deployed=False,
            has_offline=False,
            mtime=0.0,
            sort_name="X",
            content_status=status,
        )

    assert matches_status_filter(idx(FILTER_CONTENT_MISSING), FILTER_CONTENT_MISSING)
    assert matches_status_filter(idx(FILTER_FOLDER_MISSING), FILTER_FOLDER_MISSING)
    assert matches_status_filter(idx(FILTER_IDENTITY_CONFLICT), FILTER_IDENTITY_CONFLICT)
    assert matches_status_filter(idx(FILTER_BACKUP_INVALID), FILTER_BACKUP_INVALID)


def test_case9_dynamic_sources_unchanged() -> None:
    from ui.library_query import ModFilterIndex

    def idx(source: str) -> ModFilterIndex:
        return ModFilterIndex(
            mod_id=source,
            display_name="X",
            steam_name="",
            notes="",
            game_name="G",
            favorite=False,
            deployed=False,
            has_offline=False,
            mtime=0.0,
            sort_name="X",
            source_type=source,
            platform=source,
        )

    keys = collect_source_keys([idx("steam"), idx("github")])
    assert keys == [FILTER_PLATFORM_STEAM, FILTER_PLATFORM_GITHUB]


def test_case10_dynamic_categories_unchanged() -> None:
    from ui.library_query import ModFilterIndex

    a = ModFilterIndex(
        mod_id="1",
        display_name="X",
        steam_name="",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="X",
        category_tags="建筑 美化",
    )
    assert set(collect_category_labels([a])) == {"建筑", "美化"}


def test_deploy_audit_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.deploy_audit")
    root = Path(__file__).resolve().parents[1]
    assert not (root / "services" / "deploy_audit.py").exists()
    assert not (root / "tests" / "test_deploy_audit.py").exists()


def test_phase8_deploy_surface_still_present() -> None:
    from services.deploy_status import (
        DEPLOYMENT_CONFLICT,
        DEPLOYMENT_DEPLOYED,
        DEPLOYMENT_NOT_DEPLOYED,
        DEPLOYMENT_OUTDATED,
    )
    from services import deploy as deploy_mod

    src = inspect.getsource(deploy_mod)
    assert "undeploy" in src.lower()
    assert "redeploy" in src.lower() or "deploy" in src.lower()
    assert DEPLOYMENT_NOT_DEPLOYED == "not_deployed"
    assert DEPLOYMENT_DEPLOYED == "deployed"
    assert DEPLOYMENT_OUTDATED == "outdated"
    assert DEPLOYMENT_CONFLICT == "conflict"
    assert (Path(__file__).resolve().parents[1] / "services" / "deploy_conflict.py").exists()
