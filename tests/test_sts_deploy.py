"""Slay the Spire deploy: jars → mods/; ModTheSpire → game root."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_SLAY_THE_SPIRE,
    load_manifest,
    resolve_deploy_type,
)
from services.deploy_rules.slay_the_spire import (
    PREREQUISITE_WORKSPACE_ID,
    SLAY_THE_SPIRE_APP_ID,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

STS_APP = SLAY_THE_SPIRE_APP_ID


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "sts_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_resolve_sts_deploy_type() -> None:
    assert resolve_deploy_type(STS_APP, "folder_copy") == DEPLOY_TYPE_SLAY_THE_SPIRE
    assert resolve_deploy_type(1623730, "folder_copy") == "palworld_pak"


def test_sts_jar_deploys_into_mods(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    install = tmp_path / "StSInstall"
    install.mkdir()
    assert not (install / "mods").exists()

    mod = library / "杀戮尖塔" / "AddAnimeVoice"
    mod.mkdir(parents=True)
    (mod / "AddAnimeVoice.jar").write_bytes(b"jar-bytes")
    (mod / "readme.txt").write_text("skip", encoding="utf-8")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        '  "published_file_id": "3574381350",\n'
        '  "workspace_id": "3574381350",\n'
        '  "title": "AddAnimeVoice",\n'
        f'  "app_id": {STS_APP},\n'
        '  "game_name": "杀戮尖塔"\n'
        "}\n",
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        STS_APP,
        name="杀戮尖塔",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="3574381350",
            title="AddAnimeVoice",
            app_id=STS_APP,
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("3574381350")
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_SLAY_THE_SPIRE
    assert (install / "mods" / "AddAnimeVoice.jar").is_file()
    assert not (install / "AddAnimeVoice.jar").exists()
    assert not (install / "mods" / "readme.txt").exists()

    man = load_manifest(mod)
    assert man is not None
    assert len(man.files) == 1
    assert Path(man.files[0].target) == (install / "mods" / "AddAnimeVoice.jar").resolve()


def test_sts_modthespire_deploys_to_game_root(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "StSInstall"
    install.mkdir()

    mid = PREREQUISITE_WORKSPACE_ID
    mod = library / "杀戮尖塔" / "ModTheSpire"
    mod.mkdir(parents=True)
    (mod / "ModTheSpire.jar").write_bytes(b"loader")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "workspace_id": "{mid}",\n'
        '  "title": "ModTheSpire",\n'
        '  "category": "前置",\n'
        f'  "app_id": {STS_APP}\n'
        "}\n",
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        STS_APP,
        name="杀戮尖塔",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="ModTheSpire", app_id=STS_APP)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    assert (install / "ModTheSpire.jar").is_file()
    assert not (install / "mods" / "ModTheSpire.jar").exists()


def test_sts_basemod_category_qianzhi_still_uses_mods(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Category 前置 alone must not force game-root deploy (BaseMod)."""
    library = tmp_path / "library"
    install = tmp_path / "StSInstall"
    install.mkdir()

    mod = library / "杀戮尖塔" / "BaseMod"
    mod.mkdir(parents=True)
    (mod / "BaseMod.jar").write_bytes(b"base")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        '  "published_file_id": "1605833019",\n'
        '  "workspace_id": "1605833019",\n'
        '  "title": "BaseMod",\n'
        '  "category": "前置",\n'
        f'  "app_id": {STS_APP}\n'
        "}\n",
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        STS_APP,
        name="杀戮尖塔",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="1605833019", title="BaseMod", app_id=STS_APP
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("1605833019")
    assert result["success"] is True, result
    assert (install / "mods" / "BaseMod.jar").is_file()
    assert not (install / "BaseMod.jar").exists()
