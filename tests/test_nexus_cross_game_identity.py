"""Nexus import duplicate detection scoped by game app_id."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.importers.duplicate_check import (
    DUPLICATE_STATUS,
    check_import_duplicate,
)
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.offline_html_batch import make_empty_mod_stub
from ui.import_thread import ImportWorker

STARDEW_APP = 413150
BG3_APP = 990001


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "nexus_identity.db")
    manager.upsert_game(
        GameInfo(app_id=STARDEW_APP, name="Stardew Valley", folder_name="Stardew Valley")
    )
    manager.upsert_game(
        GameInfo(app_id=BG3_APP, name="Baldurs Gate 3", folder_name="Baldurs Gate 3")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "mod.dll").write_bytes(b"x")
    return folder


def _bg3_offline_html(tmp_path: Path) -> Path:
    html = tmp_path / "bg3_mod.html"
    html.write_text(
        """<!DOCTYPE html>
<html><head>
<meta property="og:url" content="https://www.nexusmods.com/baldursgate3/mods/6183"/>
<meta property="og:title" content="Sit This One Out 2"/>
</head><body><section data-mod-id="6183"></section></body></html>
""",
        encoding="utf-8",
    )
    return html


def test_case1_cross_game_same_nexus_id_allowed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Stardew 6183 + BG3 6183 may coexist (different app_id)."""
    lib = tmp_path / "lib"
    lib.mkdir()

    stardew_ctx = ImportContext(game_id=STARDEW_APP, game_name="Stardew Valley")
    bg3_ctx = ImportContext(game_id=BG3_APP, game_name="Baldurs Gate 3")

    first = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "TrainStation"),
        title="Train Station",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/6183",
        nexus_id="6183",
        library_root=lib,
        context=stardew_ctx,
    )
    assert first.success, first.error

    second = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "SitThisOneOut"),
        title="Sit This One Out 2",
        nexus_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        nexus_id="6183",
        library_root=lib,
        context=bg3_ctx,
    )
    assert second.success, second.error
    assert first.mod_id != second.mod_id


def test_case2_same_game_same_nexus_id_duplicate(
    tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    ctx = ImportContext(game_id=BG3_APP, game_name="Baldurs Gate 3")

    first = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "ModA"),
        title="Sit This One Out 2",
        nexus_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        nexus_id="6183",
        library_root=lib,
        context=ctx,
    )
    assert first.success

    materialize = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.importers.nexus.materialize_imported_mod",
            materialize,
        )
        again = NexusImporter(db=db).import_mod(
            source_folder=_folder(tmp_path, "ModB"),
            title="Duplicate",
            nexus_url="https://www.nexusmods.com/baldursgate3/mods/6183",
            nexus_id="6183",
            library_root=lib,
            context=ctx,
        )

    assert again.is_duplicate
    assert again.status == DUPLICATE_STATUS
    materialize.assert_not_called()


def test_case3_different_source_url_same_external_id_not_duplicate(
    tmp_path: Path, db: DatabaseManager
) -> None:
    dup = check_import_duplicate(
        db,
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        app_id=BG3_APP,
    )
    assert dup is None

    db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/stardewvalley/mods/6183",
        title="Train Station",
        app_id=STARDEW_APP,
        game_name="Stardew Valley",
    )

    dup = check_import_duplicate(
        db,
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        app_id=BG3_APP,
    )
    assert dup is None


def test_offline_html_import_cross_game_not_duplicate(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline MHTML identity for BG3 must not collide with Stardew 6183."""
    lib = tmp_path / "lib"
    lib.mkdir()
    stardew_ctx = ImportContext(game_id=STARDEW_APP, game_name="Stardew Valley")

    NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "TrainStation"),
        title="Train Station",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/6183",
        nexus_id="6183",
        library_root=lib,
        context=stardew_ctx,
    )

    html = _bg3_offline_html(tmp_path)
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "folder": str(make_empty_mod_stub(ident=html.stem)),
            "nexus_url": "",
            "nexus_id": "",
            "offline_html_path": str(html),
            "game_id": BG3_APP,
            "game_name": "Baldurs Gate 3",
            "context": ImportContext(game_id=BG3_APP, game_name="Baldurs Gate 3"),
        },
    )
    result = worker._import_one_folder(
        Path(worker.params["folder"]),
        offline_html=str(html),
        batch=False,
    )
    assert result.success, result.error
    assert result.mod_id
