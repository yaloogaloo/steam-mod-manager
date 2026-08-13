# Phase 9 — Performance Audit (Read-Only)

**Date:** 2026-08-13  
**Scope:** Library / Game list / Detail / Filter / Cover / Startup  
**Constraint:** No business-rule changes. Deploy / Resolver / Backup / Identity frozen.

---

## Call chains (current)

### Library.refresh()

```
ModLibraryView.refresh()
  → mkdir(library_root)                         [UI]
  → start_reconcile_library_async()             [daemon]
  → LibraryLoadWorker.run()                     [QThread]
       → ModLibraryCache.load_snapshot(force=True)
            → build_library_snapshot()
                 → list_visible_mods()
                      → list_managed_mods (iterdir)
                      → per folder: read_info + resolve()
                 → batch SQLite: search_fields / tags / relations
                 → per mod: mtime, offline is_file, missing_content,
                            get_mod_backup_row(mid)   ← N+1
                 → resolve_games() + status summaries
  → _apply_library_snapshot()                   [UI]
       → _rebuild_game_list (from snapshot)
       → _render_mod_cards (create/rebind)
       → _apply_view_filter
  → _finish_library_load / optional show_mod
```

### Card click → Detail

```
on_mod_selected → detail_panel.show_mod()
  → resolve_mod_metadata (pure read)            [UI]
  → get_mod_display_info / deploy / tags / …
  → resolve_cover_path + sync QPixmap decode    [UI]
  → directory_size (os.walk, mtime-cached)      [UI]
```

### Game switch

```
_on_game_item_changed
  → _render_mod_cards(force_reload=_snapshot_dirty)
  → filter snapshot.cards by game_folder        [memory]
  → _apply_view_filter                          [memory + layout]
```

### Search / Source / Status / Offline / Favorite filters

```
signal → _apply_view_filter
  → filter_and_sort(ModFilterIndex entries)     [memory only]
  → FlowLayout takeAt/hide/addWidget            [UI layout O(V)]
```

### MainWindow startup

```
MainWindow.__init__
  → stylesheet + build Sync/Library/Deploy views
  → _restore_settings
       → if PAGE_LIBRARY: library_view.refresh()  (before show)
  → show()
  → QTimer(0): deploy audit [UI]
  → QTimer(0): reconcile async [daemon]
```

---

## A. Top 10 bottlenecks

| # | Bottleneck | Call chain | UI thread? | Scales with N? | Duplicate? |
|---|------------|------------|------------|----------------|------------|
| 1 | **Every refresh forces full snapshot rebuild** (`force=True`) | refresh → LibraryLoadWorker → load_snapshot | No (worker), but gates paint | Yes O(N) | Yes vs warm cache |
| 2 | **`list_visible_mods` → `resolve()` per Mod** (.info JSON, backup load, cover glob, SQLite) | build_library_snapshot | Worker | Yes O(N) | Inherent to full scan |
| 3 | **N+1 `get_mod_backup_row`** after batch DB reads | build_library_snapshot loop | Worker | Yes O(N) | Yes — batchable |
| 4 | **`_apply_view_filter` full layout thrash** | every filter/search/sort | **Yes** | O(V) layout | Yes on each keystroke/filter |
| 5 | **Detail sync cover decode** (`QPixmap` + scale) | show_mod → `_set_cover` | **Yes** | Per open | Does not use CoverLoader/cache |
| 6 | **Detail `directory_size` walk** on open | show_mod → `_mod_size_label` | **Yes** | O(files) | Cached by mtime only |
| 7 | **Nav→Library always `refresh()`** | main_window `_on_nav_changed` | Starts worker | O(N) again | Ignores warm snapshot |
| 8 | **`_rebuild_game_list` full clear/recreate** on every snapshot apply | _apply_library_snapshot | **Yes** | O(G) widgets | Sidebar wipe even if same games |
| 9 | **Card cover `exists`/`resolve` + fallback `resolve_cover_path`** on create/rebind miss | ModCardWidget | **Yes** (path only); decode async | O(visible) | CoverLoader OK if path known |
| 10 | **Reconcile + backup rebuild concurrent with load** | refresh + startup timers | Daemon, disk contention | O(N) copies/hash | Duplicate schedule (refresh + startup) |

