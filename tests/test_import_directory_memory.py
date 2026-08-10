"""Import dialog remembers last_import_directory via QSettings."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QFileDialog

from services.importers.import_settings import (
    get_last_import_directory,
    resolve_import_start_directory,
    set_last_import_directory,
)
from ui.mod_import_dialog import ModImportDialog, parse_archive_path_list


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point QSettings at a temp INI so tests do not touch real user config."""
    ini = tmp_path / "import_settings.ini"

    def _settings():
        return QSettings(str(ini), QSettings.Format.IniFormat)

    monkeypatch.setattr(
        "services.importers.import_settings._settings", _settings
    )
    yield ini


def test_import_directory_memory(
    tmp_path: Path,
    isolated_settings: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports = tmp_path / "Downloads" / "mods"
    imports.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()

    assert get_last_import_directory(fallback=str(other)) == str(other)

    set_last_import_directory(imports / "Main.zip")  # file → parent dir
    assert get_last_import_directory() == str(imports)
    assert resolve_import_start_directory() == str(imports)

    # Explicit candidate wins over remembered dir.
    assert resolve_import_start_directory(str(other)) == str(other)

    captured: dict[str, str] = {}

    def fake_open_names(parent, title, start, filt):  # noqa: ANN001
        del parent, title
        captured["start"] = start
        return ([str(imports / "Main.zip"), str(imports / "Optional.zip")], filt)

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(fake_open_names))
    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    try:
        dlg.radio_nexus.setChecked(True)
        dlg._browse_archive(dlg.nexus_archive_edit)
        assert captured["start"] == str(imports)
        paths = parse_archive_path_list(dlg.nexus_archive_edit.text())
        assert len(paths) == 2
        assert paths[0].endswith("Main.zip")
        assert get_last_import_directory() == str(imports)
    finally:
        dlg.close()


def test_parse_archive_path_list() -> None:
    assert parse_archive_path_list(r"C:\a\Main.zip; C:\a\Optional.zip") == [
        r"C:\a\Main.zip",
        r"C:\a\Optional.zip",
    ]
    assert parse_archive_path_list("") == []
