# Phase 14 — Codebase consolidation report

Code governance only. Identity, Asset Store, manifest schema, Deployment core, WH3 deploy rules, Collection model, and `mod_manager.db` schema were not changed.

## Before

Source: VSCode Counter `2026-09-14_20-58-06` (the numbers in the Phase 14 brief).

| Metric | Count |
|--------|------:|
| Python files | 691 |
| Python LOC (code, excluding comments/blanks) | 182,825 |
| `tests/` | 366 files / 73,659 code |
| `tools/` | 64 files / 19,849 code |
| `services/` | 203 files / 60,655 code |

Physical-line recount immediately before this phase (excluding `_tmp`, venv, caches): **729 files / 229,151 physical / ~196,300 non-blank-non-`#`**.

The VSCode “code” column is stricter (docstrings counted as comments). Use the VSCode row as the public baseline; use the physical-line delta below for what this phase actually removed.

## After

| Scope | Files | Physical LOC | Non-blank non-`#` |
|-------|------:|-------------:|------------------:|
| Repo Python excluding `_tmp` / venv / caches | 724 | 228,781 | 196,015 |
| Same, excluding gitignored `mod/` library scripts | 692 | 226,754 | 194,415 |
| `services/` | 195 | 70,000 | 60,553 |
| `tests/` | 367 | 91,137 | 76,084 |
| `tools/` (including `tools/archive/`) | 71 | 29,060 | 25,620 |
| Production (`core/` + `services/` + `ui/` + `main.py`) | 247 | 104,912 | 91,233 |

## Removed

| | |
|--|--:|
| Deleted files | **6** |
| Deleted physical lines (those 6 files) | **~743** |
| Net physical-line change this phase | **−370** (deletes minus small import/doc edits) |
| Files moved (not deleted) | **13** modules to `tools/archive/` + **23** tests renamed |

Deleted (no production callers, no tests, no dynamic import):

- `tools/_repro_dialog_import.py`
- `tools/_repro_e3_detail.py`
- `tools/_repro_import_crash.py`
- `tools/_repro_real_import.py`
- `tools/_repro_real_library_refresh.py`
- `tools/_query_db_games.py`

Why 150k–160k was **not** hit: that target is a ~23k–33k cut from the VSCode baseline. Moving code to `tools/archive/` does not reduce repo Python LOC. Deleting tests would violate “do not delete test value.” Deleting `services/` runtime (`info_asset_migration`, Backup OPEN materialize, Identity, Deploy) is forbidden. Remaining bulk is tests (~76k) + production (~91k) + operator tools.

Further LOC cuts belong in a later phase that either (a) archives historical tests out of the default pytest path without deleting them, or (b) deletes proven-dead functions inside large modules after a per-symbol call graph — not a directory redesign.

## What changed

### 14.1 Services historical debt

Audit: `_tmp/phase14_service_cleanup_audit.json`

| Class | Action |
|-------|--------|
| A — runtime | Kept: Asset Store, OPEN, Identity, Deploy, Backup protocol, `info_asset_migration` (finalize leftover ingest), `backup_asset_migration` (Store → view), `mod_type_legacy_migration` (catalog), traces |
| B — one-shot | Moved out of `services/`: `legacy_workspace_backup.py`, `legacy_backup_finalize.py`, `library_diagnostics.py` → `tools/archive/` |
| C — dead | No unknown `services/` module deleted |

Each move:

1. **Why it existed:** leftover Backup storage-key (`workspace_id` bucket) GC / read-only library diagnostics.
2. **Callers:** tools + `tests/test_backup_storage_key_contract.py` / `scripts/library_diagnostics.py`. No UI / Deploy / OPEN.
3. **After:** same functions, new import path `tools.archive.*`.
4. **Maintenance:** `services/` is no longer the home for finished Backup-key migration.

Not moved: `manifest_migration_plan.py` (no UI caller, but tests lock a no-write Deploy path plan). `services/offline/readable_snapshot.py` (large; `readable_provider` still accepted — product decision required).

### 14.2 Test suite governance

Audit: `_tmp/phase14_test_cleanup_audit.json`

No tests deleted.

Layout started (only scanned phase/legacy/migration/cleanup files; the other ~340 tests stay under `tests/`):

```
tests/contract/     architecture constraints
tests/regression/   leftover GC / historical cleanup
tests/              remaining integration and unit tests (not bulk-moved)
```

Examples applied:

