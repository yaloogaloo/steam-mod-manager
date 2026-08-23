"""Batch offline HTML file import — one HTML = one Mod task."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager, GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.importers.directory_batch import discover_mod_directories
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.offline_html_batch import (
    make_empty_mod_stub,
    normalize_offline_html_paths,
)
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

_HTML_A = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/palworld/mods/111"/>
<meta property="og:title" content="Mod Alpha"/>
</head><body><section data-mod-id="111"></section></body></html>
"""

_HTML_B = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/palworld/mods/222"/>
<meta property="og:title" content="Mod Beta"/>
</head><body><section data-mod-id="222"></section></body></html>
"""

_HTML_C = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/palworld/mods/333"/>
<meta property="og:title" content="Mod Gamma"/>
</head><body><section data-mod-id="333"></section></body></html>
"""

_HTML_DUP = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/stardewvalley/mods/21381"/>
<meta property="og:title" content="Stardew Valley Map Teleport"/>
</head><body><section data-mod-id="21381"></section></body></html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "batch_html.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_case1_multiple_html_files_create_tasks(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        _write(tmp_path, "a.html", _HTML_A),
        _write(tmp_path, "b.html", _HTML_B),
        _write(tmp_path, "c.html", _HTML_C),
    ]
    assert len(normalize_offline_html_paths([str(p) for p in files])) == 3

    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=tmp_path / "lib",
        params={
            "offline_html_paths": [str(p) for p in files],
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._do_import()
    assert result.success
    assert int(result.imported_count or 0) == 3
    assert int(result.skipped_count or 0) == 0
    assert int(result.failed_count or 0) == 0
    rows = db._conn.execute(
        "SELECT external_id FROM mods WHERE platform='nexus' ORDER BY external_id"
    ).fetchall()
    assert [r["external_id"] for r in rows] == ["111", "222", "333"]


def test_case2_duplicate_html_skipped(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    NexusImporter(db=db).import_mod(
        source_folder=make_empty_mod_stub(ident="seed"),
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/21381",
        nexus_id="21381",
        library_root=lib,
        context=PALWORLD,
    )
    before = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]

    html = _write(tmp_path, "21381.html", _HTML_DUP)
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "offline_html_paths": [str(html)],
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._do_import()
    assert result.success
    assert int(result.imported_count or 0) == 0
    assert int(result.skipped_count or 0) == 1
    after = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert after == before


def test_case3_bad_html_isolated(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _write(tmp_path, "good.html", _HTML_A)
    bad = _write(tmp_path, "bad.html", "<html><body>not a nexus page</body></html>")
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=tmp_path / "lib",
        params={
            "offline_html_paths": [str(good), str(bad)],
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._do_import()
    assert result.success
    assert int(result.imported_count or 0) == 1
    assert int(result.failed_count or 0) == 1
    rows = db._conn.execute(
        "SELECT external_id FROM mods WHERE platform='nexus'"
    ).fetchall()
    assert [r["external_id"] for r in rows] == ["111"]


def test_case4_directory_batch_regression(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "batch"
    for name in ("modA", "modB"):
        d = parent / name
        d.mkdir(parents=True)
        (d / "a.pak").write_bytes(b"1")
    assert len(discover_mod_directories(parent)) == 2

    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=tmp_path / "lib",
        params={
            "folder": str(parent),
            "is_batch_mode": True,
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._do_import()
    assert result.success
    assert int(result.imported_count or 0) == 2
