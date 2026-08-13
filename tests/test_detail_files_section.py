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
            full = getattr(lab, "fullText", None)
            if callable(full):
                return str(full())
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


def test_nexus_context_menu_sets_category_and_selection(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from core.db_manager import PLATFORM_NEXUS as DB_NEXUS
    from core.mod_platform import (
        FILE_ROLE_NEXUS_OPTIONAL,
        FILE_ROLE_UNKNOWN,
        SOURCE_TYPE_NEXUS,
    )
    from ui.mod_files_ux import nexus_category_label

    lib = tmp_path / "library"
    folder = _seed(lib, mid="9202", title="NexusCtx")
    db.upsert_mod(
        ModMetadata(published_file_id="9202", title="NexusCtx", managed_path=str(folder))
    )
    db.update_mod_platform_info("9202", platform=DB_NEXUS)
    db.set_mod_files(
        "9202",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="f1",
                    filename="a.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                ),
            ]
        ),
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    from PySide6.QtWidgets import QMenu

    m = QMenu()
    panel._build_nexus_context_menu(m, None)
    labels = [a.text() for a in m.actions()]
    assert labels == [
        "设为 Main (主文件)",
        "设为 Optional (可选文件)",
        "设为 Miscellaneous (杂项)",
        "设为 汉化",
        "设为 Other (其他/普通文件)",
    ]
    assert all(a.data()[0] == "nexus" for a in m.actions())

    g = QMenu()
    panel._build_github_context_menu(g, None)
    assert "设为 Source (源码)" in [a.text() for a in g.actions()]
    assert "设为 汉化" not in [a.text() for a in g.actions()]

    panel._apply_nexus_category("f1", "汉化")
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files("9202")}
    assert nexus_category_label(files["f1"]) == "汉化"
    assert files["f1"].selected_for_deploy is False
    assert files["f1"].metadata.get("category") == "汉化"

    panel._apply_nexus_category("f1", "Main")
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files("9202")}
    assert nexus_category_label(files["f1"]) == "Main"
    assert files["f1"].selected_for_deploy is True

    panel._apply_nexus_category("f1", "Optional")
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files("9202")}
    assert nexus_category_label(files["f1"]) == "Optional"
    assert files["f1"].file_role == FILE_ROLE_NEXUS_OPTIONAL
    assert files["f1"].selected_for_deploy is False


def test_nexus_badge_colors_and_edit_visible_for_all(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QPushButton

    from core.db_manager import PLATFORM_NEXUS as DB_NEXUS
    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_UNKNOWN,
        SOURCE_TYPE_NEXUS,
    )
    from ui.styles import PANEL_STYLE

    lib = tmp_path / "library"
    folder = _seed(lib, mid="9203", title="NexusBadge")
    db.upsert_mod(
        ModMetadata(published_file_id="9203", title="NexusBadge", managed_path=str(folder))
    )
    db.update_mod_platform_info("9203", platform=DB_NEXUS)
    db.set_mod_files(
        "9203",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="m1",
                    filename="main.zip",
                    file_role=FILE_ROLE_NEXUS_MAIN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                    metadata={"category": "Main"},
                ),
                ModFileEntry(
                    id="h1",
                    filename="ABC_very_long_mod_name_2026_version_chinese_pack.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                    metadata={"category": "汉化"},
                ),
            ]
        ),
    )
    panel = ModDetailPanel()
    panel.setStyleSheet(PANEL_STYLE)
    panel.setFixedWidth(420)
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()

    by_id = {r.property("file_id"): r for r in _file_rows(panel)}
    # Force row to the panel content width before geometry asserts.
    for row in by_id.values():
        row.setMaximumWidth(380)
        row.adjustSize()
    qapp.processEvents()
    main_badge = next(
        lab
        for lab in by_id["m1"].findChildren(QLabel)
        if lab.objectName() == "detailFileCategoryBadge"
    )
    i18n_badge = next(
        lab
        for lab in by_id["h1"].findChildren(QLabel)
        if lab.objectName() == "detailFileCategoryBadge"
    )
    assert main_badge.property("category") == "Main"
    assert i18n_badge.property("category") == "汉化"
    assert main_badge.width() == 38 and main_badge.height() == 18
    assert i18n_badge.property("category") != "Main"

    for rid in ("m1", "h1"):
        row = by_id[rid]
        edits = row.findChildren(QPushButton, "detailFilesEditButton")
        assert len(edits) == 1 and not edits[0].isHidden()
        # Edit button stays inside the row viewport even with long filenames.
        btn = edits[0]
        assert btn.parent() is not None
        assert btn.parent() is row or row.isAncestorOf(btn)
        assert not btn.isWindow(), "edit button must not be a top-level window"
        assert btn.geometry().right() <= row.rect().right()
        assert btn.geometry().left() >= 0


