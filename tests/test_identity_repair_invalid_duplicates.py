"""Invalid duplicate removal: confirmed extras are deleted, originals untouched."""

from __future__ import annotations

import inspect
import json
import sqlite3
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
    ACTION_REMOVE_INVALID,
    REASON_INVALID_DUPLICATE,
    _count_dangling_mod_id,
    apply_identity_repair,
    plan_identity_repair,
    summarize_sqlite_findings,
)
from services.library_reconcile import reconcile_library


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "dup.db")
    manager.upsert_game(GameInfo(app_id=413150, name="Stardew Valley", folder_name="SV"))
    manager.upsert_game(GameInfo(app_id=916440, name="Anno 1800", folder_name="Anno 1800"))
    yield manager
    DatabaseManager.reset_instance()


def _folder(library: Path, game: str, name: str) -> Path:
    folder = library / game / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text("{}", encoding="utf-8")
    (folder / "payload.bin").write_bytes(b"orig")
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
            (
                platform,
                app_id,
                title,
                external_id,
                source_url,
                str(folder.resolve()),
                title,
                mid,
            ),
        )
        db._conn.commit()
    return str(mid)


def _nine_pairs(db: DatabaseManager, library: Path) -> list[tuple[str, str, str]]:
    """Return [(invalid_id, original_id, url), ...] length 9."""
    out: list[tuple[str, str, str]] = []
    for i in range(9):
        url = f"https://www.nexusmods.com/stardewvalley/mods/{20000 + i}"
        kind = i % 3
        if kind == 0:
            shared = _folder(library, "SV", f"Shared {i}")
            original = _plant_internal(
                db,
                folder=shared,
                platform=PLATFORM_NEXUS,
                external_id=str(20000 + i),
                source_url=url,
                app_id=413150,
                title=f"Official {i}",
            )
            invalid = _plant_internal(
                db,
                folder=shared,
                platform=PLATFORM_NEXUS,
                external_id=f"stub:tmp{i}",
                source_url=url,
                app_id=0,
                title=f"Stub {i}",
            )
            with db._lock:
                db._conn.execute(
                    "UPDATE mods SET external_id=? WHERE mod_id=?",
                    (f"stub:{invalid}", int(invalid)),
                )
                db._conn.commit()
        elif kind == 1:
            orig_folder = _folder(library, "SV", f"Orig {i}")
            original = _plant_internal(
                db,
                folder=orig_folder,
                platform=PLATFORM_NEXUS,
                external_id=f"stub:pending{i}",
                source_url=url,
                app_id=0,
                title=f"Orig {i}",
            )
            with db._lock:
                db._conn.execute(
                    "UPDATE mods SET external_id=? WHERE mod_id=?",
                    (f"stub:{original}", int(original)),
                )
                db._conn.commit()
            inv_folder = _folder(library, "SV", f"Orig {i}_PLACEHOLDER")
            invalid = _plant_internal(
                db,
                folder=inv_folder,
                platform=PLATFORM_NEXUS,
                external_id=str(20000 + i),
                source_url=url,
                app_id=413150,
                title=f"Dup {i}",
            )
            renamed = inv_folder.parent / f"Orig {i}_{invalid}"
            inv_folder.rename(renamed)
            with db._lock:
                db._conn.execute(
                    "UPDATE mods SET last_known_path=? WHERE mod_id=?",
                    (str(renamed.resolve()), int(invalid)),
                )
                db._conn.commit()
        else:
            orig_folder = _folder(library, "SV", f"Keep {i}")
            original = _plant_internal(
                db,
                folder=orig_folder,
                platform=PLATFORM_NEXUS,
                external_id=f"stub:orig{i}",
                source_url=url,
                app_id=413150,
                title=f"Keep {i}",
            )
            inv_folder = _folder(library, "SV", f"Later {i}")
            invalid = _plant_internal(
                db,
                folder=inv_folder,
                platform=PLATFORM_NEXUS,
                external_id=f"stub:inv{i}",
                source_url=url,
                app_id=413150,
                title=f"Later {i}",
            )
            with db._lock:
                db._conn.execute(
                    "UPDATE mods SET external_id=? WHERE mod_id=?",
                    (f"stub:{original}", int(original)),
                )
                db._conn.execute(
                    "UPDATE mods SET external_id=? WHERE mod_id=?",
                    (f"stub:{invalid}", int(invalid)),
                )
                db._conn.commit()
        out.append((invalid, original, url))
    return out


