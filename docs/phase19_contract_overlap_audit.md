# Phase 19 — Contract test overlap audit

Governance only. **No test files deleted.** No merge this phase.

Scope: `tests/contract/` OPEN/cache/asset contracts, plus unit `tests/test_asset_store.py` and `tests/test_asset_manifest.py`.

Phase 17 already merged the true Class A OPEN leftover / cache-wipe duplicates into `asset_zero` / `offline_open` / `asset_final` / `cache_contract`.

## Completely duplicate asserts (merge functions)

**None remaining** that share the same invariant and the same boundary.

| Pair considered | Why not merged |
|-----------------|----------------|
| `cas_only_info::test_missing_cas_repair_fails` vs `cas_only_backup::test_missing_cas_repair_fails` | Same story, **LIVE vs Backup** surfaces. Keep both (Class B). |
| `asset_zero` leftover rglob vs `tests/test_no_live_asset_leftover.py` | Different walk (rglob vs `game/mod/.info/assets` + optional `mod/`). Keep both. |
| `asset_final::test_cache_delete_rebuild_open` vs `cache_contract::test_wipe_cache_library_open_store_and_db_survive` | OPEN view rebuild vs Store+DB survival after cache wipe. Different locks. |
| `offline_open::test_open_never_uses_live_assets` vs `asset_final::test_asset_store_is_only_source` | Fail-closed leftover (no Store) vs planted **different bytes** lose to Store. |
| Copied `_steam_mod` helpers | Fixture duplication, not extra asserts. Optional later `tests/helpers` — not this phase. |

## Unit vs contract (keep all)

| File | Layer |
|------|--------|
| `tests/test_asset_store.py` | Store API: put/get/verify/concurrency |
| `tests/test_asset_manifest.py` | Manifest schema / path safety |
| `tests/contract/test_asset_final_contract.py` | Staging location, cache-wipe OPEN, planted leftover bytes |
| `tests/contract/test_offline_open_contract.py` | UI OPEN leftover not a source; async miss; fail-closed finalize |
| `tests/contract/test_cache_contract.py` | `cache/` vs `data/` layout; startup must not scan cache |

Do not fold Store/manifest unit tests into contract files.

## Historical phase locks (Class C — keep)

Phase 9/10 artifact tests, `test_mod_type_legacy_migration.py`, `test_info_asset_migration.py`, `test_backup_asset_migration.py`, `test_manifest_migration_plan.py`.

## Outcome

No function merge. No file delete. Coverage unchanged.
