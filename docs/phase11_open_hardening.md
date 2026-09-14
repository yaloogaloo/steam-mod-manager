# Phase 11 — Asset Lifecycle Closure & OPEN Path Hardening

## 1. OPEN call chain

**Old**

```
UI (_open_offline)
  ↓
resolve_offline_page  (Qt thread)
  ↓
ensure_live / ensure_backup
  ↓
Store verify + sha256 + copy
  ↓
browser  (or leftover .info/assets / Backup offline/assets)
```

**New**

```
UI probe  (Qt thread)
  manifest exists?  cache fingerprint hit?
  ↓
cache hit → browser  (cache/offline_view)
  ↓
cache miss → BackgroundAssetWorker
                Store verify + materialize cache/offline_view
              signal → UI → browser
```

Failure: `OFFLINE_ASSET_UNAVAILABLE` + non-modal Repair entry (`repair_live_from_cas` then OPEN, also on the worker).

PERF_STAGE: `detail_open_probe_ms`, `detail_open_materialize_ms`, `detail_open_total_ms`.

Cache hit stays on the UI thread (manifest parse + fingerprint, no CAS content hash). Target: **&lt;100ms**. Cache miss does **not** hash/copy on the Qt thread.

## 2. Production search: `.info/assets` / `offline/assets`

### Allowed (migration / cleanup / audit / comments / skip-size)

| Location | Class |
|----------|--------|
| `services/info_asset_migration.py` | migration |
| `services/info_asset_runtime.py` `clear_*` / comments | cleanup + “never OPEN leftover” |
| `tools/archive/legacy_asset_tools/legacy_info_asset_cleanup.py` | cleanup |
| `tools/archive/legacy_asset_tools/fast_info_asset_purge.py` | cleanup |
| `tools/archive/legacy_asset_tools/legacy_info_manifest_rebuild.py` | audit/rebuild leftover |
| `tools/archive/legacy_asset_tools/legacy_backup_asset_cleanup.py` | cleanup |
| `services/backup_asset_migration.py` | migration / test restore helper |
| `tools/archive/legacy_asset_tools/backup_manifest_debt.py` | audit |
| `services/dir_size.py` | skip leftover in size |
| `services/offline/nexus_html_parser.py` | leftover gallery path comment |
| Capture writers (`archive.py`, snapshot/mhtml/html_rewriter) | **`cache/temp/offline_staging`**, then finalize |

### Forbidden in OPEN / resolver / browser launch — **removed**

- `ensure_live_offline_openable` no longer returns LIVE `.info/index.html` in place
- `ensure_backup_offline_openable` no longer returns Backup `offline/index.html` in place
- Detail `_open_offline` no longer calls `resolve_offline_page` on the Qt thread
- `ModMetadataResolver.resolve_offline_page` only returns `cache/offline_view` paths

## 3. Deleted / replaced code

| Item | Action |
|------|--------|
| `ui/library_view.py` `_build_filter_index` | **deleted** (no callers; contained OPEN) |
| `ui/library_query.py` `offline_page_exists` | **probe-only** (`probe_offline_open`); no materialize |
| `ui/background_asset_thread.py` `BackgroundAssetWorker` | **kept and wired** to Detail OPEN miss |

## 4. Performance

- Cache hit: UI probe only (`detail_open_probe_ms`); no `AssetStore.verify`
- Cache miss: verify + materialize on `BackgroundAssetWorker` (`detail_open_materialize_ms`)
- Capture finalize fail-closed: `cache/temp/offline_staging` deleted; capture does not report success. Capture must not create LIVE `.info/assets`.

## 5. Tests added

- `test_open_never_uses_live_assets`
- `test_open_never_uses_backup_assets`
- `test_open_cache_miss_background`
- `test_finalize_failure_cleanup`

in `tests/test_phase11_open_hardening.py`.
