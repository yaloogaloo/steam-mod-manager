"""Content Missing lifecycle — Validator → DB → Projection (not UI patches).

Lifecycle contract
------------------
Mod Entity → content_status_eval (authority) → DB content_status
  → ModListItem / ModCardData projection → UI badge.

Library load only reads projection (no payload re-scan).
Refresh / Sync / Import / Reconcile may *invoke* the authority API.
Conflict (user annotation) must never drive content_status.
"""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY, row_content_status
from services.mod_library_cache import apply_content_status_to_card_data, build_library_snapshot, get_library_cache, list_item_to_card_data, reset_library_cache
from services.mod_list_item import ModListItem
from services.mod_refresh import reconcile_local_state
from tests.helpers.identity import bind_managed_path, create_steam_test_mod
pytest.importorskip('PySide6')

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'content_missing_life.db')
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()

def _seed(library: Path, db: DatabaseManager, workshop: str, *, with_payload: bool, game: str='LifeGame', app_id: int=424201) -> tuple[Path, str]:
    """Create Steam entity; return (folder, mods.mod_id PK). workshop ≠ PK."""
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    created = create_steam_test_mod(
        db, external_id=workshop, title=f'Mod{workshop}', app_id=app_id, game_name=game
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or pk)
    folder = library / game / f'Mod{workshop}'
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                'internal_id': frozen,
                'published_file_id': workshop,
                'title': f'Mod{workshop}',
                'app_id': app_id,
                'game_name': game,
            }
        ),
        encoding='utf-8',
    )
    if with_payload:
        (folder / 'mod.pak').write_bytes(b'payload')
    bind_managed_path(db, pk, folder, game_name=game, title=f'Mod{workshop}')
    db.update_mod_content_status(pk, content_status=CONTENT_HEALTHY)
    return folder, pk

def test_empty_payload_persists_content_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770001', with_payload=False)
    cs = persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    assert cs == CONTENT_CONTENT_MISSING
    row = db.get_mod_backup_row(mid) or {}
    assert row_content_status(row) == CONTENT_CONTENT_MISSING

def test_healthy_payload_not_false_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770002', with_payload=True)
    cs = persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    assert cs == CONTENT_HEALTHY
    row = db.get_mod_backup_row(mid) or {}
    assert row_content_status(row) == CONTENT_HEALTHY

def test_refresh_keeps_missing_status(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770003', with_payload=False)
    persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    local = reconcile_local_state(mid, folder, db=db)
    assert local.content_status == CONTENT_CONTENT_MISSING
    assert row_content_status(db.get_mod_backup_row(mid) or {}) == CONTENT_CONTENT_MISSING

def test_reconcile_evaluates_missing_via_authority(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770004', with_payload=False)
    db.update_mod_content_status(mid, content_status=CONTENT_HEALTHY)
    reconcile_library(library)
    assert row_content_status(db.get_mod_backup_row(mid) or {}) == CONTENT_CONTENT_MISSING

def test_reconcile_does_not_false_mark_healthy_payload(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770005', with_payload=True)
    db.update_mod_content_status(mid, content_status=CONTENT_CONTENT_MISSING)
    reconcile_local_state(mid, folder, db=db)
    reconcile_library(library)
    assert row_content_status(db.get_mod_backup_row(mid) or {}) == CONTENT_HEALTHY

def test_library_snapshot_reads_projection_not_rescan(db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770006', with_payload=False)
    persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    calls: list[str] = []

    def _boom(*_a, **_k):
        calls.append('scanned')
        raise AssertionError('Library snapshot must not rescan payload')
    monkeypatch.setattr('services.file_ops.read_is_missing_content', _boom)
    monkeypatch.setattr('services.local_file_index.has_local_mod_payload', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('no payload scan')))
    reset_library_cache()
    snap = build_library_snapshot(library)
    assert calls == []
    card = next((c for c in snap.cards if c.id == mid))
    assert card.content_status == CONTENT_CONTENT_MISSING
    assert card.missing_content is True

def test_projection_patch_survives_stale_card_data(db: DatabaseManager, tmp_path: Path) -> None:
    """Viewport must not re-apply healthy ModCardData after Validator write."""
    library = tmp_path / 'mod'
    folder, mid = _seed(library, db, '770007', with_payload=False)
    reset_library_cache()
    cache = get_library_cache()
    snap = cache.load_snapshot(library, force=True)
    stale = next((c for c in snap.cards if c.id == mid))
    assert stale.content_status in ('', CONTENT_HEALTHY)
    persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    patched = cache.refresh_projection(mid)
    assert patched is not None
    assert patched.content_status == CONTENT_CONTENT_MISSING
    assert patched.missing_content is True
    still_stale = apply_content_status_to_card_data(stale, content_status=CONTENT_CONTENT_MISSING, folder_absent=False)
    assert still_stale.content_status == CONTENT_CONTENT_MISSING

def test_game_switch_projection_keeps_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    folder_a, mid_a = _seed(library, db, '770008', with_payload=False, game='GameA', app_id=101)
    _folder_b, mid_b = _seed(library, db, '770009', with_payload=True, game='GameB', app_id=102)
    persist_evaluated_content_status(mid_a, folder_a, db=db, folder_present=True)
    reset_library_cache()
    snap = build_library_snapshot(library)
    a = next((c for c in snap.cards if c.id == mid_a))
    b = next((c for c in snap.cards if c.id == mid_b))
    assert a.content_status == CONTENT_CONTENT_MISSING
    assert b.content_status == CONTENT_HEALTHY
    filtered = [c for c in snap.cards if c.game_folder == 'GameA']
    assert len(filtered) == 1
    assert filtered[0].content_status == CONTENT_CONTENT_MISSING

def test_list_item_carries_content_status_to_card() -> None:
    item = ModListItem(internal_id='1', workspace_id='1', game_id=1, game_folder='G', name='N', content_status=CONTENT_CONTENT_MISSING)
    card = list_item_to_card_data(item)
    assert card.content_status == CONTENT_CONTENT_MISSING
    assert card.missing_content is True
