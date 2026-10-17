"""Darkest Dungeon target project.xml <Title> post-apply sync."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from services.deploy_project_title import (
    replace_project_title_text,
    sync_deployed_project_title,
)
from services.deploy_rules.game_capabilities import (
    CAPABILITY_SYNC_PROJECT_XML_TITLE,
    reset_game_capabilities_cache,
    set_game_capabilities_config_path,
    supports_game_capability,
)
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder, write_info_sidecar

DD_APP = 262060
OTHER_APP = 424242

PROJECT_XML = """\
<project>
<PreviewIconFile>preview_icon.png</PreviewIconFile>
<ItemDescriptionShort/>
<ModDataPath>J:/iwamoto/ff7cmod/</ModDataPath>
<Title>Cloud Leper Skin</Title>
<Language>japanese</Language>
<PublishedFileId>3129401071</PublishedFileId>
</project>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "project_title.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _reset_caps() -> None:
    set_game_capabilities_config_path(None)
    reset_game_capabilities_cache()
    yield
    set_game_capabilities_config_path(None)
    reset_game_capabilities_cache()


def _configure(db: DatabaseManager, app_id: int, name: str, mod_path: Path) -> Path:
    mod_path.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        app_id,
        name=name,
        install_path=str(mod_path.parent / f"{name}Install"),
        mod_path=str(mod_path),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed(
    db: DatabaseManager,
    library: Path,
    *,
    app_id: int,
    game_name: str,
    folder: str,
    workspace_id: str,
    xml: str = PROJECT_XML,
    display_name: str = "SMM 显示名",
    filename: str = "project.xml",
) -> tuple[Path, str, str]:
    source = library / game_name / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    if filename:
        (source / filename).write_text(xml, encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id=workspace_id,
        title=folder,
        app_id=app_id,
        game_name=game_name,
    )
    pk = prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title=folder,
        app_id=app_id,
        game_name=game_name,
        extra={"display_name": display_name} if display_name else None,
    )
    if display_name:
        write_info_sidecar(
            source,
            internal_id=str(created.internal_id),
            title=folder,
            external_id=workspace_id,
            workspace_id=workspace_id,
            app_id=app_id,
            game_name=game_name,
            extra={"display_name": display_name},
        )
    return source, str(pk), str(created.internal_id)


def test_title_replace_keeps_surrounding_xml() -> None:
    updated = replace_project_title_text(PROJECT_XML, "修女皮肤")
    assert updated is not None
    assert "<Title>修女皮肤</Title>" in updated
    assert "<PreviewIconFile>preview_icon.png</PreviewIconFile>" in updated
    assert "<ItemDescriptionShort/>" in updated
    assert "<Language>japanese</Language>" in updated
    assert "<PublishedFileId>3129401071</PublishedFileId>" in updated
    assert "Cloud Leper Skin" not in updated
    assert replace_project_title_text("<project/>", "X") is None
    escaped = replace_project_title_text(
        "<project><Title>old</Title></project>", "A & B <C>"
    )
    assert escaped is not None
    assert "<Title>A &amp; B &lt;C&gt;</Title>" in escaped


def test_module_does_not_walk_or_rglob() -> None:
    src = inspect.getsource(sync_deployed_project_title)
    module = Path("services/deploy_project_title.py").read_text(encoding="utf-8")
    assert "os.walk" not in module
    assert "rglob" not in module
    assert "walk(" not in src