| Old | New |
|-----|-----|
| `tests/test_phase11_open_hardening.py` | `tests/contract/test_offline_open_contract.py` |
| `tests/test_phase10_cache_contract.py` | `tests/contract/test_cache_contract.py` |
| `tests/test_phase12_asset_final_contract.py` | `tests/contract/test_asset_final_contract.py` |
| `tests/test_phase9_asset_lifecycle.py` | `tests/contract/test_asset_lifecycle_contract.py` |
| `tests/test_phase5_cas_only_backup.py` | `tests/contract/test_cas_only_backup_contract.py` |
| `tests/test_phase6_cas_only_info.py` | `tests/contract/test_cas_only_info_contract.py` |
| `tests/test_phase7_info_asset_cleanup.py` | `tests/regression/test_info_asset_cleanup.py` |
| `tests/test_phase8_manifest_rebuild.py` | `tests/contract/test_info_manifest_rebuild_contract.py` |
| `tests/test_phase41_backup_backlog.py` | `tests/regression/test_backup_backlog.py` |

`tests/integration/` was not created empty. Existing full-flow tests remain in `tests/` until classified.

### 14.3 Tools

Audit: `_tmp/phase14_tools_cleanup_audit.json`

Moved to `tools/archive/` (kept, not deleted): Phase 6/CAS/legacy-asset audits, probes, `p0_data_hygiene_audit.py`, `finalize_legacy_backups.py`, `cleanup_legacy_workspace_backup.py`.

`__file__` repo-root resolution on those scripts is now `parents[2]`.

Current maintainer CLIs stay under `tools/` (`cleanup_cache.py`, identity recovery, `migrate_*` wrappers, deploy smoke, etc.).

### 14.4 Duplicate logic

Audit: `_tmp/phase14_duplicate_logic_audit.json`

Removed wrapper `repair_info_assets_from_backup_store` (alias of `repair_live_from_cas`). Callers now import `repair_live_from_cas` directly.

Kept Identity `read_entity_key` / `set_entity_key` aliases (still used by tools/tests; Identity system is frozen).

Kept `services/importers/image_scanner.py` re-exports (`IMAGE_*` still imported by production).

### 14.5 Dead code

Report: `_tmp/phase14_dead_code_report.json`

Deleted only the six `tools/_repro_*` / `_query_db_games.py` harnesses after confirming: no production import, no `importlib` of those modules, no tests.

Did not delete `deploy_conflict.py`, `readable_snapshot.py`, `manifest_migration_plan.py`, or custom-deploy audit helpers.

## Tests

Command (headless Qt):

```
python -m pytest tests/contract tests/regression/test_info_asset_cleanup.py tests/regression/test_legacy_backup_asset_cleanup.py tests/regression/test_backup_backlog.py tests/test_id_architecture_contract.py tests/test_identity_lifecycle_strict.py tests/test_backup_storage_key_contract.py tests/test_deploy_service.py tests/test_collection_db.py tests/test_no_live_asset_leftover.py tests/test_asset_store.py tests/test_asset_manifest.py tests/test_deploy_security_boundary.py
```

**251 passed, 1 failed.**

| Bucket | Result |
|--------|--------|
| Contract (`tests/contract/`) including cache, OPEN, asset lifecycle, CAS-only, manifest rebuild | Pass except one local disk check |
| Asset Store / manifest / leftover governance | Pass |
| Identity (`test_id_architecture_contract`, `test_identity_lifecycle_strict`) | Pass |
| Deployment (`test_deploy_service`, `test_deploy_security_boundary`) | Pass |
| Collection (`test_collection_db`) | Pass |

Failure (pre-existing local leftover, not introduced by this phase):

`tests/contract/test_cache_contract.py::test_production_data_has_no_cache_type_directories`

Production `data/asset_cache/` still has **807** regenerable URL-cache files. `cache/asset_cache/` already exists, so `migrate_legacy_data_caches()` will not `os.replace` over it. This is Phase 10 leftover on this machine, not a code regression. Do not delete it from this governance pass without an explicit cache-wipe request.

## Untouched (required)

- `mod_manager.db` schema
- `internal_id` / `workspace_id` / `mod_id`
- Asset Store hash and `manifest.json` schema
- Deployment pipeline and WH3 deploy rules
- Collection data model
- No new service abstractions, no `services/` directory redesign

## Audits

| File |
|------|
| `_tmp/phase14_service_cleanup_audit.json` |
| `_tmp/phase14_test_cleanup_audit.json` |
| `_tmp/phase14_tools_cleanup_audit.json` |
| `_tmp/phase14_duplicate_logic_audit.json` |
| `_tmp/phase14_dead_code_report.json` |
| `_tmp/phase14_apply_actions.json` |
