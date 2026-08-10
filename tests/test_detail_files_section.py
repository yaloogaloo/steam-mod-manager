"""Detail panel Files section: unified list, sort, badges, context role mark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QWidget

from core.db_manager import PLATFORM_GITHUB, DatabaseManager
from core.mod_platform import (
    FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_UNKNOWN,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    ModFileEntry,
    ModFilesBundle,
    SOURCE_TYPE_GITHUB,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.mod_files import ModFileManager
from ui.mod_detail_panel import ModDetailPanel
from ui.mod_files_ux import file_badge_kind, file_description, sort_files_for_detail


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "files_section.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, *, mid: str, title: str) -> Path:
    folder = lib / "Game" / title
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {"published_file_id": mid, "title": title},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return folder


def _multi_bundle() -> ModFilesBundle:
    return ModFilesBundle(
        files=[
            ModFileEntry(
                id="main",
                filename="release.zip",
                file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                source_type=SOURCE_TYPE_GITHUB,
                type=FILE_TYPE_MAIN,
                selected_for_deploy=True,
            ),
            ModFileEntry(
                id="src",
                filename="source.zip",
                file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
                source_type=SOURCE_TYPE_GITHUB,
                type=FILE_TYPE_OPTIONAL,
                selected_for_deploy=False,
            ),
            ModFileEntry(
                id="dev",
                filename="dev.zip",
                file_role=FILE_ROLE_GITHUB_DEVELOPER_BUILD,
                source_type=SOURCE_TYPE_GITHUB,
                type=FILE_TYPE_OPTIONAL,
                selected_for_deploy=False,
                metadata={"description": "开发者包"},
            ),
        ]
    )


def _file_rows(panel: ModDetailPanel) -> list[QWidget]:
    return [
        w
        for w in panel.mod_files_host.findChildren(QWidget)
        if w.objectName() == "detailFilesRow"
    ]


def _row_primary(row: QWidget) -> str:
    for lab in row.findChildren(QLabel):
        if lab.objectName() == "detailFilesPrimary":
            return lab.text()
    return ""


def test_badge_and_description_helpers() -> None:
    main = ModFileEntry(
        filename="a.zip",
        file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
        source_type=SOURCE_TYPE_GITHUB,
    )
    source = ModFileEntry(
        filename="src.zip",
        file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        source_type=SOURCE_TYPE_GITHUB,
    )
    other = ModFileEntry(
        filename="dev.zip",
        file_role=FILE_ROLE_GITHUB_DEVELOPER_BUILD,
        source_type=SOURCE_TYPE_GITHUB,
        metadata={"description": "nightly"},
    )
    assert file_badge_kind(main) == "Main"
    assert file_badge_kind(source) == "Source"
    assert file_badge_kind(other) is None
    assert file_description(other) == "nightly"


def test_sort_files_for_detail_order() -> None:
    files = [
        ModFileEntry(id="s", filename="z_source.zip", file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE),
        ModFileEntry(id="o", filename="a_other.zip", file_role=FILE_ROLE_UNKNOWN),
        ModFileEntry(id="m", filename="m_main.zip", file_role=FILE_ROLE_GITHUB_RELEASE_ASSET),
        ModFileEntry(id="o2", filename="b_other.zip", file_role=FILE_ROLE_GITHUB_DEVELOPER_BUILD),
    ]
    ordered = sort_files_for_detail(files)
    assert [f.id for f in ordered] == ["m", "o", "o2", "s"]


def test_files_section_hidden_when_single_file(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed(lib, mid="9101", title="Single")
    db.upsert_mod(
        ModMetadata(published_file_id="9101", title="Single", managed_path=str(folder))
    )
    db.update_mod_platform_info("9101", platform=PLATFORM_GITHUB)
    db.set_mod_files(
        "9101",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="only",
                    filename="only.zip",
                    file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_MAIN,
                )
            ]
        ),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    assert panel._files_section_frame.isHidden()


def test_file_combo_label_keeps_similar_filenames() -> None:
    from ui.mod_files_ux import file_combo_label

    a = ModFileEntry(
        id="a",
        filename="PalSchema_0.6.1.zip",
        display_name="PalSchema",
        file_role=FILE_ROLE_UNKNOWN,
    )
    b = ModFileEntry(
        id="b",
        filename="PalSchema-0.6.1.zip",
        display_name="PalSchema",
        file_role=FILE_ROLE_UNKNOWN,
    )
    assert file_combo_label(a) == "PalSchema_0.6.1.zip"
    assert file_combo_label(b) == "PalSchema-0.6.1.zip"
    assert file_combo_label(a) != file_combo_label(b)


def test_unified_list_shows_all_filenames_sorted(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed(lib, mid="9105", title="Similar")
    db.upsert_mod(
        ModMetadata(published_file_id="9105", title="Similar", managed_path=str(folder))
    )
    db.update_mod_platform_info("9105", platform=PLATFORM_GITHUB)
    db.set_mod_files(
        "9105",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="u",
                    filename="PalSchema_0.6.1.zip",
                    display_name="PalSchema",
                    file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="h",
                    filename="PalSchema-0.6.1.zip",
                    display_name="PalSchema",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                ),
                ModFileEntry(
                    id="s",
                    filename="source.zip",
                    file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                ),
            ]
        ),
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    rows = _file_rows(panel)
    assert len(rows) == 3
    names = [_row_primary(r) for r in rows]
    assert names[0] == "PalSchema_0.6.1.zip"  # Main first
    assert "PalSchema-0.6.1.zip" in names
    assert names[-1] == "source.zip"  # Source last

    # No role combos
    assert not hasattr(panel, "_files_main_combo") or panel.__dict__.get(
        "_files_main_combo"
    ) in (None,)

    # Main checkbox checked; Source has no checkbox; Other has edit pencil
    main_row = next(r for r in rows if r.property("file_id") == "u")
    src_row = next(r for r in rows if r.property("file_id") == "s")
    other_row = next(r for r in rows if r.property("file_id") == "h")
    assert main_row.findChildren(QCheckBox)
    assert main_row.findChildren(QCheckBox)[0].isChecked()
    assert not src_row.findChildren(QCheckBox)
    assert other_row.findChildren(QPushButton, "detailFilesEditButton")

    descs = [
        lab.text()
        for lab in panel.mod_files_host.findChildren(QLabel)
        if lab.objectName() == "detailFileDesc"
    ]
    assert "（无说明）" not in descs


def test_files_section_unified_list_and_other_edit(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    folder = _seed(lib, mid="9102", title="Multi")
    db.upsert_mod(
        ModMetadata(published_file_id="9102", title="Multi", managed_path=str(folder))
    )
    db.update_mod_platform_info("9102", platform=PLATFORM_GITHUB)
    db.set_mod_files("9102", _multi_bundle())

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    assert not panel._files_section_frame.isHidden()
    assert panel._files_section_label.text().startswith("文件")

    badges = {
        lab.text(): lab.objectName()
        for lab in panel.mod_files_host.findChildren(QLabel)
        if lab.objectName() in ("detailFileBadgeMain", "detailFileBadgeSource")
    }
    assert badges.get("Main") == "detailFileBadgeMain"
    assert badges.get("Source") == "detailFileBadgeSource"

    rows = _file_rows(panel)
    assert len(rows) == 3
    names = [_row_primary(r) for r in rows]
    assert names == ["release.zip", "dev.zip", "source.zip"]

    descs = [
        lab.text()
        for lab in panel.mod_files_host.findChildren(QLabel)
        if lab.objectName() == "detailFileDesc"
    ]
    assert "开发者包" in descs
    edits = panel.mod_files_host.findChildren(QPushButton, "detailFilesEditButton")
    assert edits
    assert edits[0].text() == "✎"
    assert "（无说明）" not in descs

    monkeypatch.setattr(
        "ui.mod_detail_panel.QInputDialog.getText",
        lambda *a, **k: ("CI 构建产物", True),
    )
    panel._on_edit_file_description("dev", "开发者包")
    qapp.processEvents()
    updated = {f.id: f for f in ModFileManager(db).get_files("9102")}
    assert updated["dev"].metadata.get("description") == "CI 构建产物"


def test_context_menu_role_remap_resorts(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed(lib, mid="9103", title="Remap")
    db.upsert_mod(
        ModMetadata(published_file_id="9103", title="Remap", managed_path=str(folder))
    )
    db.update_mod_platform_info("9103", platform=PLATFORM_GITHUB)
    db.set_mod_files("9103", _multi_bundle())

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    panel._apply_file_badge_role("dev", "Main")
    qapp.processEvents()

    files = {f.id: f for f in ModFileManager(db).get_files("9103")}
    assert files["dev"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["main"].file_role == FILE_ROLE_UNKNOWN
    assert files["src"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert file_badge_kind(files["dev"]) == "Main"

    names = [_row_primary(r) for r in _file_rows(panel)]
    assert names[0] == "dev.zip"
    assert names[-1] == "source.zip"
    assert "release.zip" in names


def test_set_file_role_mapping_exclusive(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "library"
    folder = _seed(lib, mid="9104", title="Map")
    db.upsert_mod(
        ModMetadata(published_file_id="9104", title="Map", managed_path=str(folder))
    )
    db.update_mod_platform_info("9104", platform=PLATFORM_GITHUB)
    db.set_mod_files("9104", _multi_bundle())

    mgr = ModFileManager(db)
    mgr.set_file_role_mapping(
        "9104",
        main_file_id="dev",
        source_file_id="main",
        platform=PLATFORM_GITHUB,
    )
    files = {f.id: f for f in mgr.get_files("9104")}
    assert files["dev"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["dev"].selected_for_deploy is True
    assert files["main"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert files["main"].selected_for_deploy is False
    assert files["src"].file_role == FILE_ROLE_UNKNOWN
    assert files["src"].selected_for_deploy is False
