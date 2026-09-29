"""Unified metadata persistence: skip disk writes when semantic content is unchanged."""

from __future__ import annotations

import inspect
import json
import os
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    MISSING_CONTENT_METADATA_KEY,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
    set_is_missing_content,
)
from services.identity_service import create_mod_identity, identity_create_scope
from services.library_status import CONTENT_CONTENT_MISSING
from services.metadata_backup import backup_root
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from services.mod_identity import LEGACY_ENTITY_KEY
from services.presence_reconcile import reconcile_presence
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242
GAME = "小丑牌"
SYNTHETIC_LIVE_COUNT = 507


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "write_amp.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name=GAME, folder_name=GAME))
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


class _MetadataWriteProbe:
    """Count real Path.write_text calls on metadata.json (not a mock-as-PASS)."""

    def __init__(self) -> None:
        self.live_writes = 0
        self.live_bytes = 0
        self.all_meta_writes = 0
        self.all_meta_bytes = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = Path.write_text
        probe = self

        def wrapped(self_path: Path, data: str, *args: object, **kwargs: object) -> int:
            if Path(self_path).name == METADATA_FILENAME:
                encoding = str(kwargs.get("encoding") or "utf-8")
                nbytes = (
                    len(data)
                    if isinstance(data, (bytes, bytearray))
                    else len(str(data).encode(encoding))
                )
                probe.all_meta_writes += 1
                probe.all_meta_bytes += nbytes
                if Path(self_path).parent.name == INFO_DIR_NAME:
                    probe.live_writes += 1
                    probe.live_bytes += nbytes
            return real(self_path, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", wrapped)


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def _pin_mtime(path: Path) -> int:
    """Stamp a distinctive past mtime so a later real write is observable without sleep()."""
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    return path.stat().st_mtime_ns


def _force_stale_live_missing(db: DatabaseManager, pk: str) -> None:
    """Put a LIVE folder on the Presence stale-missing re-eval path."""
    db.update_mod_content_status(
        pk,
        content_status=CONTENT_CONTENT_MISSING,
        folder_present=True,
    )


def _meta(folder: Path) -> Path:
    return folder / INFO_DIR_NAME / METADATA_FILENAME


def _load(folder: Path) -> dict:
    return json.loads(_meta(folder).read_text(encoding="utf-8"))


def _seed(
    db: DatabaseManager,
    *,
    library: Path,
    folder: str,
    workshop_id: str,
    title: str,
    extra: dict | None = None,
    with_backup: bool = False,
    payload: bool = True,
) -> tuple[Path, str, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=APP_ID,
            game_name=GAME,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    path = library / GAME / folder
    path.mkdir(parents=True)
    if payload:
        (path / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        path,
        internal_id=frozen,
        title=title,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=APP_ID,
        game_name=GAME,
        extra=extra,
    )
    bind_managed_path(db, pk, path, game_name=GAME, title=title)
    db.update_mod_identity_fields(
        pk,
        workspace_id=str(created.workspace_id or workshop_id),
        last_known_path=str(path.resolve()),
        folder_present=True,
    )
    if with_backup:
        sync_after_metadata_change(pk, path, "edit", wait=True)
    return path, pk, frozen


def test_same_metadata_no_write_and_mtime_unchanged(tmp_path: Path) -> None:
    folder = tmp_path / "ModA"
    folder.mkdir()
    payload = {
        "title": "Same",
        "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "workspace_id": "1001",
        "display_name": "Same",
        MISSING_CONTENT_METADATA_KEY: False,
    }
    persist_unified_metadata_dict(folder, payload, sync_backup=False)
    meta = _meta(folder)
    before = meta.read_bytes()
    mtime = _pin_mtime(meta)

    persist_unified_metadata_dict(folder, dict(payload), sync_backup=False)
    persist_unified_metadata_dict(folder, dict(payload), sync_backup=False)

    assert meta.read_bytes() == before
    assert _mtime_ns(meta) == mtime


def test_missing_metadata_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "ModMissing"
    folder.mkdir()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    persist_unified_metadata_dict(
        folder,
        {"title": "New", "internal_id": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"},
        sync_backup=False,
    )
    assert _meta(folder).is_file()
    assert probe.live_writes == 1
    assert probe.live_bytes > 0


def test_corrupted_metadata_is_not_treated_as_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "ModCorrupt"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    meta = info / METADATA_FILENAME
    meta.write_text("{not-json", encoding="utf-8")
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)

    persist_unified_metadata_dict(
        folder,
        {
            "title": "Repaired",
            "internal_id": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            MISSING_CONTENT_METADATA_KEY: False,
        },
        sync_backup=False,
    )
    parsed = _load(folder)
    assert parsed["title"] == "Repaired"
    assert parsed["internal_id"] == "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime


def test_legacy_entity_key_still_migrates_on_disk(tmp_path: Path) -> None:
    folder = tmp_path / "ModLegacy"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    frozen = "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee"
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {"title": "Legacy", LEGACY_ENTITY_KEY: frozen, "workspace_id": "9"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    mtime = _pin_mtime(info / METADATA_FILENAME)
    persist_unified_metadata_dict(
        folder,
        {"title": "Legacy", LEGACY_ENTITY_KEY: frozen, "workspace_id": "9"},
        sync_backup=False,
    )
    data = _load(folder)
    assert data["internal_id"] == frozen
    assert LEGACY_ENTITY_KEY not in data
    assert _mtime_ns(info / METADATA_FILENAME) != mtime


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [
        ("display_name", "Old Name", "New Name"),
        ("cover_path", ".info/cover.jpg", ".info/cover.png"),
        ("workspace_id", "88001", "88002"),
    ],
)
def test_real_field_change_writes(
    tmp_path: Path, field: str, old: str, new: str
) -> None:
    folder = tmp_path / f"Mod_{field}"
    folder.mkdir()
    payload = {
        "title": "T",
        "internal_id": "eeeeeeee-bbbb-cccc-dddd-eeeeeeeeeeee",
        "display_name": "Old Name",
        "cover_path": ".info/cover.jpg",
        "workspace_id": "88001",
        MISSING_CONTENT_METADATA_KEY: False,
    }
    persist_unified_metadata_dict(folder, payload, sync_backup=False)
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    changed = dict(payload)
    changed[field] = new
    persist_unified_metadata_dict(folder, changed, sync_backup=False)
    assert _load(folder)[field] == new
    assert meta.read_bytes() != body
    assert _mtime_ns(meta) != mtime


def test_is_missing_content_false_to_true_writes(tmp_path: Path) -> None:
    folder = tmp_path / "ModFlag"
    folder.mkdir()
    payload = {
        "title": "Flag",
        "internal_id": "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
        MISSING_CONTENT_METADATA_KEY: False,
    }
    persist_unified_metadata_dict(folder, payload, sync_backup=False)
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    set_is_missing_content(folder, True)
    assert _load(folder)[MISSING_CONTENT_METADATA_KEY] is True
    assert _mtime_ns(meta) != mtime


def test_is_missing_content_true_to_false_writes(tmp_path: Path) -> None:
    folder = tmp_path / "ModFlag2"
    folder.mkdir()
    payload = {
        "title": "Flag",
        "internal_id": "11111111-bbbb-cccc-dddd-eeeeeeeeeeee",
        MISSING_CONTENT_METADATA_KEY: True,
    }
    persist_unified_metadata_dict(folder, payload, sync_backup=False)
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    set_is_missing_content(folder, False)
    assert _load(folder)[MISSING_CONTENT_METADATA_KEY] is False
    assert _mtime_ns(meta) != mtime


def test_set_is_missing_content_same_value_does_not_write(tmp_path: Path) -> None:
    folder = tmp_path / "ModFlagSame"
    folder.mkdir()
    payload = {
        "title": "Flag",
        "internal_id": "22222222-bbbb-cccc-dddd-eeeeeeeeeeee",
        MISSING_CONTENT_METADATA_KEY: False,
    }
    persist_unified_metadata_dict(folder, payload, sync_backup=False)
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    set_is_missing_content(folder, False)
    set_is_missing_content(folder, False)
    assert _load(folder)[MISSING_CONTENT_METADATA_KEY] is False
    assert _mtime_ns(meta) == mtime


def test_presence_reconcile_second_pass_does_not_write(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db,
        library=library,
        folder="Alpha",
        workshop_id="88011",
        title="Alpha",
        extra={MISSING_CONTENT_METADATA_KEY: False},
    )
    _force_stale_live_missing(db, pk)
    stats1 = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats1.content_reevaluated >= 1, stats1.as_dict()
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)

    _force_stale_live_missing(db, pk)
    t0 = time.perf_counter()
    stats2 = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert stats2.content_reevaluated >= 1, stats2.as_dict()

    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime
    assert probe.live_writes == 0, (
        f"presence second pass wrote metadata "
        f"writes={probe.live_writes} bytes={probe.live_bytes} elapsed_ms={elapsed_ms:.1f}"
    )
    assert probe.live_bytes == 0


def test_presence_reconcile_false_to_true_writes(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db,
        library=library,
        folder="Emptying",
        workshop_id="88012",
        title="Emptying",
        extra={MISSING_CONTENT_METADATA_KEY: False},
    )
    (folder / "payload.txt").unlink()
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    _force_stale_live_missing(db, pk)
    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_reevaluated >= 1, stats.as_dict()
    assert _load(folder).get(MISSING_CONTENT_METADATA_KEY) is True
    assert _mtime_ns(meta) != mtime


def test_presence_reconcile_true_to_false_writes(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db,
        library=library,
        folder="Restored",
        workshop_id="88013",
        title="Restored",
        extra={MISSING_CONTENT_METADATA_KEY: True},
        payload=True,
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    _force_stale_live_missing(db, pk)
    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_reevaluated >= 1, stats.as_dict()
    assert _load(folder).get(MISSING_CONTENT_METADATA_KEY) is False
    assert _mtime_ns(meta) != mtime


def test_507_synthetic_unchanged_mods_zero_metadata_writes(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folders: list[Path] = []
    pks: list[str] = []
    with identity_create_scope():
        for i in range(SYNTHETIC_LIVE_COUNT):
            folder, pk, _frozen = _seed(
                db,
                library=library,
                folder=f"Live{i:04d}",
                workshop_id=str(910000 + i),
                title=f"Live{i:04d}",
                extra={MISSING_CONTENT_METADATA_KEY: False},
            )
            folders.append(folder)
            pks.append(pk)

    for pk in pks:
        _force_stale_live_missing(db, pk)
    mtimes = {p: _pin_mtime(_meta(p)) for p in folders}
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)

    t0 = time.perf_counter()
    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert stats.live == SYNTHETIC_LIVE_COUNT
    assert stats.content_reevaluated == SYNTHETIC_LIVE_COUNT, stats.as_dict()
    assert probe.live_writes == 0, (
        f"507 unchanged first pass: writes={probe.live_writes} "
        f"bytes={probe.live_bytes} elapsed_ms={elapsed_ms:.1f}"
    )
    assert probe.live_bytes == 0
    for path in folders:
        assert _mtime_ns(_meta(path)) == mtimes[path]

    for pk in pks:
        _force_stale_live_missing(db, pk)
    t1 = time.perf_counter()
    stats2 = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    elapsed2_ms = (time.perf_counter() - t1) * 1000.0
    assert stats2.content_reevaluated == SYNTHETIC_LIVE_COUNT, stats2.as_dict()
    assert probe.live_writes == 0, (
        f"507 unchanged second pass: writes={probe.live_writes} "
        f"bytes={probe.live_bytes} elapsed_ms={elapsed2_ms:.1f}"
    )
    assert probe.live_bytes == 0
    for path in folders:
        assert _mtime_ns(_meta(path)) == mtimes[path]


def test_backup_unchanged_metadata_does_not_churn(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _seed(
        db,
        library=library,
        folder="Backed",
        workshop_id="88014",
        title="Backed",
        extra={MISSING_CONTENT_METADATA_KEY: False, "display_name": "Backed"},
        with_backup=True,
    )
    bak = backup_root(frozen, mod_pk=pk) / METADATA_FILENAME
    assert bak.is_file()
    live_mtime = _pin_mtime(_meta(folder))
    bak_mtime = _pin_mtime(bak)
    bak_body = bak.read_bytes()
    data = read_info_metadata_dict(folder) or {}
    persist_unified_metadata_dict(folder, data, sync_backup=True, sync_reason="edit")
    drain_backup_queue(timeout=5.0)
    assert _mtime_ns(_meta(folder)) == live_mtime
    assert _mtime_ns(bak) == bak_mtime
    assert bak.read_bytes() == bak_body

    live_mtime = _pin_mtime(_meta(folder))
    bak_mtime = _pin_mtime(bak)
    data["display_name"] = "Backed-Changed"
    persist_unified_metadata_dict(
        folder, data, sync_backup=True, sync_reason="cover_change"
    )
    assert _load(folder)["display_name"] == "Backed-Changed"
    assert json.loads(bak.read_text(encoding="utf-8"))["display_name"] == "Backed-Changed"
    assert _mtime_ns(_meta(folder)) != live_mtime
    assert _mtime_ns(bak) != bak_mtime


def test_production_writers_share_one_idempotent_boundary() -> None:
    """Callers must not grow their own skip-if-unchanged copies."""
    import services.file_ops as file_ops

    persist_src = inspect.getsource(file_ops.persist_unified_metadata_dict)
    set_src = inspect.getsource(file_ops.set_is_missing_content)
    commit_src = inspect.getsource(file_ops._commit_unified_metadata)
    write_src = inspect.getsource(file_ops._write_unified_metadata)
    assert "_commit_unified_metadata" in persist_src
    assert "write_text" not in persist_src
    assert "persist_unified_metadata_dict" in set_src
    assert "write_text" not in set_src
    assert "write_text" in commit_src
    assert "_commit_unified_metadata" in write_src
