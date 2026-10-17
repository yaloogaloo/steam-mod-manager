# Phase 15.1 — Confirmed Class A dead code removal

White-box deletions only. Source of truth: `_tmp/phase15_confirmed_candidates.json` (`class_A_delete_candidates`).

Did **not** execute the Phase 15 AST scanner auto-DELETE list (276 symbols, known false positives).

Frozen (untouched): Identity architecture, Asset Store, manifest schema, Deployment core, WH3 deploy rules, Collection model.

Each item was grepped for production callers, imports, `importlib`, and tests. No Class A item was skipped for a production caller.

Machine audit: `_tmp/phase15_1_delete_audit.json`.

## Deleted files

| File | Physical LOC |
|------|-------------:|
| `services/deploy_conflict.py` | 97 |
| `services/offline/provider_snapshot.py` | 110 |
| **Total** | **207** |

Live deploy conflict detection remains `services.conflict` plus `services.deploy._schedule_post_deploy_conflict_scan`. The detail-panel QLabel `view_deploy_conflict` is unrelated.

## Deleted wrappers / public helpers (modules kept)

| Module | Removed | Kept |
|--------|---------|------|
| `services/offline/browser.py` | `capture_with_browser` | `BrowserSnapshotBackend` |
| `services/offline/layout_snapshot.py` | `run_layout_offline_snapshot` | `LayoutSnapshotProcessor` / providers |
| `services/orphan_import.py` | `import_orphan_candidates`, `OrphanImportResult` | `OrphanCandidate` |
| `services/mod_fs_observer.py` | `schedule_observe_mod_fs` (+ `__all__`) | `observe_mod_fs`, `schedule_observe_mods_fs_batch` |
| `services/metadata_backup_sync.py` | `start_rebuild_missing_metadata_backup_async` | `rebuild_missing_metadata_backup` + shutdown/join |
| `core/steam_api.py` | `enrich_scanned_mods` | `SteamWorkshopClient` |
| `services/metadata_backup.py` | `restore_check`, `metadata_dict_for_ui`, `resolve_backup_cover`, `resolve_backup_offline`, `mod_metadata_from_backup_row` | `restore_info_sidecar_from_backup`, `is_mod_folder_absent`, `managed_path_from_backup_row` |
| `services/backup_manager.py` | `deploy_backup_root`, `backups_dir_for` (+ `__all__`) | `BackupManager`, `DEPLOY_BACKUP_DIR_NAME` |
| `services/importers/duplicate_check.py` | `find_mod_by_source_url_relaxed` | `find_mod_by_source_url` |
| `services/mod_identity_validator.py` | `classify_identity_confidence` | remaining validators (no identity semantics change) |
| `services/mod_relationships.py` | `apply_declared_relationships` | `add_dependency_by_workspace_id` |
| `services/windows_path_locks.py` | `log_path_lock_holders` | `find_processes_locking_path`, `audit_self_open_files` |
| `services/offline/mhtml.py` | `is_mhtml_path` | `MHTML_SUFFIXES` |
| `ui/offline_open.py` | `cancel_detail_offline_open` | `start_detail_offline_open` |
| `ui/popup_trace.py` | `install_popup_trace` and now-orphaned install machinery (`PopupTraceFilter`, QToolTip wrap) | `log_popup` |
| `ui/window_lifecycle.py` | `create_dialog`, `parented_widget`, `is_registered_toplevel` | `register_toplevel`, `exec_dialog`, ownership guard |

Comments in `services/library_reconcile.py` / `services/identity_service.py` that mention `import_orphan_candidates` were left as-is (not callers; Identity freeze).

## Deleted LOC

Git physical-line diff for the Phase 15.1 file set (18 production files + 2 tests):

| | |
|--|--:|
| Insertions | 9 |
| Deletions | 897 |
| **Net** | **−888** |
| Whole-file deletions | 207 |
| Phase 15 Class A estimate | 765 |

Net is above the 765 estimate because `ui/popup_trace.py` also dropped the install event-filter / `QToolTip.showText` wrap that became unreachable after `install_popup_trace` was removed.

## Tests updated

- `tests/test_deploy_audit_removed.py` — `deploy_conflict.py` must **not** exist.
- `tests/test_no_runtime_mod_id_identity_usage.py` — removed `services/deploy_conflict.py` from `SCOPED_FILES`.

No other test imported the deleted symbols.

## Not deleted (and why)

No confirmed Class A item was skipped.

Class B from the Phase 15 confirmation list stays:

| Item | Why |
|------|-----|
| `services/info_asset_migration.py` | leftover `.info/assets` ingest still live |
| `services/backup_asset_migration.py` | OPEN materialize Store → `cache/offline_view` |
| `services/mod_type_legacy_migration.py` | catalog still calls it |
| `services/manifest_migration_plan.py` | tests lock a no-write Deploy path plan |
| `tools/archive/legacy_*` | leftover GC / operator tools |
| `mod_identity.read_entity_key` aliases | Identity frozen |
| `services/importers/image_scanner.py` | production still imports `IMAGE_*` |
| `services/offline/readable_snapshot.py` | product decision; tests still import |

Also not deleted: AST-scanner false positives; `services.conflict`; Deploy post-scan; UI `view_deploy_conflict` widget.

## Test results

`QT_QPA_PLATFORM=offscreen`.

### Identity / Deploy / Collection / Asset lifecycle

137 passed:

- Identity: `test_id_architecture_contract.py`, `test_identity_lifecycle_strict.py`, `test_identity_lifecycle_contract_freeze.py`, `test_identity_boundary_contract.py`
- Deploy: `test_deploy_service.py`, `test_deploy_security_boundary.py`, `test_deploy_conflict.py` (`services.conflict` + post-scan), `test_no_runtime_mod_id_identity_usage.py`, `test_window_lifecycle.py`
- Collection: `test_collection_db.py`
- Asset: `test_asset_lifecycle_contract.py`, `test_asset_final_contract.py`, `test_asset_store.py`, `test_asset_manifest.py`

### `tests/contract`

103 passed, 1 failed (pre-existing, not 15.1):

- `test_cache_contract.py::test_production_data_has_no_cache_type_directories` — production `data/asset_cache/` still has leftover URL-cache files.

### `tests/regression`

91 passed, 2 failed (Phase 14 path after move into `tests/regression/`, not 15.1):

- `test_identity_data_migration_review.py::test_tool_source_has_no_update_create_delete` — `ROOT = parents[1]` now points at `tests/`, so it looks for `tests/tools/identity_data_migration_review.py`.
- `test_orphan_backup_safe_cleanup.py::test_cleanup_module_does_not_open_sqlite` — same `parents[1]`; looks for `tests/services/backup_storage_cleanup.py`. The module import `from services.backup_storage_cleanup import ...` still works.

### Combined run (contract + regression + the contract files above + `test_deploy_audit_removed.py`)

326 passed, 4 failed. The fourth failure is `test_deploy_audit_removed.py::test_case5_game_name_not_squeezed_by_status_slot` (Qt offscreen encoding of `Anno 1800`). 15.1 only flipped the `deploy_conflict.py` existence assert in a different test; case5 was not edited.

None of the four failures import or call a deleted Class A symbol.
