"""Dynamic source options — mod.io only for Anno 1800 / 纪元1800."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.mod_platform import (
    MODIO_ANNO_1800_URL,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    default_source_url_for_platform,
    get_available_sources,
    is_anno_1800_game,
    normalize_platform,
    platform_requires_source_url,
)
from ui.edit_mod_dialog import EditModDialog
from ui.mod_import_dialog import ModImportDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_is_anno_1800_by_name_and_app_id() -> None:
    assert is_anno_1800_game("纪元1800")
    assert is_anno_1800_game("Anno 1800")
    assert is_anno_1800_game("anno1800")
    assert is_anno_1800_game(game_id=916440)
    assert not is_anno_1800_game("Palworld")
    assert not is_anno_1800_game(game_id=1623730)


def test_get_available_sources_injects_modio_only_for_anno() -> None:
    pal = get_available_sources("Palworld", 1623730)
    assert [p for p, _ in pal] == [
        PLATFORM_STEAM,
        PLATFORM_NEXUS,
        PLATFORM_GITHUB,
        PLATFORM_OTHER,
    ]
    assert PLATFORM_MODIO not in {p for p, _ in pal}
    assert dict(pal)[PLATFORM_OTHER] == "其它"

    anno = get_available_sources("纪元1800", 916440)
    ids = [p for p, _ in anno]
    assert ids[0] == PLATFORM_MODIO
    assert ids[1:] == [
        PLATFORM_STEAM,
        PLATFORM_NEXUS,
        PLATFORM_GITHUB,
        PLATFORM_OTHER,
    ]
    labels = dict(anno)
    assert labels[PLATFORM_MODIO] == "mod.io"

    anno_en = get_available_sources("Anno 1800", 0)
    assert PLATFORM_MODIO in {p for p, _ in anno_en}
    assert [p for p, _ in anno_en][0] == PLATFORM_MODIO


def test_stardew_sources_omit_steam() -> None:
    from core.mod_platform import is_stardew_valley_game

    assert is_stardew_valley_game("星露谷物语")
    assert is_stardew_valley_game("Stardew Valley")
    assert is_stardew_valley_game(game_id=413150)

    sv = get_available_sources("星露谷物语", 413150)
    assert PLATFORM_STEAM not in {p for p, _ in sv}
    assert [p for p, _ in sv] == [
        PLATFORM_NEXUS,
        PLATFORM_GITHUB,
        PLATFORM_OTHER,
    ]
    assert PLATFORM_MODIO not in {p for p, _ in sv}

    en = get_available_sources("Stardew Valley", 0)
    assert PLATFORM_STEAM not in {p for p, _ in en}


def test_other_platform_url_optional() -> None:
    assert not platform_requires_source_url(PLATFORM_OTHER)
    assert platform_requires_source_url(PLATFORM_GITHUB)
    assert normalize_platform("其它") == PLATFORM_OTHER
    assert default_source_url_for_platform(PLATFORM_OTHER) == ""


def test_default_modio_url_for_anno() -> None:
    assert (
        default_source_url_for_platform(
            PLATFORM_MODIO, game_name="纪元1800", game_id=916440
        )
        == MODIO_ANNO_1800_URL
    )
    assert normalize_platform("mod.io") == PLATFORM_MODIO


def test_import_dialog_hides_modio_for_palworld(
    qapp: QApplication, tmp_path
) -> None:
    dlg = ModImportDialog(
        tmp_path,
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    assert PLATFORM_MODIO not in dlg._platform_radios
    assert dlg.radio_modio is None
    labels = [btn.text() for btn in dlg._platform_radios.values()]
    assert "mod.io" not in labels
    dlg.close()


def test_import_dialog_shows_modio_for_anno(
    qapp: QApplication, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "ui.mod_import_dialog.QMessageBox.warning",
        lambda *args, **kwargs: None,
    )
    dlg = ModImportDialog(
        tmp_path,
        game_context={"game_id": 916440, "game_name": "纪元1800"},
    )
    assert PLATFORM_MODIO in dlg._platform_radios
    assert dlg.radio_modio is not None
    assert dlg.radio_modio.text() == "mod.io"
    assert dlg.radio_steam is not None
    assert dlg.radio_steam.isHidden()
    assert dlg.radio_modio.isChecked()
    assert dlg.selected_platform() == PLATFORM_MODIO
    params = dlg._collect_params(PLATFORM_MODIO)
    # No folder selected → empty stub import is allowed
    assert params is not None
    assert params["folder"]
    assert Path(params["folder"]).is_dir()
    dlg.modio_folder_edit.setText(str(tmp_path))
    (tmp_path / "mod.txt").write_text("x", encoding="utf-8")
    params = dlg._collect_params(PLATFORM_MODIO)
    assert params is not None
    assert params["modio_url"] == MODIO_ANNO_1800_URL
    assert params["folder"] == str(tmp_path)
    dlg.close()


def test_import_dialog_steam_visible_for_non_anno(
    qapp: QApplication, tmp_path: Path
) -> None:
    dlg = ModImportDialog(
        tmp_path,
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    assert dlg.radio_steam is not None
    assert not dlg.radio_steam.isHidden()
    dlg.close()


def test_import_dialog_omits_steam_for_stardew(
    qapp: QApplication, tmp_path: Path
) -> None:
    dlg = ModImportDialog(
        tmp_path,
        game_context={"game_id": 413150, "game_name": "星露谷物语"},
    )
    assert PLATFORM_STEAM not in dlg._platform_radios
    assert dlg.radio_steam is None
    dlg.close()


def test_edit_dialog_source_options_by_game(qapp: QApplication) -> None:
    pal = EditModDialog(
        game_name="Palworld",
        game_id=1623730,
        platform=PLATFORM_STEAM,
    )
    pal_ids = [
        pal.platform_combo.itemData(i) for i in range(pal.platform_combo.count())
    ]
    assert PLATFORM_MODIO not in pal_ids
    assert PLATFORM_OTHER in pal_ids
    assert "mod.io" not in [
        pal.platform_combo.itemText(i) for i in range(pal.platform_combo.count())
    ]
    idx_other = pal.platform_combo.findData(PLATFORM_OTHER)
    pal.platform_combo.setCurrentIndex(idx_other)
    pal.source_url_edit.clear()
    values = pal.values()
    assert values["platform"] == PLATFORM_OTHER
    assert values["source_url"] == ""
    pal.close()

    anno = EditModDialog(
        game_name="Anno 1800",
        game_id=916440,
        platform=PLATFORM_NEXUS,
        source_url="",
    )
    anno_ids = [
        anno.platform_combo.itemData(i) for i in range(anno.platform_combo.count())
    ]
    assert PLATFORM_MODIO in anno_ids
    idx = anno.platform_combo.findData(PLATFORM_MODIO)
    anno.platform_combo.setCurrentIndex(idx)
    values = anno.values()
    assert values["platform"] == PLATFORM_MODIO
    assert values["source_url"] == MODIO_ANNO_1800_URL
    anno.close()


def test_import_dialog_other_allows_empty_url(qapp: QApplication, tmp_path) -> None:
    dlg = ModImportDialog(
        tmp_path,
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    assert PLATFORM_OTHER in dlg._platform_radios
    dlg.radio_other.setChecked(True)
    (tmp_path / "mod.txt").write_text("x", encoding="utf-8")
    dlg.other_folder_edit.setText(str(tmp_path))
    dlg.other_url_edit.clear()
    params = dlg._collect_params(PLATFORM_OTHER)
    assert params is not None
    assert params["source_url"] == ""
    assert params["folder"] == str(tmp_path)
    dlg.close()
