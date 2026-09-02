"""Identity production repair planner + gated apply."""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_STEAM, is_internal_mod_id
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import identity_create_scope, repair_no_allocate_scope
from services.identity_repair import (
    ACTION_CONFLICT,
    ACTION_MERGE,
    ACTION_REMOVE_INVALID,
    CONF_HIGH,
    CONF_MEDIUM,
    REL_AMBIGUOUS,
    REL_DUPLICATE_ENTITY,
    REL_INTERNAL_POLLUTION,
    apply_identity_repair,
    plan_identity_repair,
)
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_mod_identity

STEAM_ID = "3591453758"
GHOST_ID = "9000000000003438"
OTHER_STEAM = "3591459999"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "repair.db")
    manager.upsert_game(
        GameInfo(app_id=3167020, name="逃离鸭科夫", folder_name="逃离鸭科夫")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_folder(library: Path, name: str, published_file_id: str, extra: dict | None = None) -> Path:
    folder = library / "逃离鸭科夫" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": published_file_id,
        "title": name,
        "app_id": 3167020,
        "source_type": "steam",
    }
    if extra:
        payload.update(extra)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "payload.bin").write_bytes(b"mod-bytes")
    return folder


def _plant_ghost(
    db: DatabaseManager,
    *,
    folder: Path,
    ghost_id: str = GHOST_ID,
    published_file_id: str = GHOST_ID,
    source_url: str = "",
    workspace_id: str | None = None,
) -> None:
    with identity_create_scope(), db._lock:
        db._ensure_mod_stub(int(ghost_id))
        db._conn.execute(
            """
            UPDATE mods SET
                platform = ?, app_id = ?, title = ?, external_id = ?,
                workspace_id = ?, source_url = ?, last_known_path = ?,
                folder_present = 1
            WHERE mod_id = ?
            """,
            (
                PLATFORM_STEAM,
                3167020,
                folder.name,
                f"stub:{ghost_id}",
                workspace_id if workspace_id is not None else ghost_id,
                source_url,
                str(folder.resolve()),
                int(ghost_id),
            ),
        )
        db._conn.commit()
    meta_path = folder / INFO_DIR_NAME / METADATA_FILENAME
    if meta_path.is_file():
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["published_file_id"] = published_file_id
        meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _canonical(db: DatabaseManager, folder: Path, steam_id: str = STEAM_ID) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=steam_id,
            title="更多收集品1.9b [AdditionalCollectibles]",
            app_id=3167020,
        )
    )
    db.update_mod_identity_fields(
        int(steam_id),
        last_known_path=str(folder.resolve()),
        app_id=3167020,
        platform=PLATFORM_STEAM,
        external_id=steam_id,
        source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam_id}",
    )


