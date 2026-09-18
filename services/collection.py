"""Mod Collection service — named per-game Mod sets (browse / organise only).

ARCHITECTURE
------------
Collection is an independent Library work mode. It must not:

* change WH3 Load Order / ``used_mods.txt`` / ``mods.enabled`` / deploy
* reuse ``mod_tags`` / ``game_categories`` / deployment records
* use ``workspace_id`` as membership identity

Identity boundary (Frozen Minimal Model)::

    internal_id (TEXT) = durable business Entity Identity
    mods.mod_id        = SQLite PK / collection_mods.mod_id FK
    workspace_id       = platform identity — never membership

Public membership APIs accept Frozen ``internal_id`` (UUID TEXT).
Digit SQLite PK is accepted only as an already-resolved DAL handle.
Entry conversion is always ``internal_id → dal_mod_pk() → mods.mod_id``.
Never workspace_id / folder name / published_file_id.
``list_collection_member_ids`` returns FK PK values (DB readout, not Frozen identity).

Future Collection Content must call ``ui.library_query.filter_sort_entries``
on the existing Library Mod projection (membership filter only). Do not add
``collection_sort_entries``. Collection list order is ``collections.sort_order``,
which is not Library Sort and not WH3 Load Order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from core.db_manager import CollectionRecord, DatabaseManager, get_db


def _db(db: DatabaseManager | None) -> DatabaseManager:
    return db if db is not None else get_db()


def _member_pks(
    internal_ids: Iterable[int | str],
    *,
    db: DatabaseManager,
) -> list[int]:
    """Frozen ``internal_id`` UUID (or digit PK handle) → ``mods.mod_id`` FKs."""
    from services.mod_library_cache import dal_mod_pk

    _ = db
    pks: list[int] = []
    seen: set[int] = set()
    for raw in internal_ids or ():
        if not str(raw).strip():
            continue
        pk = dal_mod_pk(raw)
        if not pk:
            raise ValueError(f"invalid mod_id: {raw!r}")
        value = int(pk)
        if value in seen:
            continue
        seen.add(value)
        pks.append(value)
    return pks


def list_collections(
    app_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> list[CollectionRecord]:
    return _db(db).list_collections(app_id)


def create_collection(
    app_id: int | str,
    name: str,
    *,
    db: DatabaseManager | None = None,
) -> CollectionRecord:
    return _db(db).create_collection(app_id, name)


def rename_collection(
    collection_id: int | str,
    name: str,
    *,
    db: DatabaseManager | None = None,
) -> CollectionRecord:
    return _db(db).rename_collection(collection_id, name)


def delete_collection(
    collection_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    """Delete Collection + memberships only. Never deletes Mod rows.

    After a successful DB delete, removes only this Collection's Cover files.
    """
    from services.collection_cover import delete_collection_cover_files
    from services.cover_cache import invalidate_cover

    database = _db(db)
    rec = database.get_collection(collection_id)
    old_rel = str(rec.cover_path or "").strip() if rec is not None else ""
    old_files = []
    try:
        from services.collection_cover import own_cover_files, stored_cover_file

        old_files = list(own_cover_files(collection_id))
        stored = stored_cover_file(old_rel) if old_rel else None
        if stored is not None and stored not in old_files:
            old_files.append(stored)
    except Exception:  # noqa: BLE001
        old_files = []
    ok = database.delete_collection(collection_id)
    if not ok:
        return False
    removed = delete_collection_cover_files(collection_id)
    for path in list(old_files) + list(removed):
        try:
            invalidate_cover(path)
        except Exception:  # noqa: BLE001
            pass
    return True


def set_collection_cover(
    collection_id: int | str,
    source: str | Path,
    *,
    db: DatabaseManager | None = None,
) -> CollectionRecord:
    """Copy *source* into Collection Cover storage and persist ``cover_path``.

    File IO happens outside the DB lock. On DB failure the new file is removed
    and the previous Cover is kept.
    """
    from services.collection_cover import (
        commit_staged_cover,
        rollback_staged_cover,
        stage_collection_cover,
        stored_cover_file,
    )
    from services.cover_cache import invalidate_cover

    database = _db(db)
    rec = database.get_collection(collection_id)
    if rec is None:
        raise LookupError(f"collection not found: {collection_id}")
    old_rel = str(rec.cover_path or "").strip()
    old_abs = stored_cover_file(old_rel) if old_rel else None
    staged = stage_collection_cover(
        rec.collection_id, source, current_relative=old_rel
    )
    try:
        updated = database.update_collection_cover_path(
            rec.collection_id, staged.relative_path
        )
    except Exception:
        rollback_staged_cover(staged)
        raise
    final = commit_staged_cover(staged)
    if old_abs is not None:
        invalidate_cover(old_abs)
    invalidate_cover(final)
    return updated


def set_collection_cover_from_member(
    collection_id: int | str,
    internal_id: int | str,
    *,
    db: DatabaseManager | None = None,
):
    """Copy a member Mod's Cover file into Collection storage. Never writes Mod Cover.

    ``internal_id`` is Frozen UUID. Digit PK is accepted as a DAL handle.
    """
    from services.collection_cover import member_cover_file
    from services.mod_library_cache import dal_mod_pk

    database = _db(db)
    members = list_collection_member_ids(collection_id, db=database)
    pk = dal_mod_pk(internal_id)
    if not pk or pk not in {str(x).strip() for x in members}:
        raise ValueError("mod is not a member of this collection")
    src = member_cover_file(pk, db=database)
    if src is None:
        raise ValueError("该 Mod 没有可用封面")
    return set_collection_cover(collection_id, src, db=database)


def reorder_collections(
    app_id: int | str,
    ordered_ids: Sequence[int | str],
    *,
    db: DatabaseManager | None = None,
) -> list[CollectionRecord]:
    """Persist Collection card order. Must not call WH3 load-order helpers."""
    return _db(db).reorder_collections(app_id, ordered_ids)


def get_collection(
    collection_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> CollectionRecord | None:
    return _db(db).get_collection(collection_id)


def list_collection_member_ids(
    collection_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> list[str]:
    """Membership FK values (``str(mods.mod_id)``), not Frozen TEXT identity."""
    return _db(db).list_collection_member_ids(collection_id)


def add_mod_to_collection(
    collection_id: int | str,
    internal_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    """Add one Mod. ``internal_id`` is Frozen UUID (digit PK also accepted)."""
    database = _db(db)
    pks = _member_pks([internal_id], db=database)
    return database.add_mod_to_collection(collection_id, pks[0])


def add_mods_to_collection(
    collection_id: int | str,
    internal_ids: Iterable[int | str],
    *,
    db: DatabaseManager | None = None,
) -> int:
    """One-transaction batch insert. Frozen UUID → ``dal_mod_pk`` → FK PK."""
    database = _db(db)
    pks = _member_pks(internal_ids, db=database)
    if not pks:
        return 0
    return database.add_mods_to_collection(collection_id, pks)


def remove_mod_from_collection(
    collection_id: int | str,
    internal_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    database = _db(db)
    pks = _member_pks([internal_id], db=database)
    return database.remove_mod_from_collection(collection_id, pks[0])


def remove_mods_from_collection(
    collection_id: int | str,
    internal_ids: Iterable[int | str],
    *,
    db: DatabaseManager | None = None,
) -> int:
    database = _db(db)
    pks = _member_pks(internal_ids, db=database)
    if not pks:
        return 0
    return database.remove_mods_from_collection(collection_id, pks)


def move_collection_in_order(
    ordered_ids: Sequence[int | str],
    source_id: int | str,
    target_id: int | str,
) -> list[int]:
    """Reorder helper for Collection-card drop. Independent of WH3 ``move_in_order``."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in ordered_ids:
        try:
            cid = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if cid <= 0 or cid in seen:
            continue
        seen.add(cid)
        ids.append(cid)
    try:
        source = int(str(source_id).strip())
        target = int(str(target_id).strip())
    except (TypeError, ValueError):
        return ids
    if source not in seen or target not in seen or source == target:
        return ids
    next_order = [cid for cid in ids if cid != source]
    idx = next_order.index(target)
    next_order.insert(idx, source)
    return next_order


