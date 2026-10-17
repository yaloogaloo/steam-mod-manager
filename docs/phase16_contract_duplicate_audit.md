# Phase 16 — Contract test duplicate audit

Audit only. No tests deleted.

Scope: `tests/contract/` (12 modules). Assertions were compared by **what they lock**, not by similar helper names.

## A — Completely overlapping; merge candidates later

Do not merge in Phase 16. These pairs repeat the same closed loop with only payload/mod_id differences.

| Keep (richer lock) | Overlaps | Same assertion |
|--------------------|----------|----------------|
| `test_asset_final_contract.py::test_cache_delete_rebuild_open` | `test_asset_lifecycle_contract.py::test_cache_delete_then_open_rebuilds_from_store` | finalize → OPEN → wipe `offline_view` → OPEN again from Store; leftover `.info/assets` stays gone. Final also asserts `is_offline_view_path` and byte identity. |
| `test_cas_only_info_contract.py::test_missing_cas_repair_fails` | `test_cas_only_backup_contract.py::test_missing_cas_repair_fails` | delete CAS object → `repair_live_from_cas` fails. Backup variant also clears leftover LIVE files; Info variant uses `finalize_live_offline_to_cas` first. Merge only if both paths stay asserted. |
| `test_cas_only_info_contract.py::test_repair_does_not_recreate_info_assets` | `test_cas_only_backup_contract.py::test_repair_and_miss_from_cas` (LIVE half) | Repair after snapshot does not recreate `.info/assets`. Backup test additionally locks Backup has no durable `offline/assets`. |

`_steam_mod` / `_make_steam_mod` helpers are copy-pasted across Phase 2–12 files. That is fixture duplication, not extra coverage. A later shared `tests/helpers` fixture would shrink LOC without dropping locks — still a merge, not this phase.

## B — Similar story, different boundary — keep all

| File | Boundary it uniquely protects |
|------|-------------------------------|
| `test_info_asset_migration.py` | LIVE `.info/assets` → Store+manifest migrate: dry-run, corruption, traversal, discover trees |
| `test_backup_asset_migration.py` | Backup offline → Store reference: restore without legacy assets, path traversal, `asset_cache` is not identity |
| `test_cas_only_backup_contract.py` | Snapshot must not write Backup `offline/assets`; OPEN view must not write them back |
| `test_cas_only_info_contract.py` | LIVE finalize clears `.info/assets`; OPEN uses `offline_view`; leftover-without-manifest OPEN fails closed |
| `test_info_manifest_rebuild_contract.py` | Operator rebuild tool: checkpoint, rollback, invalid asset blocked |
| `test_asset_lifecycle_contract.py` | Production scan: finalize owners, no unclassified `.info/assets` writers, leftover inventory not auto-deleted |
| `test_asset_final_contract.py` | Production leftover inventory is empty; capture staging is `cache/temp/offline_staging`; planted leftover bytes are ignored |
| `test_offline_open_contract.py` | UI OPEN: leftover LIVE/Backup trees are not sources; async miss; fail-closed finalize |
| `test_cache_contract.py` | `cache/` vs `data/` layout; leftover cache-type dirs in `data/`; startup must not scan cache |
| `test_mod_type_legacy_migration.py` | Type catalog / `mods.type_id` — not Asset |
| `test_manifest_migration_plan.py` | Deploy path plan is no-write; not Asset OPEN |

Lifecycle vs cache layout: `test_asset_lifecycle_contract.py::test_cache_dirs_are_under_cache_not_data` only checks `offline_view`. `test_cache_contract.py::test_cache_helpers_live_under_get_cache_dir_not_data` checks all cache helpers plus Asset Store staying under `data/`. Keep both.

OPEN “does not use leftover `.info/assets`”:

- Phase 6 (`cas_only_info`) — leftover without manifest → OPEN None / fail
- Phase 11 (`offline_open`) — same plus `prepare_offline_open` reason `OFFLINE_ASSET_UNAVAILABLE`
- Phase 12 (`asset_final`) — leftover tree with **different bytes** must lose to Store

Those are three gates, not one.

## C — Historical phase locks — do not delete

| Test | Why it stays |
|------|----------------|
| `test_cache_contract.py::test_docs_and_audit_artifacts_exist` | Pins Phase 10 docs/`_tmp` audits |
| `test_asset_lifecycle_contract.py::test_phase9_audit_artifacts_exist` | Pins Phase 9 audits |
| `test_asset_lifecycle_contract.py::test_production_write_need_fix_is_zero` | Sentinel list (currently empty) |
| `test_manifest_migration_plan.py::test_11_candidate_generation_has_zero_production_writes` | Source-level no-write lock |
| Entire `test_mod_type_legacy_migration.py` | Runtime catalog still calls migrate |
| Entire `test_info_asset_migration.py` / `test_backup_asset_migration.py` | Modules still live (Phase 15 Class B) |

Phase-numbered filenames are the product’s freeze record for Asset Store introduction. Collapsing them into one “OPEN test” would drop per-phase fail-closed behavior.

## Recommendation (not executed)

1. Later, optional: merge the two cache-wipe OPEN tests into `test_asset_final_contract.py` and keep a one-line import in lifecycle, **after** confirming both CI jobs run that file.
2. Do not touch `test_mod_type_legacy_migration.py` or `test_manifest_migration_plan.py`.
3. Shared `_steam_mod` helper is a test-hygiene follow-up, not a coverage cut.
