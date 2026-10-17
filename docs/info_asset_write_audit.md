# LIVE `.info` write-path audit (Phase 9 / updated Phase 12)

Scope: production Python under `services/` (plus UI call-ins).  
Question: does any path **persist** durable `.info/assets` (or `.info/offline/assets`)?

Capture staging is **not** under `.info`. Bytes go to `cache/temp/offline_staging`, then finalize writes Asset Store + LIVE `manifest.json` and **deletes** staging.

Historical leftover trees may still exist on disk; they are not writers. See `_tmp/asset_lifecycle_final_inventory.json`.

## Verdict

**PRODUCTION_WRITE (need fix): 0**

No production path writes durable `.info/assets` as Source of Truth.

---

## PRODUCTION_WRITE

Need fix: **none**.

| File | Function | mkdir/copy `.info/assets` | finalize | Bypass? |
|------|----------|---------------------------|----------|---------|
| — | — | — | — | — |

---

## PRODUCTION_STAGING (capture; not a defect)

These write into `cache/temp/offline_staging/<key>/assets` when the output root is LIVE `.info`. HTML still references `./assets/...`. Production callers then run finalize.

| File | Role | Finalize owner |
|------|------|----------------|
| `services/offline/staging.py` | Staging root / cleanup | used by capture + finalize |
| `services/archive.py` `_archive_body` / `archive_rendered_html` | Steam / rendered HTML asset download | same file: `require_cas_finalize` |
| `services/offline/provider_snapshot.py` `run_browser_offline_snapshot` | Nexus/GitHub browser snapshot wrapper | same file |
| `services/offline/github.py` `GithubOfflineProvider` | GitHub capture | same file |
| `services/offline/nexus_manual.py` `NexusManualOfflineProvider` | Manual HTML/MHTML import | same file (+ `manual_import`) |
| `services/offline/manual_import.py` `import_offline_snapshot` | HTML/MHTML import | same file |
| `services/offline/browser_snapshot/resource_rewriter.py` | Playwright rewrite + download | `provider_snapshot` |
| `services/offline/browser_snapshot/manager.py` | Orchestrates rewriter | `provider_snapshot` |
| `services/offline/snapshot.py` `WebSnapshotDownloader` | Legacy HTTP snapshot helper | `provider_snapshot` / GitHub fallback |
| `services/offline/layout_snapshot.py` `LayoutSnapshotDownloader` | Layout snapshot helper | `provider_snapshot` |
| `services/offline/html_rewriter.py` | Copy local files into staging `assets/` | `manual_import` |
| `services/offline/mhtml.py` `store_mhtml_snapshot` | Extract MHTML parts into staging | `manual_import` |
| `services/offline/nexus_cleaner/resource_processor.py` | Persist Nexus MHTML resources | `manual_import` cleaner |
| `services/offline/github_browser_snapshot.py` | HTML only; no empty `.info/assets` | `github.py` |

Failed capture deletes staging. Finalize always deletes staging on success.

---

## LEGACY_ONLY

Operator leftover GC. Not runtime OPEN. **Not deleted in Phase 12** because production leftovers are not yet zero.

| File | Note |
|------|------|
| `services/backup_asset_migration.py` `repair_info_assets_from_backup_store` | Alias of `repair_live_from_cas`. Never writes LIVE `assets/`. |
| `services/info_asset_runtime.py` `ensure_live_offline_openable` | `cache/offline_view` only. |
| `services/info_asset_runtime.py` `repair_live_from_cas` | Verifies CAS; writes LIVE `manifest.json` only. |
| `services/info_asset_migration.py` | Reads leftover `.info/assets` into Store. Never the OPEN SoT. |
| `tools/archive/legacy_asset_tools/legacy_info_manifest_rebuild.py` | Rebuilds manifest from leftover files. |
| `tools/archive/legacy_asset_tools/legacy_info_asset_cleanup.py` / `tools/archive/legacy_asset_tools/fast_info_asset_purge.py` | Delete leftovers. Not writers. |
| `services/legacy_backup_finalize.py` / `backup_closure.py` | Backup protocol. OPEN materializes `cache/offline_view`. |

---

## TEST_ONLY

Tests that `mkdir` `.info/assets` as fixtures (class B): `tests/test_phase6_cas_only_info.py`, `tests/test_phase7_info_asset_cleanup.py`, `tests/test_fast_info_asset_purge.py`, `tests/test_cas_only_offline_capture_open.py`, `tests/test_cache_directory_contract.py`, `tests/test_no_data_offline_view_regression.py`, and similar.

---

## Repair / OPEN / MISS / Backup restore

| Path | Depends on durable `.info/assets`? |
|------|-------------------------------------|
| OPEN (`ensure_live_offline_openable`) | No (CAS + `cache/offline_view`) |
| Repair (`repair_live_from_cas`) | No |
| Restore helper (`restore_backup_assets_from_store`) | Refuses LIVE `.info`. Production OPEN passes `cache/offline_view`. Tests may restore into a temp dest. |
