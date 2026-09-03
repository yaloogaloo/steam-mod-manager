"""Nexus Offline HTML import must name the library folder after the parsed title."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict
from services.identity_service import is_empty_mod_placeholder
from services.importers.identity_resolve import canonical_nexus_offline_import_title
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.offline_html_batch import make_empty_mod_stub
from services.offline.manager import attach_nexus_offline_page
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

PARSED_TITLE = "Tales of The Witcher - NEW WORLD - Cintra DLC"
PARSED_MOD_ID = "99901"
PARSED_URL = f"https://www.nexusmods.com/witcher3/mods/{PARSED_MOD_ID}"

_OFFLINE_HTML = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{PARSED_TITLE} EARLY ACCESS at The Witcher 3 Nexus - Mods and community</title>
  <meta property="og:title" content="{PARSED_TITLE}"/>
  <meta property="og:url" content="{PARSED_URL}"/>
  <meta name="description" content="Cintra DLC for The Witcher 3."/>
</head>
<body class="site-nexusmods-b modpage">
  <section data-game-id="952" data-mod-id="{PARSED_MOD_ID}"></section>
  <h1>{PARSED_TITLE}</h1>
</body>
</html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_offline_dirname.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_html(tmp_path: Path, name: str = "page.html") -> Path:
    path = tmp_path / name
    path.write_text(_OFFLINE_HTML, encoding="utf-8")
    return path


def test_canonical_title_prefers_parsed_html_over_empty_mod_stub() -> None:
    assert (
        canonical_nexus_offline_import_title(
            user_title="",
            parsed_title=PARSED_TITLE,
            folder_name="Empty Mod 3fefdcf2",
        )
        == PARSED_TITLE
    )
    assert (
        canonical_nexus_offline_import_title(
            user_title="User Name",
            parsed_title=PARSED_TITLE,
            folder_name="Empty Mod 3fefdcf2",
        )
        == "User Name"
    )
    assert (
        canonical_nexus_offline_import_title(
            user_title="",
            parsed_title="",
            folder_name="Empty Mod 3fefdcf2",
        )
        == "Empty Mod 3fefdcf2"
    )


def test_nexus_offline_import_uses_parsed_mod_name_as_directory_name(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parsed Mod name == final directory name; Empty Mod <random> must not remain."""
    html = _write_html(
        tmp_path,
        "Tales-of-The-Witcher-NEW-WORLD-Cintra-DLC-EARLY-ACCESS-"
        "at-The-Witcher-3-Nexus-Mods-and-community.html",
    )
    stub = make_empty_mod_stub()
    assert is_empty_mod_placeholder(stub.name) or stub.name.startswith("Empty Mod")
    lib = tmp_path / "library"
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "folder": str(stub),
            "source_path": "",
            "use_archive": False,
            "nexus_url": "",
            "nexus_id": "",
            "title": "",
            "cover_source": "",
            "offline_html_path": str(html),
            "offline_clean": True,
            "is_batch_mode": False,
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD.as_dict(),
        },
    )
    result = worker._do_import()
    assert result.success, result.error

    final = Path(result.managed_path)
    assert final.is_dir()
    assert final.name == PARSED_TITLE
    assert not is_empty_mod_placeholder(final.name)
    leftovers = [
        p.name
        for p in final.parent.iterdir()
        if p.is_dir() and is_empty_mod_placeholder(p.name)
    ]
    assert leftovers == []

    info = db.get_mod_display_info(result.mod_id)
    assert info is not None
    assert info.external_id == PARSED_MOD_ID
    assert info.workspace_id == PARSED_MOD_ID
    assert info.source_url == PARSED_URL
    assert PARSED_TITLE in {
        str(info.steam_name or "").strip(),
        str(info.display_name or "").strip(),
    }
    # Internal Database ID is not a Nexus Mod ID / Workspace ID.
    assert str(info.mod_id) != PARSED_MOD_ID

    meta = read_info_metadata_dict(final) or {}
    sidecar_title = str(meta.get("title") or "").strip()
    assert sidecar_title == PARSED_TITLE or PARSED_TITLE in sidecar_title
    backup = db.get_mod_backup_row(result.mod_id) or {}
    assert Path(str(backup.get("last_known_path") or "")).resolve() == final.resolve()
    assert (final / INFO_DIR_NAME / "offline" / "index.html").is_file()


def test_attach_offline_html_renames_existing_empty_mod_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = tmp_path / "payload"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    lib = tmp_path / "library"
    imported = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="Empty Mod abcdef12",
        nexus_url=PARSED_URL,
        nexus_id=PARSED_MOD_ID,
        library_root=lib,
        context=PALWORLD,
    )
    assert imported.success, imported.error
    old = Path(imported.managed_path)
    assert is_empty_mod_placeholder(old.name)

    html = _write_html(tmp_path)
    attach_nexus_offline_page(
        imported.mod_id,
        html,
        managed_path=old,
        library_root=lib,
    )

    from services.path_lifecycle import resolve_managed_folder

    resolved = resolve_managed_folder(imported.mod_id, hint_path=old, db=db)
    assert resolved.path is not None
    assert resolved.path.name == PARSED_TITLE
    assert resolved.path.is_dir()
    assert not old.exists()
    backup = db.get_mod_backup_row(imported.mod_id) or {}
    assert Path(str(backup.get("last_known_path") or "")).resolve() == resolved.path.resolve()
    meta = read_info_metadata_dict(resolved.path) or {}
    sidecar_path = str(meta.get("managed_path") or "").strip()
    if sidecar_path:
        assert Path(sidecar_path).resolve() == resolved.path.resolve()


def test_offline_html_rename_conflict_keeps_both_directories(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = tmp_path / "payload"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    lib = tmp_path / "library"
    imported = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="Empty Mod abcdef12",
        nexus_url=PARSED_URL,
        nexus_id=PARSED_MOD_ID,
        library_root=lib,
        context=PALWORLD,
    )
    assert imported.success, imported.error
    empty = Path(imported.managed_path)
    assert is_empty_mod_placeholder(empty.name)

    blocker = empty.parent / PARSED_TITLE
    blocker.mkdir()
    (blocker / INFO_DIR_NAME).mkdir(parents=True)
    (blocker / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": "88888",
                "title": PARSED_TITLE,
                "workspace_id": "88888",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (blocker / "other.pak").write_bytes(b"other")

    html = _write_html(tmp_path)
    attach_nexus_offline_page(
        imported.mod_id,
        html,
        managed_path=empty,
        library_root=lib,
    )

    assert empty.is_dir()
    assert blocker.is_dir()
    assert (blocker / "other.pak").is_file()
    backup = db.get_mod_backup_row(imported.mod_id) or {}
    assert Path(str(backup.get("last_known_path") or "")).name == empty.name