def test_1_known_incident_classified_as_duplicate_pollution(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    live = _write_folder(library, "更多收集品", STEAM_ID)
    leftover = _write_folder(
        library, f"Unknown Mod {STEAM_ID}", GHOST_ID, extra={"title": f"Unknown Mod {STEAM_ID}"}
    )
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    plan = plan_identity_repair(db, library)
    ghost = next(c for c in plan.candidates if c.ghost_mod_id == GHOST_ID)
    assert REL_DUPLICATE_ENTITY in ghost.relationships
    assert REL_INTERNAL_POLLUTION in ghost.relationships
    assert ghost.candidate_mod_id == STEAM_ID
    assert ghost.proposed_action == ACTION_REMOVE_INVALID
    assert ghost.proposed_action != ACTION_MERGE
    assert ghost.confidence in (CONF_HIGH, CONF_MEDIUM)


def test_2_unrelated_steam_mods_do_not_merge_on_display_name(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    a = _write_folder(library, "Cool Mod A", STEAM_ID)
    b = _write_folder(library, "Cool Mod B", OTHER_STEAM)
    ghost_folder = _write_folder(library, "Cool Mod Ghost", GHOST_ID)
    _canonical(db, a)
    db.upsert_mod(
        ModMetadata(published_file_id=OTHER_STEAM, title="Cool Mod", app_id=3167020)
    )
    db.update_mod_identity_fields(
        int(OTHER_STEAM),
        last_known_path=str(b.resolve()),
        app_id=3167020,
        platform=PLATFORM_STEAM,
        external_id=OTHER_STEAM,
    )
    _plant_ghost(db, folder=ghost_folder)
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET title=? WHERE mod_id IN (?, ?)",
            ("Cool Mod", int(STEAM_ID), int(OTHER_STEAM)),
        )
        db._conn.execute(
            "UPDATE mods SET title=? WHERE mod_id=?",
            ("Cool Mod", int(GHOST_ID)),
        )
        db._conn.commit()
    plan = plan_identity_repair(db, library)
    ghost = next(c for c in plan.candidates if c.ghost_mod_id == GHOST_ID)
    assert ghost.proposed_action != ACTION_MERGE
    assert ghost.candidate_mod_id not in {STEAM_ID, OTHER_STEAM} or REL_AMBIGUOUS in ghost.relationships


def test_3_internal_id_never_written_as_steam_identity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    plan = plan_identity_repair(db, library)
    result = apply_identity_repair(
        db, library, plan, apply=True, quarantine_root=tmp_path / "q"
    )
    assert result.success
    info = db.get_mod_display_info(STEAM_ID)
    assert info is not None
    assert info.external_id == STEAM_ID
    assert info.workspace_id != GHOST_ID
    assert GHOST_ID not in (info.source_url or "")
    assert not is_internal_mod_id(info.external_id)
    meta = json.loads(
        (live / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert meta.get("published_file_id") != GHOST_ID
    assert not is_internal_mod_id(str(meta.get("published_file_id") or ""))


def test_4_repair_cannot_allocate(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    calls: list[int] = []
    real = DatabaseManager.allocate_mod_id

    def wrapped(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(DatabaseManager, "allocate_mod_id", wrapped)
    plan = plan_identity_repair(db, library)
    result = apply_identity_repair(
        db, library, plan, apply=True, quarantine_root=tmp_path / "q"
    )
    assert result.success
    assert calls == []
    assert result.allocations == 0


def test_5_ambiguous_two_canonicals_never_merge(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    other = _write_folder(library, "Alias", "3590001111")
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    db.upsert_mod(
        ModMetadata(published_file_id="3590001111", title="Alias", app_id=3167020)
    )
    db.update_mod_identity_fields(
        3590001111,
        last_known_path=str(other.resolve()),
        app_id=3167020,
        platform=PLATFORM_STEAM,
        external_id="3590001111",
        source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={STEAM_ID}",
    )
    _plant_ghost(db, folder=leftover)
    plan = plan_identity_repair(db, library)
    ghost = next(c for c in plan.candidates if c.ghost_mod_id == GHOST_ID)
    assert ghost.proposed_action != ACTION_MERGE
    assert ghost.proposed_action == ACTION_REMOVE_INVALID
    assert ghost.candidate_mod_id == STEAM_ID
    result = apply_identity_repair(
        db, library, plan, apply=True, quarantine_root=tmp_path / "q"
    )
    assert result.success
    assert db.get_mod(GHOST_ID) is None
    assert db.get_mod(STEAM_ID) is not None
    assert db.get_mod("3590001111") is not None


def test_6_apply_is_idempotent(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    q = tmp_path / "q"
    first = apply_identity_repair(
        db, library, apply=True, quarantine_root=q
    )
    assert first.success
    assert first.applied_counts.get("removed_invalid", 0) >= 1
    second = apply_identity_repair(
        db, library, apply=True, quarantine_root=q
    )
    assert second.success
    assert second.applied_counts.get("removed_invalid", 0) == 0
    assert db.get_mod(GHOST_ID) is None
    assert db.get_mod(STEAM_ID) is not None


def test_7_reference_migration(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    db.create_deployment_record(3167020, "set-a", [GHOST_ID])
    apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    with db._lock:
        rows = db._conn.execute(
            "SELECT mod_id FROM deployment_record_items"
        ).fetchall()
        mids = {str(r["mod_id"]) for r in rows}
    assert STEAM_ID in mids
    assert GHOST_ID not in mids


def test_8_filesystem_collision_quarantines_without_overwrite(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    (live / "canonical_only.txt").write_text("keep-me", encoding="utf-8")
    (leftover / "ghost_only.txt").write_text("preserve-me", encoding="utf-8")
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)
    q = tmp_path / "q"
    apply_identity_repair(db, library, apply=True, quarantine_root=q)
    assert (live / "canonical_only.txt").read_text(encoding="utf-8") == "keep-me"
    assert not leftover.exists()
    preserved = list(q.rglob("ghost_only.txt"))
    assert preserved
    assert preserved[0].read_text(encoding="utf-8") == "preserve-me"
    assert list(q.rglob("MANIFEST.json"))


def test_9_transaction_failure_rolls_back(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    live = _write_folder(library, "Collectibles", STEAM_ID)
    leftover = _write_folder(library, f"Unknown Mod {STEAM_ID}", GHOST_ID)
    _canonical(db, live)
    _plant_ghost(db, folder=leftover)

    def boom(_conn, ghost):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr("services.identity_repair._delete_invalid_mod_row", boom)
    plan = plan_identity_repair(db, library)
    result = apply_identity_repair(
        db, library, plan, apply=True, quarantine_root=tmp_path / "q"
    )
    assert not result.success
    assert db.get_mod(GHOST_ID) is not None
    assert db.get_mod(STEAM_ID) is not None
    assert leftover.is_dir()


def test_10_lifecycle_does_not_invoke_repair_or_mint(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [
        Path("services/mod_refresh.py").read_text(encoding="utf-8"),
        Path("services/library_reconcile.py").read_text(encoding="utf-8"),
        Path("services/deploy.py").read_text(encoding="utf-8"),
        Path("services/offline/manager.py").read_text(encoding="utf-8"),
        inspect.getsource(ensure_mod_identity),
    ]
    for src in sources:
        assert "plan_identity_repair" not in src
        assert "apply_identity_repair" not in src
        assert "services.identity_repair" not in src

    called = []

    def forbidden(*_a, **_k):
        called.append(1)
        raise AssertionError("repair invoked from lifecycle")

    monkeypatch.setattr("services.identity_repair.plan_identity_repair", forbidden)
    monkeypatch.setattr("services.identity_repair.apply_identity_repair", forbidden)

    library = tmp_path / "mod"
    leftover = library / "逃离鸭科夫" / f"Unknown Mod {STEAM_ID}"
    leftover.mkdir(parents=True)
    (leftover / "info.ini").write_text("[Mod]\nname=x\n", encoding="utf-8")
    before = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert after == before
    assert called == []
    assert db.get_mod(GHOST_ID) is None


def test_resolve_steam_workshop_external_id_not_internal(
    db: DatabaseManager,
) -> None:
    from services.metadata_refresh import resolve_steam_workshop_external_id

    assert resolve_steam_workshop_external_id(db, STEAM_ID) == STEAM_ID
    assert resolve_steam_workshop_external_id(db, GHOST_ID) == ""


def test_repair_no_allocate_scope_raises(db: DatabaseManager) -> None:
    with repair_no_allocate_scope():
        with pytest.raises(Exception):
            db.allocate_mod_id()
