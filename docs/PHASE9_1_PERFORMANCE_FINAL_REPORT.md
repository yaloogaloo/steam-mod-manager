# Phase 9.1 — Library Performance Finalization Report

**Date:** 2026-08-13  
**Status:** Complete — **STOP**

References: `docs/PHASE9_PERFORMANCE_AUDIT.md`, `docs/PHASE9_PERFORMANCE_REPORT.md`

---

## 1. Modified files

| File | Change |
|------|--------|
| `services/mod_library_cache.py` | `peek`/`load` root key via `resolve()`; confirmed batch `get_mods_backup_rows` |
| `services/library_reconcile.py` | Concurrent reconcile coalesced (`_reconcile_pending_root` → one follow-up) |
| `ui/library_view.py` | Search debounce 150ms; filter signature skip; game sidebar fingerprint reuse |
| `ui/mod_detail_panel.py` | Directory size on `QThreadPool` + token guard (not UI-sync walk) |
| `ui/mod_detail_dialog.py` | Cover via `CoverLoaderManager` (no sync `QPixmap(path)`) |
| `ui/main_window.py` | Deploy audit delayed to 750ms after show |
| `tests/test_library_performance.py` | **New** Cases 1–9 |

Unchanged (frozen): Resolver, Backup write rules, Identity, Deploy, Import, content_status semantics.

---

## 2. Call chains (after)

### Library refresh
- Soft / nav: `refresh(force=False)` → `peek_snapshot` → apply UI (no rebuild when warm)
- Explicit Refresh: `force=True` → async worker rebuild + reconcile (coalesced)

### Game switch
- `_render_mod_cards(force_reload=_snapshot_dirty)` → filter `snapshot.cards` in memory
- Sidebar: fingerprint match → reuse widgets (no `clear`)

### Filter / Search
- Memory `ModFilterIndex` only
- Search: debounce 150ms; identical filter signature → skip layout

### Detail
- Resolve (pure read) → paint basics → async cover → async size

### Startup
- UI first → soft library load → reconcile async → deploy audit @750ms

### Reconcile
- If running: queue latest root; run once after current finishes (no parallel scans)

---

## 3. Benchmark (synthetic tmp fixtures)

From `tests/test_library_performance.py` Case 9 + prior Phase 9 bench:

| N | Cold snapshot | Warm / soft |
|---|---------------|-------------|
| 100 | &lt; 1.5s budget (typ. ~0.2s) | ~0.00s |
| 500 | &lt; 4.0s budget (typ. ~1.1s) | ~0.00s |
| 1000 | &lt; 12s budget (typ. ~3.9s) | ~0.00s |

Cold cost remains O(N) Resolver/`list_visible_mods` — **not rewritten** (Phase boundary). UI warm paths stay near-instant.

---

## 4. UI main thread

| Work | Status |
|------|--------|
| Detail cover decode | Off UI (CoverLoader) — panel + dialog |
| Detail directory_size | Off UI (`QThreadPool`) |
| Filter layout | Still UI but debounced + skip-if-unchanged |
| Game sidebar rebuild | Skipped when fingerprint unchanged |
| Snapshot build | Worker thread |
| Deploy audit | Delayed; not first paint |

---

## 5. Cache hits

| Scenario | Reuses snapshot? |
|----------|------------------|
| Warm navigation | Yes (`peek_snapshot`) |
| Warm game switch | Yes (in-memory cards) |
| Warm filter | Yes (ModFilterIndex) |
| Explicit Refresh | Force rebuild (intentional) |

---

## 6. N+1

- **Snapshot card loop:** no `get_mod_backup_row`; uses `get_mods_backup_rows` once — **eliminated**
- **Resolver `resolve()`:** still one SQLite backup-row read per mod (pure-read Resolver; **not changed** by Phase 9.1 mandate)

---

## 7. Reconcile concurrency

- Second start while running → `False` + queue pending root
- After finish → at most one follow-up run
- No parallel dual disk sweeps

---

## 8. Hygiene

**Deleted / already cleaned (Phase 9):** probe dumps under `data/_*.txt`, startup probes, test backup `910003`

**This phase:** no large deletions; only performance/test files

**Retained:** Resolver helpers, `read_info_metadata_dict`, Deploy stack, scripts for diagnostics

---

## 9. Regression

```
pytest tests/test_library_performance.py          → 11 passed
+ library/game/status/reconcile/backup/resolver/
  deploy_lifecycle/performance suites             → all passed
```

No production `mod/` pollution from fixtures (tmp_path only).

---

## Acceptance checklist

- [x] Warm navigation no meaningless rebuild  
- [x] Explicit Refresh force rebuild  
- [x] Game switch no disk scan  
- [x] Filter no disk scan  
- [x] Snapshot backup N+1 eliminated  
- [x] Detail cover non-blocking  
- [x] Detail size non-blocking  
- [x] Reconcile not dual-concurrent  
- [x] Startup not blocked by deploy audit  
- [x] 100/500/1000 benchmarks acceptable  
- [x] Core tests green  

---

## Residual (do not expand Phase 9.1)

1. Cold snapshot O(N) Resolver cost  
2. FlowLayout still rebuilds on real filter changes (debounced only)  
3. Resolver per-mod SQLite inside `resolve()`  

## STOP

Phase 9.1 complete. No Phase 10 / feature work.
