"""Runtime resource regression — cover + path cache after identity consumer migration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.cover_loader import resolve_cover_path
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.managed_path_cache import (
    get_cached_managed_path,
    invalidate_managed_path_cache,
    put_managed_path,
    reset_managed_path_cache_stats,
)
from services.mod_library_cache import build_library_snapshot
from services.path_lifecycle import discover_folder_by_internal_id, resolve_managed_folder

STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "resource_reg.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    invalidate_managed_path_cache()
    reset_managed_path_cache_stats()
    yield manager
    DatabaseManager.reset_instance()
    invalidate_managed_path_cache()


def _png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    workspace_id: str,
    app_id: int,
    game: str,
    title: str,
    with_published: bool,
) -> tuple[str, Path]:
    slug = "stardewvalley" if app_id == STARDEW else "baldursgate3"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=workspace_id,
        source_url=f"https://www.nexusmods.com/{slug}/mods/{workspace_id}",
        title=title,
        app_id=app_id,
        game_name=game,
        operation="import",
    )
    mid = str(created.mod_id)
    db.update_mod_identity_fields(mid, internal_id=mid)
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "internal_id": mid,
        "workspace_id": workspace_id,
        "platform": PLATFORM_NEXUS,
        "app_id": app_id,
        "title": title,
        "cover_path": ".info/cover.png",
    }
    if with_published:
        payload["published_file_id"] = workspace_id
    (info / METADATA_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    _png(info / "cover.png")
    (folder / "mod.bin").write_bytes(b"x")
    db.update_mod_identity_fields(
        mid, last_known_path=str(folder.resolve()), folder_present=True
    )
    db.update_mod_cover_path(mid, ".info/cover.png")
    return mid, folder


def test_1_mod_loads_without_published_file_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_mod(
        db,
        library,
        workspace_id="9001",
        app_id=STARDEW,
        game="Stardew",
        title="NoPub",
        with_published=False,
    )
    data = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert "published_file_id" not in data
    snap = build_library_snapshot(library)
    assert any(c.id == mid for c in snap.cards)
    resolved = resolve_managed_folder(mid, library_root=library, db=db)
    assert resolved.path is not None
    assert resolved.path.resolve() == folder.resolve()


def test_2_cover_loads_with_stable_internal_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_mod(
        db,
        library,
        workspace_id="9002",
        app_id=STARDEW,
        game="Stardew",
        title="HasCover",
        with_published=False,
    )
    found = resolve_cover_path(folder, ".info/cover.png")
    assert found is not None and found.is_file()
    found2 = resolve_cover_path(folder, "")
    assert found2 is not None and found2.is_file()
    assert mid


def test_3_library_load_does_not_rescan_all_mod_dirs(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    for i in range(3):
        _seed_mod(
            db,
            library,
            workspace_id=str(9100 + i),
            app_id=STARDEW,
            game="Stardew",
            title=f"Mod{i}",
            with_published=False,
        )
    reset_managed_path_cache_stats()
    with mock.patch(
        "services.path_lifecycle.discover_folder_by_internal_id",
        wraps=discover_folder_by_internal_id,
    ) as discovered:
        snap = build_library_snapshot(library)
        assert len(snap.cards) >= 3
        assert discovered.call_count == 0
    mid = snap.cards[0].id
    assert get_cached_managed_path(mid, library_root=library) is not None
    with mock.patch(
        "services.path_lifecycle.discover_folder_by_internal_id"
    ) as discovered2:
        resolve_managed_folder(mid, library_root=library, db=db)
        assert discovered2.call_count == 0


def test_4_same_workspace_digits_across_games_do_not_mix_covers(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid_a, folder_a = _seed_mod(
        db,
        library,
        workspace_id="1333",
        app_id=BG3,
        game="BG3",
        title="LibA",
        with_published=False,
    )
    mid_b, folder_b = _seed_mod(
        db,
        library,
        workspace_id="1333",
        app_id=STARDEW,
        game="Stardew",
        title="LibB",
        with_published=False,
    )
    cover_a = resolve_cover_path(folder_a, ".info/cover.png")
    cover_b = resolve_cover_path(folder_b, ".info/cover.png")
    assert cover_a is not None and cover_b is not None
    assert cover_a.resolve() != cover_b.resolve()
    assert mid_a != mid_b
    ra = resolve_managed_folder(mid_a, library_root=library, db=db)
    rb = resolve_managed_folder(mid_b, library_root=library, db=db)
    assert ra.path.resolve() == folder_a.resolve()
    assert rb.path.resolve() == folder_b.resolve()


def test_path_cache_invalidated_on_missing_dir(tmp_path: Path) -> None:
    invalidate_managed_path_cache()
    put_managed_path("42", tmp_path / "gone", library_root=tmp_path)
    assert get_cached_managed_path("42", library_root=tmp_path) is None
