# Phase 16 — Test governance & low-risk consolidation

No product architecture change. Identity, Asset Store, manifest schema, `cache/offline_view` OPEN, Deploy/WH3, Collection DB, and Backup storage-key/manifest protocol were not modified.

Not a refactor. No new abstraction. No production feature modules deleted. No valid tests removed.

## 1. Files changed

| File | Role |
|------|------|
| `tests/regression/test_identity_data_migration_review.py` | Task 1: `ROOT = project_root()` |
| `tests/regression/test_orphan_backup_safe_cleanup.py` | Task 1: source path via `project_root()` |
| `services/offline/layout_snapshot.py` | Task 5: unused imports after 15.1 wrapper removal |
| `services/importers/duplicate_check.py` | Task 5: unused platform helpers after `find_mod_by_source_url_relaxed` removal |
| `services/metadata_backup.py` | Task 5: unused `ModFileManager` import |
| `docs/phase16_contract_duplicate_audit.md` | Task 3 (audit only) |
| `docs/phase16_readable_snapshot_decision.md` | Task 4 (audit only) |
| `_tmp/phase16_asset_cache_cleanup_audit.json` | Task 2 audit |
| `data/asset_cache/` | Task 2: leftover directory removed from disk |

## 2. Deleted LOC / disk

**Git/code (Task 5 isolated imports only):** **−10** physical lines.

- `layout_snapshot.py`: `shutil`, `OFFLINE_STATUS_ARCHIVED`, `OFFLINE_STATUS_FAILED`, `PLATFORM_GITHUB`
- `duplicate_check.py`: `is_internal_mod_id`, `is_modio_external_id_pollution`, `is_provisional_external_id`
- `metadata_backup.py`: `ModFileManager`

Did not hit the 100–300 LOC guess: 15.1 already removed the dead functions; this pass only stripped imports that became unused. No empty `__all__`, no extra wrappers, no comment rewrites in Identity modules.

**Disk leftover (Task 2, not git):** `data/asset_cache/` — **807** URL-hash files, **~242 MB**. Regenerable HTTP cache. Not CAS. Not migrated into `cache/asset_cache` (already nonempty).

## 3. Added LOC

| | Physical lines (approx.) |
|--|--:|
| Task 1 test path fixes | +3 (`project_root` imports / ROOT) |
| Docs + audit JSON | ~170 |
| Production feature code | 0 |

## 4. Test results

`QT_QPA_PLATFORM=offscreen`.

| Suite | Result |
|-------|--------|
| Task 1 + Task 2 targeted | **24 passed** (`test_identity_data_migration_review`, `test_orphan_backup_safe_cleanup`, `test_cache_contract`) |
| `tests/contract` + `tests/regression` + Identity + Deploy + Asset lifecycle + Collection | **307 passed, 0 failed** |

Identity: `test_id_architecture_contract.py`, `test_identity_lifecycle_strict.py`, `test_identity_lifecycle_contract_freeze.py`, `test_identity_boundary_contract.py`.

Deploy: `test_deploy_service.py`, `test_deploy_security_boundary.py`.

Asset: `test_asset_lifecycle_contract.py`, `test_asset_store.py`, `test_asset_manifest.py` (plus the rest of `tests/contract`).

Collection: `test_collection_db.py`.

### FAIL classification (this run: none)

Historical failures from Phase 15.1 that this phase addressed:

| Prior fail | Class | This phase |
|------------|-------|------------|
| `test_cache_contract.py::test_production_data_has_no_cache_type_directories` | B — local leftover `data/asset_cache` | Deleted leftover; **PASS** |
| `test_identity_data_migration_review.py::test_tool_source_has_no_update_create_delete` | C — `parents[1]` after move to `tests/regression/` | **PASS** |
| `test_orphan_backup_safe_cleanup.py::test_cleanup_module_does_not_open_sqlite` | C — same ROOT bug | **PASS** |

No Class A (introduced by this phase) failures.

## 5. Remaining debt

| Item | Status |
|------|--------|
| `services/offline/readable_snapshot.py` (~1.1k LOC) | **KEEP** — tests-only provider; see `docs/phase16_readable_snapshot_decision.md`. ARCHIVE/REMOVE needs a product call. |
| Contract OPEN/cache-wipe overlap | Class A merge candidates documented; **not merged** |
| Phase 15 Class B modules | `info_asset_migration`, `backup_asset_migration`, `mod_type_legacy_migration`, `manifest_migration_plan`, `tools/archive/legacy_*` |
| `github.py` `readable_provider=` | Discarded kwarg; leave until readable snapshot is archived |
| Comments mentioning `import_orphan_candidates` | `library_reconcile.py` / `identity_service.py` — not 15.1/16 modules; Identity freeze |
| Regenerable cache | Live tree is `cache/asset_cache/` (~11k files). 333 leftover-only URL keys were deleted, not migrated — they can be re-downloaded |
| `test_deploy_audit_removed.py::test_case5` | Not in this pytest set. Previously flaked on offscreen encoding of `Anno 1800` (Class C), unrelated to 16 |

## Task summary

1. Regression ROOT → `core.paths.project_root()`. Logic unchanged.
2. Audited then deleted `data/asset_cache/`. OPEN/Store unused. `cache/asset_cache/` kept.
3. Contract duplicate audit written. No tests deleted.
4. Readable snapshot decision: KEEP. No delete.
5. Dead exports on 15.1 modules: unused imports only.
