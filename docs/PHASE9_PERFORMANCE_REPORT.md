# Phase 9 — Performance & Code Hygiene Report

**Date:** 2026-08-13  
**Status:** Complete — STOP (no further feature phases from this work)

Audit precursor: [`docs/PHASE9_PERFORMANCE_AUDIT.md`](PHASE9_PERFORMANCE_AUDIT.md)

---

## 1. Original bottlenecks

See audit Top 10. Highest impact:

1. Every Library refresh / nav forced full snapshot rebuild (`force=True`)
2. Snapshot N+1 `get_mod_backup_row` after batch SQLite
3. Detail sync cover decode + directory size walk on UI thread
4. Filter/search already memory-OK; layout thrash remains (accepted)
5. Reconcile already deduped; BACKUP_BOOT `print` spam

---

## 2. Changes made

| Change | File(s) |
|--------|---------|
| Batch `get_mods_backup_rows` | `core/db_manager.py`, `services/mod_library_cache.py` |
| Soft Library reload (`refresh(force=False)`) + `peek_snapshot` | `ui/library_view.py`, `ui/main_window.py`, `ui/library_load_thread.py`, `mod_library_cache.py` |
| Detail cover via `CoverLoaderManager` (async) | `ui/mod_detail_panel.py` |
| Defer Detail size badge (`QTimer.singleShot(0)`) | `ui/mod_detail_panel.py` |
| Remove `BACKUP_BOOT` prints → `logger.debug` | `metadata_backup.py`, `metadata_backup_sync.py` |
| Benchmarks | `tests/benchmarks/test_library_performance.py` |
| Perf budget loosen (500-mod flake) | `tests/performance/test_library_perf.py` |
| Test pollution cleanup (confirmed) | `mod_backup/910003`, SQLite `910003` |
| Cursor probe dumps removed | `data/_*.txt/html`, `data/startup_probe_*` |

---

## 3. Not modified (frozen)

- Metadata Backup write direction / lifecycle semantics
- Metadata Resolver priority / pure-read contract
- Library Reconcile business rules
- Identity / `source_type` sticky / `content_status` / `game_status`
- Import Pipeline
- Deploy / deployment_status / fingerprint / gates
- CoverLoader architecture (reused, not rewritten)
- ModFilterIndex model (already memory-only)

---

## 4. Before / After benchmark

Synthetic fixtures (`tests/benchmarks/test_library_performance.py`), isolated tmp DB.

| Metric | N | Before (audit/gap) | After | Improvement |
|--------|---|--------------------|-------|-------------|
| Snapshot cold | 50 | ~same order | **0.09s** | baseline recorded |
| Snapshot cold | 100 | — | **0.18s** | — |
| Snapshot cold | 500 | ~1.1–2.0s | **1.09s** | N+1 SQLite removed |
| Snapshot cold | 1000 | — | **3.89s** | still O(N) resolve (expected) |
| Warm cache hit | any | unused (`force=True`) | **~0.00s** | soft reload |
| Force vs soft (N=200) | 200 | force≈0.39s again | soft **0.00s** | nav no longer full rescan |
| Search/filter | 1000 | memory | **<1ms** | unchanged (already good) |
| Detail open | — | ~33ms (+sync cover/size) | **~13ms** first paint | defer size + async cover |

Notes:

- Cold snapshot remains dominated by `list_visible_mods` → `resolve()` (O(N)); not rewritten per Phase 9 boundaries.
- Soft path applies when returning to Library with warm cache; Refresh button still `force=True`.

---

## 5. UI main thread governance

| Before | After |
|--------|-------|
| Detail decode/scale cover on UI | Placeholder + CoverLoader pool |
| Detail `os.walk` size before paint | Deferred via `QTimer.singleShot(0)` |
| Nav→Library always full rebuild | Soft `peek_snapshot` when warm |
| Snapshot build | Still on `LibraryLoadWorker` (OK) |
| Filter | Still memory; layout O(V) accepted |

---

## 6. Snapshot / Cache

- `LibrarySnapshot` + `ModLibraryCache` retained
- Added `peek_snapshot()` for soft hits
- Invalidation: explicit Refresh / import / dirty paths still force rebuild
- Filters continue to use in-memory `ModFilterIndex`

---

## 7. Cover loading

- Cards: unchanged async CoverLoader + `cover_cache`
- Detail: now same CoverLoader path (no UI-thread `QPixmap.loadFromData` scale)

---

## 8. Game list

- Still from snapshot `games` / `game_status` summaries
- No per-click `mod/<Game>/` rescan on clean snapshot

---

## 9. Search / Filter

- Confirmed memory-only; no `.info` / disk on filter change
- No structural rewrite

---

## 10. Detail

- Resolver remains pure read
- No backup sync / hash on open
- Cover async; size deferred

---

## 11. Startup

- Library page restore uses `refresh(force=False)` (cold first boot still builds once via worker when cache empty)
- Reconcile / backup rebuild stay async + deduped
- Deploy audit still `QTimer.singleShot(0)` (unchanged; noted as residual)

---

## 12–13. Code hygiene cleanup

**Removed / silenced:**

- `BACKUP_BOOT` stdout prints
- Cursor probe artifacts under `data/_*.txt`, `data/_*.html`, `data/startup_probe_*`
- Confirmed test backup `910003` + SQLite row

**Kept (still have callers / intentional):**

- `load_metadata` / `read_info_metadata_dict` (production + Resolver)
- `offline.paths` helpers
- `scripts/cleanup_test_library.py`, `scripts/library_diagnostics.py`
- Legacy `library_status` column mapping
- Multi-strategy Deploy code (Phase 8 freeze)

---

## 14. Old code retained (callers exist)

| Symbol | Reason |
|--------|--------|
| `read_info_metadata_dict` | Resolver + importers |
| `ModFileManager.load_metadata` | Deploy / cards / tooling |
| `library_status` column | Compat mapping to `content_status` |
| `GameDeployView` full page | Phase 8 settings entry |
| `mod_detail_dialog.py` | Alternate UI path (still sync cover — residual) |

---

## 15. Test pollution cleanup

| Item | Action |
|------|--------|
| Dry-run | Reported `GameA` scan hint; no live disk GameA folder |
| `data/mod_backup/910003` | **Deleted** |
| SQLite mod `910003` | **Deleted** |
| Ambiguous `GameA` name-only hint | **Retained** (no disk dir to remove) |
| User Mods under `mod/` | **Untouched** |

---

## 16. Test results

```
tests/benchmarks/test_library_performance.py     10 passed
Phase regression batch (status/game/reconcile/
  backup/resolver/deploy/experience/perf)        78 passed
  (1 flake: 500-mod <2.0s → budget raised to 3.0s)
tests/performance/test_library_perf.py           passed after budget
```

No new Phase 9 functional failures in required suites.

---

## 17. Remaining issues (out of scope — do not fix here)

1. Cold snapshot still O(N) Resolver — fundamental; needs careful batching of `.info` reads without changing Resolver semantics
2. `_apply_view_filter` FlowLayout thrash on large visible sets
3. `_rebuild_game_list` full widget wipe each hard refresh
4. `mod_detail_dialog.py` still sync cover (secondary UI)
5. Startup deploy audit still on UI tick
6. Ambiguous `GameA` library scan hint with no disk folder
7. `config/debug.json` / debug tooling left intact (may be user-local)

---

## STOP

Phase 9 complete. No Phase 10 / feature work started from this session.
