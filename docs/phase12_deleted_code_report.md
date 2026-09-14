# Phase 12 — Deleted code report

Leftover GC tools were **not** deleted.

## Gate

Delete the listed cleanup modules only when:

- LIVE `.info/assets` / `.info/offline/assets` file count = **0**
- Backup `offline/assets` file count = **0**

Read-only inventory (`_tmp/asset_lifecycle_final_inventory.json`, 2026-09-14):

| Surface | Trees | Files | Bytes |
|---------|------:|------:|------:|
| LIVE `.info/.../assets` | 28 (1 nonempty + 27 empty dirs) | 8 | 348937 |
| Backup `offline/assets` | 1 | 1 (0-byte) | 0 |

Because leftovers remain, operator tools stay.

## Candidates (kept)

| File | Why kept | Runtime caller? |
|------|----------|-----------------|
| `tools/archive/legacy_asset_tools/legacy_info_asset_cleanup.py` | LIVE leftover GC | **No** — `tools/cleanup_legacy_info_assets.py`, `tools/audit_legacy_info_assets.py`, tests |
| `tools/archive/legacy_asset_tools/fast_info_asset_purge.py` | Faster LIVE leftover GC | **No** — `tools/purge_legacy_info_assets.py`, tests |
| `tools/archive/legacy_asset_tools/legacy_backup_asset_cleanup.py` | Backup leftover GC | **No** — `tools/audit_legacy_backup_assets.py`, tests; `backup_manifest_debt.py` imports `_list_mod_ids` |
| `tools/archive/legacy_asset_tools/backup_manifest_debt.py` | Backup manifest audit | **No** — `tools/audit_legacy_backup_assets.py`, tests |
| `tools/archive/legacy_asset_tools/legacy_info_manifest_rebuild.py` | Rebuild LIVE manifest from leftover files | **No** — `tools/rebuild_legacy_info_manifests.py`, tests |
| `tools/cleanup_legacy_info_assets.py` | CLI for LIVE GC | operator |
| `tools/purge_legacy_info_assets.py` | CLI for LIVE GC | operator |
| `tools/rebuild_legacy_info_manifests.py` | CLI leftover index repair | operator |
| `tools/migrate_backup_assets_to_store.py` | CLI Backup → Store | operator |

## Grep confirmation

Searched `ui/` and production `services/` (excluding the modules themselves):

- No `ui/` imports of these modules.
- Production OPEN / capture / Repair does not call them.
- `tools/archive/legacy_asset_tools/backup_manifest_debt.py` → `legacy_backup_asset_cleanup._list_mod_ids` only (audit helper).

Callers that remain are **docs**, **tests**, **tools**, and **migration/cleanup** services.

## Deleted in this phase (compat, not the GC list)

These were obsolete **runtime** paths, not leftover GC:

| Change | Reason |
|--------|--------|
| `repair_info_assets_from_backup_store` gate-OFF `materialize` into LIVE `.info/assets` | Removed; function is now an alias of `repair_live_from_cas` |
| `repair_live_from_cas` delegate to gate-OFF Repair | Removed |
| `ModMetadataResolver._offline_existing` LIVE index fallback when gate OFF | Removed; presence uses probe (manifest) only |
| Capture `mkdir` of LIVE `.info/assets` | Replaced with `cache/temp/offline_staging` |
| `restore_backup_assets_from_store` into LIVE `.info` | Refused |

After an operator run of the existing purge tools brings leftover counts to zero, a later change may delete the GC modules listed above.
