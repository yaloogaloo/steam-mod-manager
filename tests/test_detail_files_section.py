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
    PLATFORM_NEXUS,
    ModFileEntry,
    ModFilesBundle,
    SOURCE_TYPE_GITHUB,
)
from services.file_ops import INFO_DIR_NAME
from services.identity_service import create_mod_identity
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
            {
                "published_file_id": mid,
                "internal_id": mid,
                "title": title,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return folder


def _register_mod(
    db: DatabaseManager,
    lib: Path,
    *,
    title: str,
    platform: str = PLATFORM_GITHUB,
    bundle: ModFilesBundle | None = None,
    token: str = "",
) -> tuple[Path, str]:
    slug = (token or title).lower().replace(" ", "-")
    plat = str(platform or "").strip().lower()
    if plat == PLATFORM_NEXUS or plat == "nexus":
        nid = str(abs(hash(slug)) % 900000 + 100000)
        created = create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id=nid,
            source_url=f"https://www.nexusmods.com/palworld/mods/{nid}",
            title=title,
            app_id=1623730,
            game_name="Palworld",
            operation="import",
            mod_files=bundle,
        )
    else:
        created = create_mod_identity(
            db,
            platform=PLATFORM_GITHUB,
            external_id=f"owner/{slug}",
            source_url=f"https://github.com/owner/{slug}",
            title=title,
            app_id=1623730,
            game_name="Palworld",
            operation="import",
            mod_files=bundle,
        )
    mid = str(created.mod_id)
    folder = _seed(lib, mid=mid, title=title)
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder.resolve()),
        platform=plat or PLATFORM_GITHUB,
    )
    if bundle is not None:
        db.set_mod_files(mid, bundle)
    return folder, mid


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
    folder, mid = _register_mod(
        db,
        lib,
        title="Single",
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
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
    folder, mid = _register_mod(
        db,
        lib,
        title="Similar",
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
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
    folder, mid = _register_mod(db, lib, title="Multi", bundle=_multi_bundle())

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
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
    updated = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert updated["dev"].metadata.get("description") == "CI 构建产物"


def test_context_menu_role_remap_resorts(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder, mid = _register_mod(db, lib, title="Remap", bundle=_multi_bundle())

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()

    panel._apply_file_badge_role("dev", "Main")
    qapp.processEvents()

    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert files["dev"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["main"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["src"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert file_badge_kind(files["dev"]) == "Main"
    assert file_badge_kind(files["main"]) == "Main"

    names = [_row_primary(r) for r in _file_rows(panel)]
    assert names[0] in {"dev.zip", "release.zip"}
    assert names[-1] == "source.zip"
    assert "release.zip" in names
    assert "dev.zip" in names
    main_badges = [
        lab.text()
        for lab in panel.mod_files_host.findChildren(QLabel)
        if lab.objectName() == "detailFileBadgeMain"
    ]
    assert main_badges.count("Main") == 2


def test_set_multiple_main_roles_allowed(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "library"
    folder, mid = _register_mod(db, lib, title="Map", bundle=_multi_bundle())

    mgr = ModFileManager(db)
    mgr.set_file_badge_role(mid, "dev", "Main", platform=PLATFORM_GITHUB)
    files = {f.id: f for f in mgr.get_files(mid)}
    assert files["dev"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["dev"].selected_for_deploy is True
    assert files["main"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["src"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert files["src"].selected_for_deploy is False


def test_two_files_set_main(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="TwoMain",
        bundle=ModFilesBundle(
            files=[
                ModFileEntry(
                    id="a",
                    filename="release-v1.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                ),
                ModFileEntry(
                    id="b",
                    filename="release-v2.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                ),
            ]
        ),
    )
    mgr = ModFileManager(db)
    mgr.set_file_badge_role(mid, "a", "Main", platform=PLATFORM_GITHUB)
    mgr.set_file_badge_role(mid, "b", "Main", platform=PLATFORM_GITHUB)
    files = {f.id: f for f in mgr.get_files(mid)}
    assert files["a"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["b"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert file_badge_kind(files["a"]) == "Main"
    assert file_badge_kind(files["b"]) == "Main"


def test_two_files_set_source(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="TwoSrc",
        bundle=ModFilesBundle(
            files=[
                ModFileEntry(
                    id="s1",
                    filename="source-main.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="s2",
                    filename="source-dev.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                    selected_for_deploy=True,
                ),
            ]
        ),
    )
    mgr = ModFileManager(db)
    mgr.set_file_badge_role(mid, "s1", "Source", platform=PLATFORM_GITHUB)
    mgr.set_file_badge_role(mid, "s2", "Source", platform=PLATFORM_GITHUB)
    files = {f.id: f for f in mgr.get_files(mid)}
    assert files["s1"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert files["s2"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert file_badge_kind(files["s1"]) == "Source"
    assert file_badge_kind(files["s2"]) == "Source"


def test_source_never_selected_for_deploy(db: DatabaseManager, tmp_path: Path) -> None:
    from core.mod_platform import is_entry_selected_for_deploy, normalize_file_role
    from services.deploy import resolve_deploy_sources

    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="SrcDeploy",
        bundle=ModFilesBundle(
            files=[
                ModFileEntry(
                    id="s1",
                    filename="source-a.zip",
                    path="source-a.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="s2",
                    filename="source-b.zip",
                    path="source-b.zip",
                    file_role=FILE_ROLE_UNKNOWN,
                    source_type=SOURCE_TYPE_GITHUB,
                    selected_for_deploy=True,
                ),
            ]
        ),
    )
    (folder / "source-a.zip").write_bytes(b"PK")
    (folder / "source-b.zip").write_bytes(b"PK")
    mgr = ModFileManager(db)
    mgr.set_file_badge_role(mid, "s1", "Source", platform=PLATFORM_GITHUB)
    mgr.set_file_badge_role(mid, "s2", "Source", platform=PLATFORM_GITHUB)
    files = {f.id: f for f in mgr.get_files(mid)}
    for entry in files.values():
        assert normalize_file_role(entry.file_role) == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
        assert entry.selected_for_deploy is False
        assert is_entry_selected_for_deploy(entry) is False
    allowed = resolve_deploy_sources(mid, folder, db=db)
    assert allowed is None or "source-a.zip" not in allowed
    assert allowed is None or "source-b.zip" not in allowed


def test_legacy_one_main_one_source_unchanged(
    db: DatabaseManager, tmp_path: Path
) -> None:
    lib = tmp_path / "library"
    folder, mid = _register_mod(db, lib, title="Legacy", bundle=_multi_bundle())

    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert files["main"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["src"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    assert files["dev"].file_role == FILE_ROLE_GITHUB_DEVELOPER_BUILD
    assert files["main"].selected_for_deploy is True
    assert files["src"].selected_for_deploy is False


def test_nexus_context_menu_sets_category_and_selection(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from core.mod_platform import (
        FILE_ROLE_NEXUS_OPTIONAL,
        FILE_ROLE_UNKNOWN,
        SOURCE_TYPE_NEXUS,
    )
    from ui.mod_files_ux import nexus_category_label

    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="NexusCtx",
        platform=PLATFORM_NEXUS,
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
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
    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert nexus_category_label(files["f1"]) == "汉化"
    assert files["f1"].selected_for_deploy is False
    assert files["f1"].metadata.get("category") == "汉化"

    panel._apply_nexus_category("f1", "Main")
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert nexus_category_label(files["f1"]) == "Main"
    assert files["f1"].selected_for_deploy is True

    panel._apply_nexus_category("f1", "Optional")
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert nexus_category_label(files["f1"]) == "Optional"
    assert files["f1"].file_role == FILE_ROLE_NEXUS_OPTIONAL
    assert files["f1"].selected_for_deploy is False


def test_nexus_badge_colors_and_edit_visible_for_all(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QPushButton

    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_UNKNOWN,
        SOURCE_TYPE_NEXUS,
    )
    from ui.styles import PANEL_STYLE

    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="NexusBadge",
        platform=PLATFORM_NEXUS,
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
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

    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_NEXUS_OPTIONAL,
        SOURCE_TYPE_NEXUS,
    )

    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="NexusMulti",
        platform=PLATFORM_NEXUS,
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
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
    from core.mod_platform import (
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_NEXUS_OPTIONAL,
        SOURCE_TYPE_NEXUS,
    )

    lib = tmp_path / "library"
    folder, mid = _register_mod(
        db,
        lib,
        title="MainUnlock",
        platform=PLATFORM_NEXUS,
        bundle=ModFilesBundle(
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
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()

    row = next(r for r in _file_rows(panel) if r.property("file_id") == "m1")
    cb = row.findChildren(QCheckBox)[0]
    assert cb.isEnabled()
    assert cb.isChecked()

    panel._on_mod_file_toggled("m1", False)
    qapp.processEvents()

    assert emitted == []
    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert files["m1"].selected_for_deploy is False

    # Sidecar re-apply / show_mod must not force Main back on.
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    files = {f.id: f for f in ModFileManager(db).get_files(mid)}
    assert files["m1"].selected_for_deploy is False
    row = next(r for r in _file_rows(panel) if r.property("file_id") == "m1")
    cb = row.findChildren(QCheckBox)[0]
    assert cb.isEnabled() and not cb.isChecked()
