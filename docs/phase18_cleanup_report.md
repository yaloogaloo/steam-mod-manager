# Phase 18 — Symbol cleanup & test boundary

Governance, not a refactor. No production behavior change. No new abstraction. No database change. No large-file split.

Frozen and untouched: Identity (`internal_id` / `workspace_id` / `mod_id`), Asset Store + manifest schema, OPEN (`manifest → Asset Store → cache/offline_view`), Deploy / WH3, Collection DB, Backup storage protocol.

Did not modify `core/db_manager.py`, `services/deploy.py`, or `services/identity_service.py`.

## 1. Deleted symbols

Human-confirmed Class A from `_tmp/phase17_symbol_audit.json`. Each passed: no production caller, no `importlib`/`__import__`, no tests.

| Symbol | File | LOC |
|--------|------|----:|
| `EMPTY_ARCHIVE_MSG` | `services/importers/archive.py` | 1 |
| `local_directory_external_id` | `services/importers/nexus.py` | 11 |
| `hashlib` import | `services/importers/nexus.py` | 1 |
| `live_path_leak_refs` | `services/offline/backup_closure.py` | 4 |
| `iter_local_html_refs` | `services/offline/backup_closure.py` | 4 |
| `Iterator` import | `services/offline/backup_closure.py` | 1 |
| `process_layout_html` | `services/offline/layout_snapshot.py` | 7 |
| `iter_offline_page_candidates` | `services/offline/paths.py` | 14 |
| `_parse_srcset` | `services/offline/browser_snapshot/resource_rewriter.py` | 8 |
| **Total** | | **51** |

Machine record: `_tmp/phase18_symbol_delete_audit.json`.

Not deleted:

| Symbol | Class | Why |
|--------|-------|-----|
| `managed_path_from_backup_row` | C | Phase 15.1 KEEP public helper |
| `CANONICAL_OFFLINE_REL` / `LEGACY_STEAM_OFFLINE_REL` | B | Named OPEN path contract |

## 2. Test moves

Eight operator leftover-GC / one-shot tool tests moved `tests/regression/` → `tests/legacy/` (logic unchanged; they already used `project_root()` / `tmp_path`, not `parents[n]`).

| Moved | Tests |
|-------|------:|
| `test_info_asset_cleanup.py` | 14 |
| `test_fast_info_asset_purge.py` | 12 |
| `test_legacy_backup_asset_cleanup.py` | 14 |
| `test_backup_backlog.py` | 8 |
| `test_cleanup_legacy_suffix_folders.py` | 6 |
| `test_cleanup_duplicate_workspace_mods.py` | 7 |
| `test_cleanup_stardew_workspace_duplicates.py` | 4 |
| `test_orphan_backup_safe_cleanup.py` | 12 |
| **Total moved, not deleted** | **77** |

Left in `tests/regression/`: `test_redeploy_cleanup.py`, `test_relative_status_cleanup.py`, `test_detail_legacy_cleanup.py`, `test_identity_data_migration_review.py`.

`pytest.ini`: `norecursedirs` includes `legacy` and `archive`. Default `pytest tests` no longer descends into `tests/legacy`. Explicit `pytest tests/legacy` still collects (77).

Did not move contract, identity, deploy, asset, or collection.

## 3. `readable_snapshot` status

**KEEP.** See `docs/phase18_readable_snapshot_decision.md`. No code change.

## 4. Tests

`QT_QPA_PLATFORM=offscreen`.

| Suite | Result |
|-------|--------|
| `tests/contract` + `tests/regression` + Identity + Deploy + Asset + Collection | **226 passed** (36.39s) |
| `tests/legacy` (moved files) | **77 passed** (9.37s) |

Identity: `test_id_architecture_contract.py`, `test_identity_lifecycle_strict.py`, `test_identity_lifecycle_contract_freeze.py`, `test_identity_boundary_contract.py`.

Deploy: `test_deploy_service.py`, `test_deploy_security_boundary.py`.

Asset: `test_asset_store.py`, `test_asset_manifest.py` (lifecycle in contract).

Collection: `test_collection_db.py`.

Phase 17 same command was **303**. 303 − 77 moved = **226** on the default path. Combined with `tests/legacy`: still **303**, zero coverage dropped.

### FAIL classification

None this run.

| Class | This phase |
|-------|------------|
| A — introduced here | **0** |
| B — environment | **0** |
| C — historical test | **0** |

## 5. Remaining debt

| Item | Notes |
|------|-------|
| `readable_snapshot.py` (~1.1k) | **KEEP** until product ARCHIVE/REMOVE |
| `github.py` `readable_provider=` | Discarded kwarg; leave with readable snapshot |
| `managed_path_from_backup_row` | Public helper, no caller |
| Scanner remaining Class A (same_file > 1) | In-module-only public names; do not auto-delete |
| Phase 15 Class B modules | `info_asset_migration`, `backup_asset_migration`, `mod_type_legacy_migration`, `manifest_migration_plan` |
| Optional CI | Slow job `pytest tests/legacy` so operator tools still run |

## Task checklist

1. High-confidence unused symbols deleted after grep. 51 LOC.
2. Readable snapshot: **KEEP**. No code change.
3. Eight historical tests moved to `tests/legacy/`; `pytest.ini` skips that dir by default.
4. Dead imports only on Phase 18 edited files (`hashlib`, `Iterator`).
