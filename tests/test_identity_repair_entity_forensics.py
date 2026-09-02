"""Entity-lifecycle rule: confirmed invalid Mods are deleted, not merged or conflicted."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import identity_create_scope
from services.identity_repair import (
    ACTION_CONFLICT,
    ACTION_MERGE,
    ACTION_REMOVE_INVALID,
    apply_identity_repair,
    plan_identity_repair,
    summarize_sqlite_findings,
)
from services.library_reconcile import reconcile_library


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "forensics.db")
    manager.upsert_game(GameInfo(app_id=3167020, name="Duckov", folder_name="Duckov"))
    manager.upsert_game(GameInfo(app_id=413150, name="Stardew Valley", folder_name="SV"))
    yield manager
    DatabaseManager.reset_instance()


def _folder(library: Path, game: str, name: str, payload: bool = True) -> Path:
    folder = library / game / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text("{}", encoding="utf-8")
    if payload:
        (folder / "payload.bin").write_bytes(b"mod")
    return folder


def _plant_internal(
    db: DatabaseManager,
    *,
    folder: Path,
    platform: str,
    external_id: str,
    source_url: str,
    app_id: int,
    title: str,
) -> str:
    with identity_create_scope(), db._lock:
        mid = int(db.allocate_mod_id())
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=?, title=?, external_id=?,
                   source_url=?, last_known_path=?, folder_present=1, display_name=?
            WHERE mod_id=?
            """,
            (platform, app_id, title, external_id, source_url, str(folder.resolve()), title, mid),
        )
        db._conn.commit()
    return str(mid)


def _canonical_steam(db: DatabaseManager, folder: Path, steam_id: str) -> None:
    db.upsert_mod(ModMetadata(published_file_id=steam_id, title="Live", app_id=3167020))
    db.update_mod_identity_fields(
        int(steam_id),
        last_known_path=str(folder.resolve()),
        app_id=3167020,
        platform=PLATFORM_STEAM,
        external_id=steam_id,
        source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam_id}",
    )


