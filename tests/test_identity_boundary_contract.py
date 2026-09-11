"""Identity boundary contract — prevent third-ID / multi-identity regressions."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.metadata_backup import backup_root, restore_info_sidecar_from_backup
from services.mod_identity import ensure_mod_identity, resolve_existing_mod_id

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "IDENTITY_LIFECYCLE_CONTRACT.md"
STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "boundary.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    yield manager
    DatabaseManager.reset_instance()


def _write_info(folder: Path, payload: dict) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.bin").write_bytes(b"x")
    return folder


def _call_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _strip(source: str) -> str:
    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r"#.*?$", "", stripped, flags=re.M)
    return stripped


def test_1_internal_id_is_sole_entity_key(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="9001",
        source_url="https://www.nexusmods.com/stardewvalley/mods/9001",
        title="Entity",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    col_iid = str(row.get("internal_id") or "").strip()
    # Entity key: internal_id column when set, else historical PK (still entity, not workspace)
    entity_key = col_iid or mid
    if col_iid:
        assert db.find_mod_by_internal_id(col_iid) == mid
    assert db.get_mod(mid) is not None
    # workspace alone never resolves entity
    assert db.find_mod_by_workspace_id("9001") is None
    assert resolve_existing_mod_id({"workspace_id": "9001", "app_id": STARDEW}, db=db) == ""
    assert resolve_existing_mod_id({"internal_id": entity_key}, db=db) == mid


def test_2_workspace_id_may_repeat_across_app_id(db: DatabaseManager) -> None:
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert a.mod_id != b.mod_id
    assert a.workspace_id == b.workspace_id == "1333"
    assert db.find_mod_for_registration(PLATFORM_NEXUS, BG3, "1333").mod_id == a.mod_id
    assert db.find_mod_for_registration(PLATFORM_NEXUS, STARDEW, "1333").mod_id == b.mod_id


def test_3_same_app_workspace_duplicate_refuses_second_entity(db: DatabaseManager) -> None:
    first = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="5555",
        source_url="https://www.nexusmods.com/stardewvalley/mods/5555",
        title="Once",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    second = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="5555",
        source_url="https://www.nexusmods.com/stardewvalley/mods/5555",
        title="Twice",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    # Registration rematch — refuse minting a second entity
    assert str(first.mod_id) == str(second.mod_id)
    count = db._conn.execute(
        """
        SELECT COUNT(*) FROM mods
        WHERE platform = ? AND app_id = ? AND TRIM(workspace_id) = ?
        """,
        (PLATFORM_NEXUS, STARDEW, "5555"),
    ).fetchone()[0]
    assert int(count) == 1


def test_4_external_id_not_in_entity_identity_lookup() -> None:
    src = (ROOT / "services" / "mod_identity.py").read_text(encoding="utf-8")
    part = src.split("def resolve_existing_mod_id", 1)[1].split("def ensure_mod_identity", 1)[0]
    body = _strip(part)
    assert "find_mod_by_external" not in body
    assert "external_id" not in body or "Forbidden" in part
    # Production resolve body must not query by external_id
    assert "find_mod_by_external(" not in body
    assert "WHERE" not in body.upper() or "external" not in body.lower()


def test_5_app_id_only_for_registration_scope(db: DatabaseManager) -> None:
    assert db.find_mod_for_registration(PLATFORM_NEXUS, 0, "1333") is None
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/7777",
        title="Scope",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    hit = db.find_mod_for_registration(PLATFORM_NEXUS, STARDEW, "7777")
    assert hit is not None and str(hit.mod_id) == str(created.mod_id)
    # Wrong game scope must not hit
    assert db.find_mod_for_registration(PLATFORM_NEXUS, BG3, "7777") is None
    # app_id alone is not an entity API
    src = (ROOT / "core" / "db_manager.py").read_text(encoding="utf-8")
    assert "def find_mod_by_app_id" not in src
    assert "def get_mod_by_app_id" not in src


def test_6_reconcile_binds_only_via_info_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="4242",
        source_url="https://www.nexusmods.com/stardewvalley/mods/4242",
        title="Bind",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    iid = str(row.get("internal_id") or mid)
    library = tmp_path / "mod"
    # Workspace-only forged proof must not bind
    forged = _write_info(
        library / "Stardew" / "Forged",
        {
            "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "workspace_id": "4242",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Forged",
        },
    )
    reconcile_library(library)
    row_after = db.get_mod_backup_row(mid) or {}
    assert Path(str(row_after.get("last_known_path") or "")).resolve() != forged.resolve()
    # True internal_id binds
    real = _write_info(
        library / "Stardew" / "Real",
        {
            "internal_id": iid,
            "workspace_id": "4242",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Bind",
        },
    )
    bound, _, _ = ensure_mod_identity(real, db=db)
    assert bound == mid
    src = (ROOT / "services" / "library_reconcile.py").read_text(encoding="utf-8")
    assert "create_mod_identity" not in _call_names(src)


def test_7_backup_cannot_restore_entity_via_external_workspace_app(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)
    orphan_mid = "9000000000888881"
    bak = backup_root(orphan_mid)
    bak.mkdir(parents=True)
    (bak / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "workspace_id": "1333",
                "external_id": "1333",
                "app_id": STARDEW,
                "title": "No",
            }
        ),
        encoding="utf-8",
    )
    folder = tmp_path / "mod" / "Stardew" / "NoEntity"
    folder.mkdir(parents=True)
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert restore_info_sidecar_from_backup(orphan_mid, folder, db=db) is False
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before
    src = (ROOT / "services" / "metadata_backup.py").read_text(encoding="utf-8")
    body = _strip(src)
    assert "create_mod_identity(" not in body
    assert "INSERT INTO mods" not in body


def test_text_internal_id_is_business_identity_distinct_from_pk(
    db: DatabaseManager,
) -> None:
    """TEXT ``mods.internal_id`` is Frozen entity identity; FK columns stay PK."""
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="9002",
        source_url="https://www.nexusmods.com/stardewvalley/mods/9002",
        title="ProofAlias",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    proof = str((db.get_mod_backup_row(mid) or {}).get("internal_id") or "")
    assert proof
    assert proof != mid
    assert db.find_mod_by_internal_id(proof) == mid
    assert db.get_mod_display_info(mid) is not None
    rec = db.create_collection(STARDEW, "Proof")
    with pytest.raises(ValueError, match="invalid mod_id"):
        db.add_mods_to_collection(rec.collection_id, [proof])


def test_contract_documents_id_boundary() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for token in (
        "ID Boundary",
        "internal_id",
        "workspace_id",
        "app_id",
        "external_id",
        "Legacy metadata",
        "platform + app_id + workspace_id",
        "Game scope",
    ):
        assert token in text, f"missing contract token {token!r}"
