# Asset architecture audit (white-box)

**Superseded for current lifecycle** by `docs/asset_lifecycle_final_status.md` (Phase 12). This file remains a historical white-box snapshot.

Source-traced. Not based on prior STATUS docs.

Default gate: `CAS_ONLY_INFO_ASSET_RUNTIME` = **true** (`core/cas_runtime.py`, `config/asset_runtime.json`).

## Target vs disk

| Target | Actual | Match |
|--------|--------|-------|
| `.info/metadata.json` | Present; sidecar metadata, not CAS | Yes |
| `.info/manifest.json` | Written beside LIVE `index.html` (`.info/` or `.info/offline/`) | Yes |
| `.info/internal_id` | Present; Entity identity proof | Yes |
| `data/mod_manager.db` | `data/mod_manager.db` | Yes |
| `data/asset_store/` | `data/asset_store/` | Yes |
| `data/backup/` | **`data/mod_backup/`** (not `backup/`) | Name differs |
| `cache/offline_view/` | `core.paths.offline_view_cache_dir()` | Yes |
| `cache/asset_cache/` | URL-keyed Steam static cache | Yes |
| `cache/import_cache/` | Import/deploy extract staging | Yes |
| `cache/headers/` | Steam header images | Yes |
| `cache/temp/` | Short-lived (incl. Repair verify tree) | Yes |

Covers remain `.info/cover.*` / `data/collection_covers/` — outside the offline CAS pipeline.

## Confirmation

### Asset Store is the unique durable **offline-asset** source

**Mostly yes** for Workshop/offline HTML assets when the gate is on.

Call sites that persist durable bytes into Store:

- Capture finalize: `finalize_live_offline_to_cas` → `migrate_info_assets` (`services/info_asset_runtime.py`)
- Backup snapshot: `snapshot_offline_closure` → Store put + Backup `manifest.json` (`services/offline/backup_closure.py`)

Not Asset Store (by design, not a bypass of offline CAS):

- `cache/asset_cache` — URL hash cache (`services/archive.py`), regenerable
- `.info/cover.*` — cover files
- Mod payload under `mod/`

### Manifest is the unique asset **index**

**Yes** for CAS OPEN: HTML refs → `manifest.json` paths → Store SHA-256.

Two manifests exist (LIVE vs Backup). They index the **same Store objects**. They are not a second byte store.

### Cache is fully deletable

**Yes.** Phase 10 helpers all live under `get_cache_dir()`. OPEN rematerializes `offline_view` from Store + manifest. `asset_cache` / `import_cache` / `headers` / `temp` are regenerable.

### `.info/assets` has exited the production lifecycle

**No — incomplete.**

Still in the production graph:

1. **Capture staging** — archive/snapshot writers `mkdir` `assets/` then `safe_finalize_live_offline`. Staging is intended temporary.
2. **Finalize soft-fail** — `safe_finalize_live_offline` logs and does not raise; leftover `assets/` can remain.
3. **OPEN disk fallback** — `ensure_live_offline_openable` (gate ON, **no usable CAS**): if physical files satisfy HTML closure, returns the LIVE index **in place** (`info_asset_runtime.py` after the CAS materialize block).
4. **Backup OPEN in-place** — `ensure_backup_offline_openable` returns Backup `index.html` when `offline/assets` files still exist beside it (no Store).
5. **Gate OFF Repair** — `repair_info_assets_from_backup_store` materializes into LIVE `assets/` (`backup_asset_migration.py`). Default gate is ON, so this is dormant unless overridden.

## Production call chains (traced)

### Capture

```
Steam: OfflinePageArchiver._archive_body / archive_rendered_html / ensure_offline_page
Nexus/GitHub/manual: provider_snapshot / github / nexus_manual / manual_import
        ↓
staging: output_dir/assets  (HTTP/MHTML/import writers)
        ↓
safe_finalize_live_offline  [unique production finalize wrapper]
        ↓
finalize_live_offline_to_cas
        ↓
migrate_info_assets → Asset Store + LIVE manifest.json
        ↓
clear_info_assets_dir  (gate ON)
```

Finalize owners (direct `safe_finalize_live_offline`):

- `services/archive.py`
- `services/offline/provider_snapshot.py`
- `services/offline/github.py`
- `services/offline/nexus_manual.py`
- `services/offline/manual_import.py`

No other production module was found calling `finalize_live_offline_to_cas` except via that wrapper (plus tests/tools).

### OPEN (LIVE)

```
UI btn: ModDetailPanel._open_offline / ModDetailDialog._open_offline
        ↓  (Qt main thread)
ModMetadataResolver.resolve_offline_page
        ↓
ensure_live_offline_openable
        ↓
fingerprint hit → cache/offline_view  (no Store verify)
        or
load_usable_live_manifest → store.verify (full content SHA-256 per object)
        ↓
_materialize_live_view → materialize_manifest_to_dir (verify again + hash copy)
        ↓
QDesktopServices.openUrl(file:// cache/offline_view/.../index.html)
```

Detail **fill** / `_has_offline_page` uses `probe_live_offline_available` (manifest existence only) — does **not** materialize.

MISS: `resolve_offline_page` → `ensure_backup_offline_openable`.

### Repair

No UI caller. Production alias:

```
repair_info_assets_from_backup_store  (gate ON)
        ↓
repair_live_from_cas
        ↓
verify_manifest_against_store
        ↓
materialize to cache/temp/repair_check_* then delete
        ↓
write LIVE manifest.json only  (written=0 durable assets)
```

Does **not** populate `cache/offline_view`. Next OPEN does that.

Gate OFF: materialize into LIVE `.info/.../assets` — forbidden by the target model; reachable only if the gate is disabled.