def list_collection_ids_for_mods(
    internal_ids: Iterable[int | str],
    *,
    db: DatabaseManager | None = None,
) -> dict[str, set[int]]:
    """Frozen UUID (or digit PK) → Collection ids. Keys are ``str(mods.mod_id)``."""
    database = _db(db)
    pks = _member_pks(internal_ids, db=database)
    return database.list_collection_ids_for_mods(pks)


def membership_check_states(
    app_id: int | str,
    internal_ids: Iterable[int | str],
    *,
    db: DatabaseManager | None = None,
) -> list[tuple[CollectionRecord, str]]:
    """Per-collection checklist state for a Mod selection: ``all`` / ``none`` / ``mixed``.

    ``internal_ids`` are Frozen UUIDs (digit PK also accepted).
    """
    database = _db(db)
    ids = [str(pk) for pk in _member_pks(internal_ids, db=database)]
    collections = list_collections(app_id, db=database)
    mapping = database.list_collection_ids_for_mods(ids)
    n = len(ids)
    out: list[tuple[CollectionRecord, str]] = []
    for rec in collections:
        count = sum(
            1 for mid in ids if int(rec.collection_id) in mapping.get(mid, set())
        )
        if n == 0 or count == 0:
            state = "none"
        elif count == n:
            state = "all"
        else:
            state = "mixed"
        out.append((rec, state))
    return out


def compute_membership_edits(
    initial: dict[int, str],
    final: dict[int, str],
) -> tuple[list[int], list[int]]:
    """Diff checklist states into add / remove Collection ids. Mixed-unchanged is a no-op."""
    add_ids: list[int] = []
    remove_ids: list[int] = []
    for cid, want in final.items():
        try:
            key = int(cid)
        except (TypeError, ValueError):
            continue
        was = str(initial.get(key) or initial.get(cid) or "none")
        want_s = str(want or "none")
        if want_s == "all" and was != "all":
            add_ids.append(key)
        elif want_s == "none" and was != "none":
            remove_ids.append(key)
    return add_ids, remove_ids


def apply_collection_memberships(
    app_id: int | str,
    internal_ids: Iterable[int | str],
    add_collection_ids: Iterable[int | str],
    remove_collection_ids: Iterable[int | str],
    *,
    db: DatabaseManager | None = None,
) -> tuple[int, int]:
    """One-transaction batch membership apply. Does not write Mod rows.

    ``internal_ids`` are Frozen UUIDs (digit PK also accepted).
    """
    database = _db(db)
    pks = _member_pks(internal_ids, db=database)
    return database.apply_collection_memberships(
        app_id, pks, add_collection_ids, remove_collection_ids
    )


