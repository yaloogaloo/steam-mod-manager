# Cache layout

`data/` holds durable business state. `cache/` holds regenerable performance data that may be deleted at any time.

```
cache/
├── offline_view/   # ephemeral file:// OPEN trees (Asset Store + manifest)
├── asset_cache/    # URL-keyed Steam static download cache
├── import_cache/   # temporary extract staging (import + deploy unpack)
├── headers/        # Steam Store header images
└── temp/           # short-lived task files, including capture offline_staging
```

All of these paths are created through `core.paths`. Production code must not hardcode `data/asset_cache`, `data/import_cache`, or `data/headers`.

## Layering

| Tree | Class | Source of Truth? | Safe to delete? |
|------|--------|------------------|-----------------|
| `data/mod_manager.db` | BUSINESS | Library / Identity / Deployment rows | No |
| `data/asset_store` | BUSINESS | Unique durable asset bytes (content SHA-256) | No |
| `data/mod_backup` | BUSINESS | Backup protocol | No |
| `data/deploy_backup` | BUSINESS | Deployment backup | No |
| `data/collection_covers` | BUSINESS | Collection covers | No |
| `cache/**` | CACHE | Never | Yes — wipe the whole tree |

Asset Store is the only durable asset source. OPEN rematerializes `cache/offline_view` from LIVE/Backup `manifest.json` + Asset Store.

Capture stages bytes under `cache/temp/offline_staging`, then finalize writes Asset Store + LIVE `manifest.json`. `.info` keeps metadata / manifest / `internal_id` / `index.html` only. OPEN / Repair / MISS must not create `.info/assets`.

## Path helpers

| Helper | Path |
|--------|------|
| `get_cache_dir()` | `cache/` |
| `offline_view_cache_dir()` | `cache/offline_view` |
| `asset_cache_dir()` | `cache/asset_cache` |
| `import_cache_dir()` | `cache/import_cache` |
| `headers_cache_dir()` | `cache/headers` |
| `cache_temp_dir()` | `cache/temp` |
| `asset_store_dir()` | `data/asset_store` |
| `database_path()` | `data/mod_manager.db` |

`import_cache_root()` in `services.importers.archive` is a compatibility wrapper around `import_cache_dir()`.

Retired paths (must not be created): `data/offline_view`, `data/asset_cache`, `data/import_cache`, `data/headers`.

## Deleting cache/

Wiping `cache/` is a supported operation:

1. Application start still succeeds (helpers recreate empty directories on demand).
2. Library stays DB-first and does not walk `cache/`.
3. OPEN rebuilds `cache/offline_view` from Asset Store + manifest.
4. Asset Store, DB, Backup, Identity, and Deployment are unchanged.
5. Startup does not scan `cache/` contents. Prune of `asset_cache` stays lazy (archiver enter), never a boot walk.

`python tools/cleanup_cache.py --execute --wipe-all` removes the tree under `cache/` only.

## Migration (Phase 10)

Upgrade relocate is directory-level `os.replace` of the three named trees (and the prune stamp). It does not hash, OPEN, or materialize the library. `migrate_legacy_data_caches()` never touches `mod_manager.db`, `mod_backup`, `asset_store`, or `deploy_backup`.