def test_dd_target_title_synced_source_unchanged(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mods = _configure(db, DD_APP, "Darkest Dungeon", tmp_path / "DDMods")
    library = tmp_path / "library"
    source, _pk, frozen = _seed(
        db,
        library,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder="LeperSkin",
        workspace_id="3129401071",
        display_name="麻风病人皮肤",
    )
    source_xml = (source / "project.xml").read_bytes()
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"])
    assert (target / "project.xml").read_text(encoding="utf-8").count(
        "<Title>麻风病人皮肤</Title>"
    ) == 1
    assert (source / "project.xml").read_bytes() == source_xml
    assert "Cloud Leper Skin" in source_xml.decode("utf-8")
    assert (target / "project.xml").read_text(encoding="utf-8").find(
        "<PreviewIconFile>preview_icon.png</PreviewIconFile>"
    ) >= 0
    timing = result.get("deploy_timing") or {}
    assert "project_title_sync_ms" in timing
    assert 0.0 <= float(timing["project_title_sync_ms"]) < 200.0
    manifest = load_manifest(source)
    assert manifest is not None
    entry = next(
        f
        for f in manifest.files
        if Path(str(f.target)).name.lower() == "project.xml"
        or str(getattr(f, "relative", "") or "").replace("\\", "/").lower()
        == "project.xml"
    )
    digest = hashlib.sha256((target / "project.xml").read_bytes()).hexdigest()
    assert entry.source_hash == digest
    assert digest != hashlib.sha256(source_xml).hexdigest()
    assert not (mods / "LeperSkin").exists() or target.name == "LeperSkin"


def test_non_dd_does_not_rewrite_title(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure(db, OTHER_APP, "SomeGame", tmp_path / "OtherMods")
    library = tmp_path / "library"
    source, _pk, frozen = _seed(
        db,
        library,
        app_id=OTHER_APP,
        game_name="SomeGame",
        folder="OtherMod",
        workspace_id="999001",
        display_name="Should Not Apply",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"])
    text = (target / "project.xml").read_text(encoding="utf-8")
    assert "<Title>Cloud Leper Skin</Title>" in text
    assert "Should Not Apply" not in text
    assert (source / "project.xml").read_text(encoding="utf-8") == PROJECT_XML


def test_missing_project_xml_skips(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure(db, DD_APP, "Darkest Dungeon", tmp_path / "DDSkip")
    library = tmp_path / "library"
    source = library / "Darkest Dungeon" / "NoXml"
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / "a.txt").write_text("x", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id="888001",
        title="NoXml",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title="NoXml",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        extra={"display_name": "显示名"},
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(
        str(created.internal_id)
    )
    assert result["success"] is True, result
    assert (Path(result["target"]) / "a.txt").is_file()
    assert not (Path(result["target"]) / "project.xml").exists()


def test_missing_title_element_skips(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure(db, DD_APP, "Darkest Dungeon", tmp_path / "DDNoTitle")
    library = tmp_path / "library"
    xml = "<project>\n<Language>english</Language>\n</project>\n"
    source, _pk, frozen = _seed(
        db,
        library,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder="NoTitle",
        workspace_id="888002",
        xml=xml,
        display_name="显示名",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target_text = (Path(result["target"]) / "project.xml").read_text(encoding="utf-8")
    assert target_text == xml
    assert (source / "project.xml").read_text(encoding="utf-8") == xml
    assert "<Title>" not in target_text


def test_abigail_ascii_folder_and_title(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mods = _configure(db, DD_APP, "Darkest Dungeon", tmp_path / "DDAbigail")
    library = tmp_path / "library"
    display = "The Abigail Williams Class"
    source, _pk, frozen = _seed(
        db,
        library,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder="The Abigail Williams Class",
        workspace_id="3308841144",
        display_name=display,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"])
    assert target.name == "The Abigail Williams Class"
    assert target == (mods / "The Abigail Williams Class").resolve()
    assert not (mods / "mod_3308841144").exists()
    assert f"<Title>{display}</Title>" in (target / "project.xml").read_text(
        encoding="utf-8"
    )
    assert "<Title>Cloud Leper Skin</Title>" in (source / "project.xml").read_text(
        encoding="utf-8"
    )
    leftover_archives = list(target.glob("*.7z")) + list(target.glob("*.zip")) + list(
        target.glob("*.rar")
    )
    assert leftover_archives == []
    assert not (target / INFO_DIR_NAME).exists()


def test_han_folder_keeps_mod_workspace_and_syncs_title(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mods = _configure(db, DD_APP, "Darkest Dungeon", tmp_path / "DDHan")
    library = tmp_path / "library"
    source, _pk, frozen = _seed(
        db,
        library,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder="死而复生 Resurrection event",
        workspace_id="2511735990",
        display_name="死而复生",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"])
    assert target.name == "mod_2511735990"
    assert target == (mods / "mod_2511735990").resolve()
    assert (target / "project.xml").is_file()
    assert "<Title>死而复生</Title>" in (target / "project.xml").read_text(
        encoding="utf-8"
    )
    assert "<Title>Cloud Leper Skin</Title>" in (source / "project.xml").read_text(
        encoding="utf-8"
    )


def test_production_capability_is_dd_only() -> None:
    assert supports_game_capability(DD_APP, CAPABILITY_SYNC_PROJECT_XML_TITLE) is True
    assert supports_game_capability(289070, CAPABILITY_SYNC_PROJECT_XML_TITLE) is False
    assert supports_game_capability(OTHER_APP, CAPABILITY_SYNC_PROJECT_XML_TITLE) is False
