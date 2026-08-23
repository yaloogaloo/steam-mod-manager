"""Import identity gate — resolve identity before duplicate / materialize / write."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager, GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.importers.identity_resolve import (
    MISSING_OFFICIAL_IDENTITY,
    parse_offline_page_identity,
    resolve_import_identity,
)
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

_HTML = """<!DOCTYPE html>
<html><head>
<meta property="og:url" content="https://www.nexusmods.com/stardewvalley/mods/21381"/>
<meta property="og:title" content="Stardew Valley Map Teleport"/>
</head><body>
<section class="modpage" data-mod-id="21381" data-game-id="1303"></section>
</body></html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_gate.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "mod.pak").write_bytes(b"x")
    return folder


def _write_offline_html(tmp_path: Path) -> Path:
    path = tmp_path / "offline_page.html"
    path.write_text(_HTML, encoding="utf-8")
    return path


def test_case1_offline_html_only_duplicates_existing(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing nexus_id=21381 + offline HTML only → duplicate, no new DB row."""
    lib = tmp_path / "lib"
    first = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "existing"),
        title="Map Teleport",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/21381",
        nexus_id="21381",
        library_root=lib,
        context=PALWORLD,
    )
    assert first.success
    before = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]

    html = _write_offline_html(tmp_path)
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    materialize = MagicMock()
    monkeypatch.setattr(
        "services.importers.nexus.materialize_imported_mod", materialize
    )
    register = MagicMock(wraps=db.register_external_mod)
    monkeypatch.setattr(db, "register_external_mod", register)

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "folder": str(_folder(tmp_path, "Empty Mod d6e406e8")),
            "nexus_url": "",
            "nexus_id": "",
            "offline_html_path": str(html),
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._import_one_folder(
        Path(worker.params["folder"]),
        offline_html=str(html),
        batch=False,
    )
    assert result.is_duplicate
    assert result.success is False
    materialize.assert_not_called()
    register.assert_not_called()
    after = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert after == before


def test_case2_identity_before_materialize(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline HTML identity must be resolved before materialize."""
    html = _write_offline_html(tmp_path)
    order: list[str] = []

    NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "seed"),
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/21381",
        nexus_id="21381",
        library_root=tmp_path / "lib",
        context=PALWORLD,
    )

    real_parse = parse_offline_page_identity

    def tracking_parse(path):
        order.append("identity")
        return real_parse(path)

    def tracking_materialize(*_a, **_k):
        order.append("materialize")
        raise AssertionError("materialize must not run on duplicate")

    monkeypatch.setattr(
        "services.importers.identity_resolve.parse_offline_page_identity",
        tracking_parse,
    )
    monkeypatch.setattr(
        "services.importers.nexus.materialize_imported_mod",
        tracking_materialize,
    )
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=tmp_path / "lib",
        params={
            "folder": str(_folder(tmp_path, "Empty Mod x")),
            "offline_html_path": str(html),
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._import_one_folder(
        Path(worker.params["folder"]),
        offline_html=str(html),
        batch=False,
    )
    assert result.is_duplicate
    assert order[0] == "identity"
    assert "materialize" not in order


def test_case3_duplicate_skips_register_and_workspace(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _write_offline_html(tmp_path)
    NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "a"),
        nexus_id="21381",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/21381",
        library_root=tmp_path / "lib",
        context=PALWORLD,
    )
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    gen = MagicMock(return_value="SHOULD_NOT_GENERATE")
    monkeypatch.setattr(
        "core.mod_platform.generate_unique_workspace_id", gen
    )
    reg = MagicMock()
    monkeypatch.setattr(db, "register_external_mod", reg)

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=tmp_path / "lib",
        params={
            "folder": str(_folder(tmp_path, "Empty Mod y")),
            "offline_html_path": str(html),
            "context": PALWORLD,
            "game_id": 1623730,
            "game_name": "Palworld",
        },
    )
    result = worker._import_one_folder(
        Path(worker.params["folder"]),
        offline_html=str(html),
        batch=False,
    )
    assert result.is_duplicate
    reg.assert_not_called()
    gen.assert_not_called()


def test_case4_new_nexus_mod_imports_ok(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "lib"
    result = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "brand_new"),
        title="New Mod",
        nexus_url="https://www.nexusmods.com/palworld/mods/999001",
        nexus_id="999001",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success
    assert result.external_id == "999001"
    assert "999001" in result.source_url
    info = db.get_mod_display_info(result.mod_id)
    assert info is not None
    assert info.external_id == "999001"
    assert info.workspace_id == "999001"


def test_resolve_offline_html_fills_identity(tmp_path: Path) -> None:
    html = _write_offline_html(tmp_path)
    resolved = resolve_import_identity(
        PLATFORM_NEXUS,
        nexus_url="",
        nexus_id="",
        offline_html=str(html),
        allow_local_fallback=False,
    )
    assert not isinstance(resolved, type(None))
    from services.importers.identity_resolve import ImportIdentity

    assert isinstance(resolved, ImportIdentity)
    assert resolved.external_id == "21381"
    assert resolved.source_url == "https://www.nexusmods.com/stardewvalley/mods/21381"


def test_nexus_without_identity_refused(tmp_path: Path, db: DatabaseManager) -> None:
    result = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "Empty Mod z"),
        nexus_url="",
        nexus_id="",
        library_root=tmp_path / "lib",
        context=PALWORLD,
    )
    assert not result.success
    assert result.error == MISSING_OFFICIAL_IDENTITY
