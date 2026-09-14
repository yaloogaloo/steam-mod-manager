# Asset lifecycle — final status (Phase 13)

Asset Store is the only durable offline byte source. `manifest.json` is the only index. `cache/` is deletable.

## Storage Model

### .info

- `metadata.json`
- `manifest.json`
- `internal_id`
- `index.html` (Steam: `.info/index.html`; Nexus-style: `.info/offline/index.html`)

`.info` does not store offline asset bytes. Capture does not create `.info/assets`.

### data

- `mod_manager.db` — Library / Identity / Deployment
- `asset_store/` — unique offline bytes (content SHA-256)
- `mod_backup/` — Backup metadata + `offline/index.html` + `offline/manifest.json`
- `deploy_backup/`, `collection_covers/`, quarantine, type catalog

### cache

Safe to delete entirely. Rebuilt from Store + manifest.

- `offline_view/` — `file://` OPEN trees
- `temp/` — including capture `offline_staging/`
- `import_cache/`
- `headers/`
- `asset_cache/`

## Asset Flow

```
Capture
  ↓
cache/temp/offline_staging
  ↓
Asset Store
  ↓
manifest
  ↓
cache/offline_view
  ↓
browser
```

OPEN is only that path. It does not read `.info/assets` or Backup `offline/assets`.

## Forbidden

Must not exist:

- `.info/assets`
- `.info/offline/assets`
- Backup `offline/assets`
- `data/offline_view`

## Remaining Debt

LIVE leftover trees: **0** (Phase 13 deleted 2 unindexed trees / 9 files / 352273 bytes).

Backup `offline/assets`: **0**.

One-shot leftover GC lives under `tools/archive/legacy_asset_tools/`, not `services/`.

Still in `services/` (not Asset leftover GC; Backup storage-key migration):

- `legacy_workspace_backup.py`
- `legacy_backup_finalize.py`

## Acceptance

| Criterion | Result |
|-----------|--------|
| `.info/assets` = 0 | **PASS** (production library scan after Phase 13) |
| Backup `offline/assets` = 0 | **PASS** |
| OPEN does not read physical leftover assets | **PASS** |
| Capture does not create `.info/assets` | **PASS** (`cache/temp/offline_staging`) |
| Cache delete then OPEN rebuilds | **PASS** (tests) |
| Asset Store is the only durable offline byte source | **PASS** |
| Legacy asset GC removed from `services/` | **PASS** |

Closeout: `_tmp/Phase13_asset_zero_closeout.json`. Decision audit: `_tmp/phase13_leftover_decision_audit.json`.
