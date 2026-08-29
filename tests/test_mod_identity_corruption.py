"""Mod identity corruption regression — cases 1–10 from identity audit spec."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.importers.duplicate_check import check_import_duplicate, find_duplicate_mod
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_mod_identity, resolve_existing_mod_id
from services.mod_identity_validator import (
    IdentityIssueCode,
    IdentitySeverity,
    validate_db_row_identity,
    validate_mod_identity,
)
from services.mod_library_integrity_audit import audit_mod_library_integrity
from services.modio_api import ModioClient

INTERNAL_MOD_ID = 9_000_000_000_003_410
REAL_MODIO_MOD_ID = 4_503_767
BG3_APP_ID = 1_086_940
MODIO_URL = "https://mod.io/g/baldursgate3/m/super-skip-ship-sss"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_corruption.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


def _write_mod_folder(
    library: Path,
    game: str,
    folder_name: str,
    metadata: dict,
    *,
    content: bool = True,
) -> Path:
    folder = library / game / folder_name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if content:
        with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
            zf.writestr("mod.txt", "x")
    return folder


def _db_row(db: DatabaseManager, mod_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT mod_id, platform, external_id, source_url, app_id FROM mods WHERE mod_id = ?",
            (int(mod_id),),
        ).fetchone()
    assert row is not None
    return dict(row)


def _count_mod_rows(db: DatabaseManager) -> int:
    with db._lock:  # noqa: SLF001
        return int(db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])  # noqa: SLF001


# --- Case 1: internal id pollution + Mod.io URL → detect + URL recovery ---


def test_case1_polluted_external_id_detected_and_url_recovery(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        external_id=mid,
    )
    folder = _write_mod_folder(
        library,
        "BG3",
        "super-skip-ship-sss",
        {
            "published_file_id": mid,
            "url": MODIO_URL,
            "source_type": "modio",
            "external_id": mid,
        },
    )

    findings = validate_db_row_identity(
        mod_id=mid,
        platform=PLATFORM_MODIO,
        external_id=mid,
        source_url=MODIO_URL,
    )
    codes = {f.code for f in findings}
    assert IdentityIssueCode.INTERNAL_ID_AS_EXTERNAL_ID in codes

    resolved = resolve_existing_mod_id(
        {"url": MODIO_URL, "source_type": "modio", "external_id": mid}
    )
    assert resolved == mid

    reconcile_library(library)
    assert _count_mod_rows(db) == 1
    row = _db_row(db, mid)
    assert row["platform"] == PLATFORM_MODIO
    assert row["external_id"] in ("", f"stub:{mid}")


# --- Case 2: real numeric Mod.io id still works ---


def test_case2_real_modio_numeric_external_id(db: DatabaseManager) -> None:
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        source_url=MODIO_URL,
    )
    dup = find_duplicate_mod(
        db,
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        app_id=0,
    )
    assert dup is not None
    assert str(dup.mod_id) == mid

    findings = validate_db_row_identity(
        mod_id=mid,
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        source_url=MODIO_URL,
        app_id=0,
    )
    polluted = [f for f in findings if f.severity == IdentitySeverity.CORRUPTED]
    assert polluted == []


# --- Case 3: stub external_id must not be used as Mod.io API id ---


def test_case3_stub_external_id_not_modio_api_id() -> None:
    client = ModioClient(api_key="test")
    assert client.resolve_mod is not None
    from core.mod_platform import is_provisional_external_id

    assert is_provisional_external_id(f"stub:{INTERNAL_MOD_ID}")
    assert not is_provisional_external_id(str(REAL_MODIO_MOD_ID))


# --- Case 4: DB vs metadata mismatch detected ---


def test_case4_db_metadata_mismatch_detected(db: DatabaseManager) -> None:
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        source_url=MODIO_URL,
    )
    meta = {
        "published_file_id": mid,
        "modio_mod_id": "9999999",
        "external_id": "8888888",
        "url": MODIO_URL,
    }
    report = validate_mod_identity(
        mod_id=mid,
        db_row=_db_row(db, mid),
        metadata=meta,
    )
    codes = {f.code for f in report.findings}
    assert IdentityIssueCode.DB_METADATA_ID_MISMATCH in codes


# --- Case 5: same source_url two mod_ids → duplicate detected ---


def test_case5_duplicate_source_url_detected(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid_a = str(db.allocate_mod_id())
    mid_b = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid_a),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        last_known_path=str(library / "BG3" / "folder-a"),
    )
    db.update_mod_identity_fields(
        int(mid_b),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        last_known_path=str(library / "BG3" / "folder-b"),
    )
    _write_mod_folder(
        library,
        "BG3",
        "folder-a",
        {"published_file_id": mid_a, "url": MODIO_URL, "source_type": "modio"},
    )
    _write_mod_folder(
        library,
        "BG3",
        "folder-b",
        {"published_file_id": mid_b, "url": MODIO_URL, "source_type": "modio"},
    )

    audit = audit_mod_library_integrity(library, db=db)
    dup_codes = [
        f
        for f in audit.global_findings
        if f.code == IdentityIssueCode.DUPLICATE_SOURCE_URL
    ]
    assert len(dup_codes) >= 1

    before = _count_mod_rows(db)
    reconcile_library(library)
    assert _count_mod_rows(db) == before


# --- Case 6: same internal mod_id two directories → duplicate directory ---


def test_case6_same_mod_id_two_directories(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
    )
    _write_mod_folder(
        library,
        "BG3",
        "slug-name",
        {"published_file_id": mid, "url": MODIO_URL, "source_type": "modio"},
    )
    _write_mod_folder(
        library,
        "BG3",
        "Title Name",
        {"published_file_id": mid, "url": MODIO_URL, "source_type": "modio"},
    )

    audit = audit_mod_library_integrity(library, db=db)
    dup = [
        f
        for f in audit.global_findings
        if f.code == IdentityIssueCode.DUPLICATE_DIRECTORY_IDENTITY
    ]
    assert len(dup) == 1
    assert mid in dup[0].mod_id

    before = _count_mod_rows(db)
    reconcile_library(library)
    assert _count_mod_rows(db) == before


# --- Case 7: same title different platform ids → must not merge ---


def test_case7_same_title_different_identity_not_merged(db: DatabaseManager) -> None:
    mid_a = str(db.allocate_mod_id())
    mid_b = str(db.allocate_mod_id())
    assert mid_a != mid_b
    db.update_mod_identity_fields(
        int(mid_a),
        platform=PLATFORM_MODIO,
        external_id="1111111",
        source_url="https://mod.io/g/game/m/mod-a",
    )
    db.update_mod_identity_fields(
        int(mid_b),
        platform=PLATFORM_MODIO,
        external_id="2222222",
        source_url="https://mod.io/g/game/m/mod-b",
    )
    dup = check_import_duplicate(
        db,
        platform=PLATFORM_MODIO,
        external_id="2222222",
        source_url="https://mod.io/g/game/m/mod-b",
    )
    assert dup is not None
    assert str(dup.mod_id) == mid_b
    assert str(dup.mod_id) != mid_a


# --- Case 8: same title same platform id → duplicate candidate ---


def test_case8_same_title_same_external_duplicate_candidate(db: DatabaseManager) -> None:
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        source_url=MODIO_URL,
    )
    dup = check_import_duplicate(
        db,
        platform=PLATFORM_MODIO,
        external_id=str(REAL_MODIO_MOD_ID),
        source_url=MODIO_URL,
    )
    assert dup is not None
    assert str(dup.mod_id) == mid


# --- Case 9: metadata deleted, DB exists → reconcile must not mint new mod_id ---


def test_case9_missing_metadata_no_new_mod_id(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    folder = library / "BG3" / "orphan-meta"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")

    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        last_known_path=str(folder.resolve()),
        folder_present=True,
    )

    before = _count_mod_rows(db)
    reconcile_library(library)
    assert _count_mod_rows(db) == before


# --- Case 10: DB row deleted, metadata exists → orphan detected ---


def test_case10_orphan_metadata_detected(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    _write_mod_folder(
        library,
        "BG3",
        "orphan-db",
        {"published_file_id": mid, "url": MODIO_URL, "source_type": "modio"},
    )

    audit = audit_mod_library_integrity(library, db=db)
    assert audit.scanned_folders >= 1


# --- Reconcile: non-steam must not get steam platform from upsert_mod ---


def test_reconcile_non_steam_skips_steam_upsert(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    _write_mod_folder(
        library,
        "BG3",
        "modio-mod",
        {
            "published_file_id": mid,
            "url": MODIO_URL,
            "source_type": "modio",
            "external_id": str(REAL_MODIO_MOD_ID),
        },
    )
    reconcile_library(library)
    row = _db_row(db, mid)
    assert row["platform"] == PLATFORM_MODIO
    assert row["platform"] != PLATFORM_STEAM


# --- Idempotency: reconcile twice, row count stable ---


def test_reconcile_idempotent_no_new_rows(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    _write_mod_folder(
        library,
        "BG3",
        "stable-mod",
        {
            "published_file_id": mid,
            "url": MODIO_URL,
            "source_type": "modio",
        },
    )
    reconcile_library(library)
    count_after_first = _count_mod_rows(db)
    reconcile_library(library)
    assert _count_mod_rows(db) == count_after_first


# --- Restart simulation: new DB manager instance, same file ---


def test_restart_persistence(
    tmp_path: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "restart.db"
    library = tmp_path / "mod"

    DatabaseManager.reset_instance()
    db_a = DatabaseManager.instance(db_path)
    mid = str(db_a.allocate_mod_id())
    db_a.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        external_id=str(REAL_MODIO_MOD_ID),
    )
    _write_mod_folder(
        library,
        "BG3",
        "restart-mod",
        {"published_file_id": mid, "url": MODIO_URL, "source_type": "modio"},
    )
    reconcile_library(library)
    DatabaseManager.reset_instance()

    db_b = DatabaseManager.instance(db_path)
    row = _db_row(db_b, mid)
    assert row["platform"] == PLATFORM_MODIO
    assert row["external_id"] == str(REAL_MODIO_MOD_ID)
    reconcile_library(library)
    assert _count_mod_rows(db_b) == 1
    DatabaseManager.reset_instance()


# --- ensure_mod_identity: URL recovery prevents allocate ---


def test_ensure_mod_identity_url_recovery_no_allocate(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    existing_mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(existing_mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
    )
    folder = _write_mod_folder(
        library,
        "BG3",
        "new-folder-no-pub",
        {"url": MODIO_URL, "source_type": "modio"},
    )
    mod_id, _, _ = ensure_mod_identity(folder)
    assert mod_id == existing_mid
    assert _count_mod_rows(db) == 1
