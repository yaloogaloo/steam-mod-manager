# Phase 17 — Contract test consolidation audit

Governance only. Identity / Asset Store / OPEN / Deploy / Collection / Backup frozen.

Scope: `tests/contract/` (13 modules after Task 2), plus Asset unit files `tests/test_asset_store.py` and `tests/test_asset_manifest.py`. Assertions compared by **what they lock**, not helper names.

Task 2 merged **only Class A** listed under “Merged this phase”. Everything else stays.

## A — Completely overlapping (same invariant)

### Merged this phase

| Former home | Same assertion | New home |
|-------------|----------------|----------|
| `test_asset_final_contract.py::test_production_live_info_assets_empty` | production LIVE library has no leftover `.info/assets` (or `.info/offline/assets`) trees | `test_asset_zero_contract.py::test_production_live_info_assets_empty` |
| `test_asset_final_contract.py::test_production_backup_offline_assets_empty` | production Backup tree has no leftover `offline/assets` dirs | `test_asset_zero_contract.py::test_production_backup_offline_assets_empty` |
| `test_asset_lifecycle_contract.py::test_open_and_repair_do_not_recreate_info_assets` | finalize → OPEN → repair leave zero durable `.info/assets` | `test_asset_zero_contract.py::test_finalize_open_repair_leave_zero_info_assets` |
| `test_asset_lifecycle_contract.py::test_cache_delete_then_open_rebuilds_from_store` | wipe `offline_view` → OPEN rebuilds from Store | dropped; richer lock already in `test_asset_final_contract.py::test_cache_delete_rebuild_open` (`is_offline_view_path` + byte identity) |
| `test_asset_lifecycle_contract.py::test_cache_dirs_are_under_cache_not_data` | `offline_view` is under `cache/` not `data/` | dropped; superset in `test_cache_contract.py::test_cache_helpers_live_under_get_cache_dir_not_data` |
| `test_cas_only_info_contract.py::test_open_uses_offline_view_not_info_assets` | successful OPEN is `offline_view`, not LIVE `.info` | folded into `test_offline_open_contract.py::test_open_never_uses_live_assets` (now also asserts leftover tree gone + `sha256_file` of view bytes) |
| `test_cas_only_info_contract.py::test_legacy_fallback_open_without_manifest` | leftover physical `.info/assets` with no Store/manifest → OPEN fail-closed | already locked by the first half of `test_open_never_uses_live_assets` (`ensure_live_offline_openable` is None + `OFFLINE_ASSET_UNAVAILABLE`) |

No entire contract **file** deleted. Coverage of each invariant remains; failure messages stay on the owning contract (`asset_zero` / `offline_open` / `asset_final` / `cache_contract`).

### Remaining Class A — do not merge yet

| Pair | Why still listed | Action |
|------|------------------|--------|
| `test_cas_only_info_contract.py::test_missing_cas_repair_fails` vs `test_cas_only_backup_contract.py::test_missing_cas_repair_fails` | Same “delete CAS object → repair fails” story, **different surfaces** (LIVE repair vs Backup leftover-clear). Phase 16 called this A-if-both-paths-stay. Treating as **B** until a single test asserts both. | Keep both |
| Copied `_steam_mod` / `_make_steam_mod` fixtures | Fixture duplication, not extra invariants | Optional later `tests/helpers` — not this phase |

## B — Similar story, different boundary — keep all

| File / test | Unique lock |
|-------------|-------------|
| `test_info_asset_migration.py` | LIVE `.info/assets` → Store+manifest migrate: dry-run, corruption, traversal |
| `test_backup_asset_migration.py` | Backup offline → Store reference; restore without legacy assets |
| `test_cas_only_backup_contract.py` | Snapshot must not write Backup `offline/assets`; OPEN view must not write them back |
| `test_cas_only_info_contract.py` (remaining) | LIVE finalize clears `.info/assets`; repair from Backup CAS; miss recovery; snapshot without LIVE assets |
| `test_info_manifest_rebuild_contract.py` | Operator rebuild tool: checkpoint, rollback, invalid asset blocked |
| `test_asset_lifecycle_contract.py` (remaining) | Production scan: finalize owners, no unclassified `.info/assets` writers, leftover inventory not auto-deleted, Phase 9 artifacts |
| `test_asset_final_contract.py::test_capture_staging_not_in_info` | Capture staging is `cache/temp/offline_staging`, not `.info/assets` |
| `test_asset_final_contract.py::test_cache_delete_rebuild_open` | Cache wipe rebuild + view path + payload bytes |
| `test_asset_final_contract.py::test_asset_store_is_only_source` | Planted **different leftover bytes** lose to Store (not merely “tree absent”) |
| `test_offline_open_contract.py` | UI OPEN leftover LIVE/Backup trees are not sources; async miss; fail-closed finalize |
| `test_cache_contract.py` | `cache/` vs `data/` layout; leftover cache-type dirs; wipe cache while Store+DB survive; startup must not scan cache |
| `test_mod_type_legacy_migration.py` | Type catalog / `mods.type_id` — not Asset OPEN |
| `test_manifest_migration_plan.py` | Deploy path plan is no-write |
| `tests/test_no_live_asset_leftover.py` | **Shallow** library scan (`game/mod/.info/assets`) plus optional `mod/` and repo `data/mod_backup`. Different walk than `asset_zero` rglob. Keep. |
| `tests/test_asset_store.py` | Store unit API (put/get/verify/concurrency) |
| `tests/test_asset_manifest.py` | Manifest schema / path safety unit API |

OPEN leftover `.info/assets` still has **three gates**, not one:

- Phase 6 (`cas_only_info`) remaining tests — finalize/repair/miss (tree-absent as a **side** of those operations)
- Phase 11 (`offline_open`) — leftover is not an OPEN source; reason `OFFLINE_ASSET_UNAVAILABLE`
- Phase 12 (`asset_final`) — leftover tree with **different bytes** must lose to Store

`asset_zero` is the production leftover **inventory** + the finalize/OPEN/repair **zero-tree** loop. It does not replace the planted-stale-bytes test.

## C — Historical phase locks — do not delete

| Test | Why it stays |
|------|----------------|
| `test_cache_contract.py::test_docs_and_audit_artifacts_exist` | Pins Phase 10 docs/`_tmp` audits |
| `test_asset_lifecycle_contract.py::test_phase9_audit_artifacts_exist` | Pins Phase 9 audits |
| `test_asset_lifecycle_contract.py::test_production_write_need_fix_is_zero` | Sentinel list (currently empty) |
| `test_manifest_migration_plan.py::test_11_candidate_generation_has_zero_production_writes` | Source-level no-write lock |
| Entire `test_mod_type_legacy_migration.py` | Runtime catalog still calls migrate |
| Entire `test_info_asset_migration.py` / `test_backup_asset_migration.py` | Modules still live (Phase 15 Class B) |

Phase-numbered filenames remain the freeze record for Asset Store introduction.

## Task 2 outcome

- Added `tests/contract/test_asset_zero_contract.py` (3 tests).
- Strengthened `test_offline_open_contract.py::test_open_never_uses_live_assets`.
- Removed 7 duplicate test functions from lifecycle / final / cas_only_info.
- Deleted **0** contract files.
- Did **not** merge Store/manifest unit tests into contract (different layer).