def test_1_nine_invalid_duplicates_planned_for_removal(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    pairs = _nine_pairs(db, library)
    plan = plan_identity_repair(db, library)
    removes = [c for c in plan.candidates if c.proposed_action == ACTION_REMOVE_INVALID]
    assert len(removes) == 9
    planned = {c.ghost_mod_id for c in removes}
    assert planned == {inv for inv, _orig, _url in pairs}


def test_2_no_identity_conflict_for_confirmed_duplicates(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    _nine_pairs(db, library)
    plan = plan_identity_repair(db, library)
    dups = [c for c in plan.candidates if c.finding_class == "duplicate_source_url"]
    assert dups
    assert all(c.proposed_action != ACTION_CONFLICT for c in dups)
    assert all(c.proposed_action == ACTION_REMOVE_INVALID for c in dups)


def test_3_and_4_and_5_apply_deletes_quarantines_preserves_original(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    pairs = _nine_pairs(db, library)
    before = {
        orig: db._conn.execute(
            "SELECT platform, external_id, workspace_id, source_url, display_name, last_known_path FROM mods WHERE mod_id=?",
            (int(orig),),
        ).fetchone()
        for _inv, orig, _url in pairs
    }
    orig_folders = {
        orig: Path(str(before[orig]["last_known_path"])) for _inv, orig, _url in pairs
    }
    inv_folders = {}
    for invalid, _orig, _url in pairs:
        row = db._conn.execute(
            "SELECT last_known_path FROM mods WHERE mod_id=?",
            (int(invalid),),
        ).fetchone()
        inv_folders[invalid] = Path(str(row["last_known_path"]))
    q = tmp_path / "q" / "run1"
    result = apply_identity_repair(db, library, apply=True, quarantine_root=q)
    assert result.success
    assert result.applied_counts.get("removed_invalid") == 9
    for invalid, original, _url in pairs:
        assert db.get_mod(invalid) is None
        row = db._conn.execute(
            "SELECT platform, external_id, workspace_id, source_url, display_name, last_known_path FROM mods WHERE mod_id=?",
            (int(original),),
        ).fetchone()
        for col in (
            "platform",
            "external_id",
            "workspace_id",
            "source_url",
            "display_name",
            "last_known_path",
        ):
            assert str(row[col] or "") == str(before[original][col] or "")
        assert orig_folders[original].is_dir()
        orig_resolved = orig_folders[original].resolve()
        inv_resolved = inv_folders[invalid].resolve()
        if inv_resolved != orig_resolved:
            assert not inv_resolved.exists()
            dests = list(q.rglob(inv_resolved.name))
            assert dests, f"expected quarantined {inv_resolved.name}"
            assert dests[0].is_dir()
            manifest = dests[0].parent / "MANIFEST.json"
            assert manifest.is_file()
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            assert payload["reason"] == REASON_INVALID_DUPLICATE
            assert payload["action"] == ACTION_REMOVE_INVALID
    with db._lock:
        audits = db._conn.execute(
            "SELECT action, reason FROM identity_repair_audit WHERE action = ?",
            (ACTION_REMOVE_INVALID,),
        ).fetchall()
    assert len(audits) == 9
    assert all(r["reason"] == REASON_INVALID_DUPLICATE for r in audits)
    assert all(r["action"] != ACTION_CONFLICT for r in audits)


def test_6_no_dangling_references(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    pairs = _nine_pairs(db, library)
    invalid, original, _url = pairs[2]
    db.create_deployment_record(413150, "pack", [invalid, original])
    apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert _count_dangling_mod_id(db, invalid) == 0
    with db._lock:
        left = db._conn.execute(
            "SELECT mod_id FROM deployment_record_items"
        ).fetchall()
    mids = {str(r["mod_id"]) for r in left}
    assert invalid not in mids
    assert original in mids


def test_7_rollback_db_failure(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    pairs = _nine_pairs(db, library)
    invalid, original, _url = pairs[1]

    def boom(_conn, _mid):
        raise sqlite3.OperationalError("injected db failure")

    monkeypatch.setattr("services.identity_repair._delete_invalid_mod_row", boom)
    plan = plan_identity_repair(db, library)
    result = apply_identity_repair(
        db, library, plan, apply=True, quarantine_root=tmp_path / "q"
    )
    assert not result.success
    assert db.get_mod(invalid) is not None
    assert db.get_mod(original) is not None


def test_7b_rollback_validation_failure(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    pairs = _nine_pairs(db, library)

    def boom(*_a, **_k):
        raise RuntimeError("injected reference validation failure")

    monkeypatch.setattr("services.identity_repair._validate_remove_invalid", boom)
    result = apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert not result.success
    for invalid, original, _url in pairs:
        assert db.get_mod(invalid) is not None
        assert db.get_mod(original) is not None


def test_7c_filesystem_failure_rolls_back(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    url = "https://www.nexusmods.com/stardewvalley/mods/21001"
    orig_folder = _folder(library, "SV", "Orig FS")
    original = _plant_internal(
        db,
        folder=orig_folder,
        platform=PLATFORM_NEXUS,
        external_id="stub:pending-fs",
        source_url=url,
        app_id=0,
        title="Orig FS",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET external_id=? WHERE mod_id=?",
            (f"stub:{original}", int(original)),
        )
        db._conn.commit()
    inv_folder = _folder(library, "SV", "Orig FS_PLACEHOLDER")
    invalid = _plant_internal(
        db,
        folder=inv_folder,
        platform=PLATFORM_NEXUS,
        external_id="21001",
        source_url=url,
        app_id=413150,
        title="Dup FS",
    )
    renamed = inv_folder.parent / f"Orig FS_{invalid}"
    inv_folder.rename(renamed)
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET last_known_path=? WHERE mod_id=?",
            (str(renamed.resolve()), int(invalid)),
        )
        db._conn.commit()

    def boom(*_a, **_k):
        raise OSError("injected filesystem failure")

    monkeypatch.setattr("services.identity_repair._quarantine_path", boom)
    result = apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert not result.success
    assert db.get_mod(invalid) is not None
    assert db.get_mod(original) is not None
    assert renamed.is_dir()


def test_8_allocation_zero(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    _nine_pairs(db, library)
    calls: list[int] = []
    real = DatabaseManager.allocate_mod_id

    def wrapped(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(DatabaseManager, "allocate_mod_id", wrapped)
    result = apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert result.success
    assert calls == []
    assert result.allocations == 0


def test_9_lifecycle_isolation() -> None:
    src = Path("services/identity_repair.py").read_text(encoding="utf-8")
    assert "reconcile_library(" not in src
    assert "create_mod_identity(" not in src
    assert "db.allocate_mod_id" not in src
    assert "allocate_internal_id(" not in src
    assert inspect.isfunction(reconcile_library)


def test_10_simulated_full_repair_zero_severity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.identity_repair.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    steam = "3591453758"
    live = _folder(library, "Anno 1800", "Collectibles")
    leftover = _folder(library, "Anno 1800", f"Unknown Mod {steam}")
    db.upsert_mod(
        ModMetadata(published_file_id=steam, title="Collectibles", app_id=916440)
    )
    db.update_mod_identity_fields(
        int(steam),
        last_known_path=str(live.resolve()),
        app_id=916440,
        platform=PLATFORM_STEAM,
        external_id=steam,
        source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam}",
    )
    with identity_create_scope(), db._lock:
        ghost = 9000000000003438
        db._ensure_mod_stub(ghost)
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=?, title=?, external_id=?,
                   workspace_id=?, last_known_path=?, folder_present=1
            WHERE mod_id=?
            """,
            (
                PLATFORM_STEAM,
                916440,
                leftover.name,
                f"stub:{ghost}",
                "",
                str(leftover.resolve()),
                ghost,
            ),
        )
        db._conn.commit()
    pollute = _plant_internal(
        db,
        folder=_folder(library, "Anno 1800", "Other"),
        platform="other",
        external_id="",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=9000000000000000",
        app_id=0,
        title="pollute",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET source_url=? WHERE mod_id=?",
            (
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={pollute}",
                int(pollute),
            ),
        )
        db._conn.commit()
    _nine_pairs(db, library)
    result = apply_identity_repair(db, library, apply=True, quarantine_root=tmp_path / "q")
    assert result.success
    after = summarize_sqlite_findings(db)
    assert after.get("steam_internal") == 0
    assert after.get("source_url_internal") == 0
    assert after.get("duplicate_source_url_groups") == 0
    assert after.get("CRITICAL") == 0
    assert after.get("HIGH") == 0