def _plant_ghost(db: DatabaseManager, folder: Path, ghost_id: str) -> None:
    with identity_create_scope(), db._lock:
        db._ensure_mod_stub(int(ghost_id))
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=?, title=?, external_id=?,
                   workspace_id=?, last_known_path=?, folder_present=1
            WHERE mod_id=?
            """,
            (
                PLATFORM_STEAM,
                3167020,
                folder.name,
                f"stub:{ghost_id}",
                "",
                str(folder.resolve()),
                int(ghost_id),
            ),
        )
        db._conn.commit()


def test_1_steam_ghost_is_remove_not_merge(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    steam = "3591453758"
    live = _folder(library, "Duckov", "Collectibles")
    leftover = _folder(library, "Duckov", f"Unknown Mod {steam}", payload=False)
    _canonical_steam(db, live, steam)
    _plant_ghost(db, leftover, "9000000000003438")
    plan = plan_identity_repair(db, library)
    ghost = next(c for c in plan.candidates if c.ghost_mod_id == "9000000000003438")
    assert ghost.proposed_action == ACTION_REMOVE_INVALID
    assert ghost.proposed_action != ACTION_MERGE
    assert ghost.proposed_action != ACTION_CONFLICT
    assert ghost.candidate_mod_id == steam


def test_2_unknown_mod_placeholder_removed(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    steam = "3592539424"
    live = _folder(library, "Duckov", "Live")
    leftover = _folder(library, "Duckov", f"Unknown_Mod_{steam}", payload=False)
    _canonical_steam(db, live, steam)
    _plant_ghost(db, leftover, "9000000000003439")
    plan = plan_identity_repair(db, library)
    ghost = next(c for c in plan.candidates if c.ghost_mod_id == "9000000000003439")
    assert ghost.proposed_action == ACTION_REMOVE_INVALID


def test_3_stub_internal_identity_removed(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    shared = _folder(library, "SV", "Official")
    original = _plant_internal(
        db,
        folder=shared,
        platform=PLATFORM_NEXUS,
        external_id="44639",
        source_url="https://www.nexusmods.com/stardewvalley/mods/44639",
        app_id=413150,
        title="Official",
    )
    invalid = _plant_internal(
        db,
        folder=shared,
        platform=PLATFORM_NEXUS,
        external_id="stub:tmp",
        source_url="https://www.nexusmods.com/stardewvalley/mods/44639",
        app_id=0,
        title="Stub",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET external_id=? WHERE mod_id=?",
            (f"stub:{invalid}", int(invalid)),
        )
        db._conn.commit()
    plan = plan_identity_repair(db, library)
    victim = next(c for c in plan.candidates if c.ghost_mod_id == invalid)
    assert victim.proposed_action == ACTION_REMOVE_INVALID
    result = apply_identity_repair(db, library, plan, apply=True, quarantine_root=tmp_path / "q")
    assert result.success
    assert db.get_mod(invalid) is None
    assert db.get_mod(original) is not None


def test_4_duplicate_external_identity_via_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    a = _folder(library, "SV", "Keep")
    b = _folder(library, "SV", "Later")
    original = _plant_internal(
        db, folder=a, platform=PLATFORM_NEXUS, external_id="stub:a",
        source_url="https://www.nexusmods.com/stardewvalley/mods/9633",
        app_id=413150, title="Keep",
    )
    invalid = _plant_internal(
        db, folder=b, platform=PLATFORM_NEXUS, external_id="9633",
        source_url="https://www.nexusmods.com/stardewvalley/mods/9633",
        app_id=413150, title="Later",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET external_id=? WHERE mod_id=?",
            (f"stub:{original}", int(original)),
        )
        db._conn.commit()
    plan = plan_identity_repair(db, library)
    victim = next(c for c in plan.candidates if c.ghost_mod_id == invalid)
    assert victim.proposed_action == ACTION_REMOVE_INVALID
    assert victim.candidate_mod_id == original


def test_5_duplicate_source_url_removed(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    url = "https://www.nexusmods.com/stardewvalley/mods/11111"
    a = _folder(library, "SV", "A")
    b = _folder(library, "SV", "B")
    original = _plant_internal(
        db, folder=a, platform=PLATFORM_NEXUS, external_id="11111",
        source_url=url, app_id=413150, title="A",
    )
    invalid = _plant_internal(
        db, folder=b, platform=PLATFORM_NEXUS, external_id="stub:b",
        source_url=url, app_id=0, title="B",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET external_id=? WHERE mod_id=?",
            (f"stub:{invalid}", int(invalid)),
        )
        db._conn.commit()
    plan = plan_identity_repair(db, library)
    victim = next(c for c in plan.candidates if c.ghost_mod_id == invalid)
    assert victim.finding_class == "duplicate_source_url"
    assert victim.proposed_action == ACTION_REMOVE_INVALID
    assert victim.candidate_mod_id == original


def test_6_duplicate_without_source_url_still_removed(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    shared = _folder(library, "SV", "SharedNoUrl")
    original = _plant_internal(
        db, folder=shared, platform=PLATFORM_NEXUS, external_id="22222",
        source_url="", app_id=413150, title="Official",
    )
    invalid = _plant_internal(
        db, folder=shared, platform=PLATFORM_NEXUS, external_id="stub:x",
        source_url="", app_id=0, title="Stub",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET external_id=? WHERE mod_id=?",
            (f"stub:{invalid}", int(invalid)),
        )
        db._conn.commit()
    plan = plan_identity_repair(db, library)
    victim = next(c for c in plan.candidates if c.ghost_mod_id == invalid)
    assert victim.proposed_action == ACTION_REMOVE_INVALID
    assert victim.finding_class == "shared_folder_stub"
    assert victim.candidate_mod_id == original


def test_apply_deletes_ghosts_keeps_canonical(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    ghosts = [str(9000000000003438 + i) for i in range(13)]
    steams = [str(3591453758 + i) for i in range(13)]
    before_canon = {}
    for ghost_id, steam in zip(ghosts, steams, strict=True):
        live = _folder(library, "Duckov", f"Live {steam}")
        leftover = _folder(library, "Duckov", f"Unknown Mod {steam}")
        _canonical_steam(db, live, steam)
        _plant_ghost(db, leftover, ghost_id)
        before_canon[steam] = db._conn.execute(
            "SELECT platform, external_id, workspace_id, source_url, display_name, last_known_path FROM mods WHERE mod_id=?",
            (int(steam),),
        ).fetchone()
    result = apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert result.success
    for ghost_id, steam in zip(ghosts, steams, strict=True):
        assert db.get_mod(ghost_id) is None
        row = db._conn.execute(
            "SELECT platform, external_id, workspace_id, source_url, display_name, last_known_path FROM mods WHERE mod_id=?",
            (int(steam),),
        ).fetchone()
        for col in ("platform", "external_id", "workspace_id", "source_url", "display_name", "last_known_path"):
            assert str(row[col] or "") == str(before_canon[steam][col] or "")
        assert Path(str(before_canon[steam]["last_known_path"])).is_dir()
    after = summarize_sqlite_findings(db)
    assert after.get("CRITICAL") == 0
    assert after.get("HIGH") == 0


def test_lifecycle_isolation_source() -> None:
    src = Path("services/identity_repair.py").read_text(encoding="utf-8")
    assert "reconcile_library(" not in src
    assert "create_mod_identity(" not in src
    assert "db.allocate_mod_id" not in src
    assert inspect.isfunction(reconcile_library)


def test_no_merge_in_entity_plan(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    steam = "3781246892"
    live = _folder(library, "Duckov", "Live")
    leftover = _folder(library, "Duckov", f"Unknown Mod {steam}")
    _canonical_steam(db, live, steam)
    _plant_ghost(db, leftover, "9000000000003440")
    plan = plan_identity_repair(db, library)
    assert all(c.proposed_action != ACTION_MERGE for c in plan.candidates)
    assert all(c.proposed_action != ACTION_CONFLICT for c in plan.candidates if c.ghost_mod_id == "9000000000003440")
