# Phase 10 — Test Pollution Report (Read-Only Audit)

Generated: 2026-08-13

## Executive summary

Production `data/mod_manager.db` contains **11 SQLite rows** whose `last_known_path` points at `pytest-of-*` temp directories. These were created during a **pre-isolation full pytest run** (sessions `pytest-1136` … `pytest-1151`) before `tests/conftest.py` autouse isolation was in place.

**Disk `mod/` is clean** — no `GameA` / `GameB` / `GameX` / `TestGame` folders on disk. Test-like names in `scan_library_issues` refer to **SQLite-only** stale game references, not live library folders.

## Process audit (2026-08-13)

| Check | Result |
|-------|--------|
| Python processes | 1 (`main.py` GUI only) |
| pytest processes | 0 |
| Stuck test workers | None observed |
| SQLite lock on production DB | None (read-only queries succeed) |

## Isolation mechanism (post-fix)

| Component | Status |
|-----------|--------|
| `tests/conftest.py` autouse | Redirects `data_dir`, `default_mod_library`, `database_path`, `DatabaseManager` to `tmp_path` |
| `DatabaseManager.instance(db_path)` | Reopens singleton when path differs (test safety) |
| `pytest.ini` / `pyproject.toml` | Not present (pytest defaults) |
| Production `mod/` / `data/` in tests | Blocked by conftest when pytest loads `tests/conftest.py` |

**Root cause of historical pollution:** Tests that trigger `sync_metadata_backup` → `get_db().update_mod_backup_snapshot()` ran against the **production singleton** when no autouse isolation existed.

## Pollution inventory

### A — Clear test pollution (safe to delete)

| Type | ID / path | Source test (from path) | Delete |
|------|-----------|-------------------------|--------|
| SQLite + backup | `1003` | `test_library_search_and_filter` | Yes |
| SQLite + backup | `7003` | `test_select_all_mods_shortcut` | Yes |
| SQLite + backup | `9021` | `test_library_scroll_preserved_*` | Yes |
| SQLite + backup | `9023` | `test_library_scroll_preserved_*` | Yes |
| SQLite + backup | `82002` | `test_game_switch_reuses_cached` | Yes |
| SQLite + backup | `83002` | `test_filter_does_not_create_ca*` | Yes |
| SQLite + backup | `83003` | `test_filter_does_not_create_ca*` | Yes |
| SQLite + backup | `93004` | `test_render_cards_always_have_*` | Yes |
| SQLite + backup | `94000` | `test_refresh_does_not_spawn_to*` | Yes |
| SQLite + backup | `1623730013` | `test_scroll_range_shrinks_when*` | Yes |
| SQLite + backup | `1623730014` | `test_scroll_range_shrinks_when*` | Yes |
| Backup dirs | `data/mod_backup/{ids above}` | backup sync side effect | Yes |

Cleanup tool: `python scripts/cleanup_test_library.py --apply --yes`

### B — Production data (do not delete)

| Item | Notes |
|------|-------|
| `mod/Anno 1800`, `mod/Palworld`, … | Real game folders (6 games on disk) |
| ~1046+ mods with paths under `E:\project\steam-mod-manager\mod\` | Real library |
| `data/mod_backup/*` matching real Workshop IDs | Production backups |

### C — Ambiguous (report only, do not auto-delete)

| ID | Title | last_known_path | Notes |
|----|-------|-----------------|-------|
| `8001` | (empty) | empty | orphan stub, origin unknown |
| `93002` | (empty) | empty | orphan stub |
| `97002` | (empty) | empty | orphan stub |
| `555001` | (empty) | empty | orphan stub |
| `9000000000000344` | 仓库扩容插件 | empty | may be import fixture residue |
| `9000000000000375` | Item Buildings… | empty | may be import fixture residue |
| `9000000000000434` | Bigger Harbour | empty | may be import fixture residue |

Also: `scan_library_issues` reports orphan games `app_id=1142710`, `app_id=281990` — **Type C**, no disk folder.

## Qt / worker hang risk (read-only)

Module-scoped `qapp` fixtures exist in many UI tests. Phase 9/9.1 workers (`CoverLoaderManager`, directory size, reconcile pending) have autouse reset in perf/cover tests. **Full suite hang at ~30%** is likely a combination of:

1. Many module-scoped Qt apps + slow UI tests
2. Historical production DB writes causing lock contention
3. Cursor Agent **re-launching pytest** while a prior run was still active

**Mitigation for Phase 10:** Never parallel pytest; run layered subsets; autouse isolation prevents new DB pollution.

## Recommendations applied in Phase 10

1. Keep `tests/conftest.py` autouse isolation (extended to patch `core.db_manager.database_path`)
2. Run `scripts/cleanup_test_library.py --apply` for Type A only
3. Do **not** run full `pytest tests` until Levels 2–5 pass individually
4. Type C rows — manual review by user if needed