def test_nexus_flat_list_badges_main_checked_optional_unchecked(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QTreeWidget

    from core.db_manager import PLATFORM_NEXUS as DB_NEXUS
    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_NEXUS_OPTIONAL,
        SOURCE_TYPE_NEXUS,
    )

    lib = tmp_path / "library"
    folder = _seed(lib, mid="9201", title="NexusMulti")
    db.upsert_mod(
        ModMetadata(published_file_id="9201", title="NexusMulti", managed_path=str(folder))
    )
    db.update_mod_platform_info("9201", platform=DB_NEXUS)
    db.set_mod_files(
        "9201",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="m1",
                    filename="main_a.zip",
                    file_role=FILE_ROLE_NEXUS_MAIN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="m2",
                    filename="main_b.zip",
                    file_role=FILE_ROLE_NEXUS_MAIN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="o1",
                    filename="opt.zip",
                    file_role=FILE_ROLE_NEXUS_OPTIONAL,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                ),
            ]
        ),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    assert not panel._files_section_frame.isHidden()
    assert panel.mod_files_host.findChild(QTreeWidget, "detailFilesTree") is None

    rows = _file_rows(panel)
    assert len(rows) == 3

    badges = {
        lab.text(): lab
        for lab in panel.mod_files_host.findChildren(QLabel)
        if lab.objectName() == "detailFileCategoryBadge"
    }
    assert "Main" in badges
    assert badges["Main"].property("category") == "Main"
    assert "Opt" in badges
    assert badges["Opt"].property("category") == "Optional"
    assert badges["Opt"].objectName() != "detailFileBadgeMain"

    # Nexus: every row shows edit button
    for row in rows:
        edits = row.findChildren(QPushButton, "detailFilesEditButton")
        assert edits and not edits[0].isHidden()

    main_rows = [r for r in rows if r.property("file_id") in ("m1", "m2")]
    opt_row = next(r for r in rows if r.property("file_id") == "o1")
    assert all(r.findChildren(QCheckBox)[0].isChecked() for r in main_rows)
    assert not opt_row.findChildren(QCheckBox)[0].isChecked()

    # Filename labels: no wrap + native tooltip = full name
    for row in rows:
        for lab in row.findChildren(QLabel):
            if lab.objectName() == "detailFilesPrimary":
                assert lab.wordWrap() is False
                assert lab.toolTip() == lab.fullText()


def test_main_checkbox_unlocked_and_toggle_stays_quiet(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Main may be unchecked; toggle must not emit tags_saved (no card flash/popup)."""
    from core.db_manager import PLATFORM_NEXUS as DB_NEXUS
    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_NEXUS_OPTIONAL,
        SOURCE_TYPE_NEXUS,
    )

    lib = tmp_path / "library"
    folder = _seed(lib, mid="9210", title="MainUnlock")
    db.upsert_mod(
        ModMetadata(
            published_file_id="9210",
            title="MainUnlock",
            managed_path=str(folder),
        )
    )
    db.update_mod_platform_info("9210", platform=DB_NEXUS)
    db.set_mod_files(
        "9210",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="m1",
                    filename="main.zip",
                    file_role=FILE_ROLE_NEXUS_MAIN,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                    metadata={"category": "Main"},
                ),
                ModFileEntry(
                    id="o1",
                    filename="opt.zip",
                    file_role=FILE_ROLE_NEXUS_OPTIONAL,
                    source_type=SOURCE_TYPE_NEXUS,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                    metadata={"category": "Optional"},
                ),
            ]
        ),
    )

    panel = ModDetailPanel()
    emitted: list[object] = []
    panel.tags_saved.connect(lambda p: emitted.append(p))
    panel.show_mod(folder)
    qapp.processEvents()

    row = next(r for r in _file_rows(panel) if r.property("file_id") == "m1")
    cb = row.findChildren(QCheckBox)[0]
    assert cb.isEnabled()
    assert cb.isChecked()

    panel._on_mod_file_toggled("m1", False)
    qapp.processEvents()

    assert emitted == []
    files = {f.id: f for f in ModFileManager(db).get_files("9210")}
    assert files["m1"].selected_for_deploy is False

    # Sidecar re-apply / show_mod must not force Main back on.
    panel.show_mod(folder)
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files("9210")}
    assert files["m1"].selected_for_deploy is False
    row = next(r for r in _file_rows(panel) if r.property("file_id") == "m1")
    cb = row.findChildren(QCheckBox)[0]
    assert cb.isEnabled() and not cb.isChecked()