---

## B–F answers (summary)

### B. UI main thread work today

- Widget create/rebind, FlowLayout filter, game sidebar rebuild
- Detail: resolve + sync cover + dir size + multiple SQLite reads
- Startup stylesheet + optional pre-show refresh kickoff
- Deploy audit on first event-loop tick

### C. Filesystem / `.info` / Resolver

| Location | FS | `.info` | Resolver |
|----------|----|---------|----------|
| Snapshot build | Heavy | Per mod | Per mod |
| Filter/search | None | None | None |
| Game switch (clean) | None | None | None |
| Detail show | Cover + size walk | Via resolve | Once |
| Card covers | Path check | Rare fallback | Rare |

### D. Cache effectiveness

| Cache | Effective? | Gap |
|-------|------------|-----|
| `ModLibraryCache` / `LibrarySnapshot` | Partial | `force=True` on every refresh/worker → warm cache unused |
| `ModFilterIndex` | Yes | Filters stay in memory |
| Card widget `_card_cache` | Yes | Rebind works |
| `cover_cache` + CoverLoaderManager | Yes for cards | Detail bypasses it |
| `dir_size` mtime cache | Yes after first | Cold open still walks |

### E. Duplicate work

1. Snapshot always rebuilt on Refresh / Library nav  
2. Batch SQLite + per-mod `get_mod_backup_row`  
3. `resolve()` already reads backup/SQLite; snapshot recomputes content_status from another backup row  
4. Reconcile scheduled from both startup and every refresh  
5. Game list rebuilt from scratch each snapshot apply  

### F. Snapshot model status

**Mostly present** — ideal shape already exists:

```
disk/SQLite → reconcile(async) → LibrarySnapshot → filter/sort → UI
```

Gaps are invalidation discipline (`force=True` always) and snapshot-build N+1, not a missing architecture.

---

## G. Minimal modification plan (priority order)

1. **Batch backup rows** in `build_library_snapshot` (eliminate N+1 SQLite)  
2. **Soft reload**: worker/nav use `force=False` when snapshot warm & not dirty; explicit Refresh keeps `force=True`  
3. **Detail cover** via CoverLoader / cover_cache (no UI-thread decode)  
4. **Defer Detail size badge** (async or after first paint)  
5. **Avoid duplicate reconcile** kick on refresh if already running  
6. **Do not rewrite** Resolver, Deploy, CoverLoader architecture, filter model  
7. Hygiene after perf: remove `BACKUP_BOOT` prints, dry-run test pollution, dead debug  

**Out of scope for Phase 9:** Deploy semantics, Resolver write side effects, new metadata sources, per-card threads.

---

## Module checklist

| Module | Finding |
|--------|---------|
| `ui/library_view.py` | Snapshot-driven; refresh always force; filter memory OK; layout thrash |
| `ui/mod_card.py` | Async covers; reuse OK |
| `ui/mod_detail_panel.py` | Pure resolve; sync cover + size on UI |
| `ui/mod_detail_dialog.py` | Sync cover |
| `services/mod_library_cache.py` | Snapshot core; N+1 backup rows; force default True |
| `services/game_library.py` | Used from snapshot; OK |
| `services/game_status.py` | Aggregated in snapshot; OK |
| `services/library_reconcile.py` | Async; I/O heavy; may contend |
| `services/mod_metadata_resolver.py` | Pure read; O(N) on list_visible |
| `services/cover_loader.py` | Thread pool OK |
| `ui/library_query.py` | Memory filter OK |
| `ui/main_window.py` | Nav refresh + dual async maintenance |

---

## Verdict

Architecture is **already snapshot-oriented**. Phase 9 should **close gaps** (batch DB, soft cache hit, Detail cover/size off UI thread, reconcile dedupe, hygiene) — **not** rebuild Library.
