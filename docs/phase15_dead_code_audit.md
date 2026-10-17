# Phase 15 — White-box dead code audit

Static AST + import graph over `core/`, `services/`, `ui/`, `tools/`, `tests/`, `scripts/`, `main.py`. **No deletions. No architecture change.**

Machine output: `_tmp/phase15_symbol_usage.json` (8019 symbols).  
Human-confirmed subset: `_tmp/phase15_confirmed_candidates.json`.

## Method

For each module-level function/class and each class method:

- **production_callers** — `core/`, `services/`, `ui/`, `main.py` that import or call it
- **test_callers** — `tests/`
- **tool_callers** — `tools/`, `scripts/`
- **dynamic_risk** — `LOW` / `MEDIUM` / `HIGH` (`importlib`, `getattr`, Qt slots, common method names)
- **decision** — `KEEP` | `DELETE` | `MERGE` | `REVIEW`

Limits of the scanner (why 276 raw `DELETE` is **not** an execute list):

- Relative imports (`from .sync_thread import SyncWorker`) under-count production callers
- `self._helper()` is not always attributed to the defining function
- Distinctive-name fallback can miss short public APIs and over-hit common names
- String monkeypatches are only partially indexed

**Execute only the human-confirmed Class A list**, after a second grep of the exact symbol.

## Scanner totals (non-test symbols)

| Decision | Count |
|----------|------:|
| KEEP | 3622 |
| REVIEW | 543 |
| DELETE (raw, high false-positive) | 276 |
| Module-level raw DELETE | 92 |
| Human-confirmed Class A | **18 symbols / 2 files** (~765 LOC) |

Frozen files were forced `KEEP`: Asset Store, manifest, `offline_view_cache`, `info_asset_runtime`, Identity, Deploy, WH3, Collection.

## Class A — confirmed delete (wait for approval)

| File | Symbol | Why | Risk | Est. LOC |
|------|--------|-----|------|---------:|
| `services/deploy_conflict.py` | whole module | no import of `detect_deploy_conflicts` | LOW | 98 |
| `services/offline/provider_snapshot.py` | whole module | never imported (only named in an ALLOWED_OTHER scan list) | LOW | 110 |
| `services/offline/browser.py` | `capture_with_browser` | unused wrapper | LOW | 4 |
| `services/offline/layout_snapshot.py` | `run_layout_offline_snapshot` | unused wrapper | LOW | 50 |
| `services/orphan_import.py` | `import_orphan_candidates` | unused; **keep** `OrphanCandidate` | LOW | 39 |
| `services/mod_fs_observer.py` | `schedule_observe_mod_fs` | unused scheduler; `observe_mod_fs` live | LOW | 38 |
| `services/metadata_backup_sync.py` | `start_rebuild_missing_metadata_backup_async` | no callers | LOW | 40 |
| `core/steam_api.py` | `enrich_scanned_mods` | no callers | LOW | 27 |
| `services/metadata_backup.py` | five unused public helpers | module stays | MEDIUM | 91 |
| `services/backup_manager.py` | `deploy_backup_root`, `backups_dir_for` | `__all__` only | MEDIUM | 14 |
| `services/importers/duplicate_check.py` | `find_mod_by_source_url_relaxed` | no callers | LOW | 80 |
| `services/mod_identity_validator.py` | `classify_identity_confidence` | no callers | MEDIUM | 40 |
| `services/mod_relationships.py` | `apply_declared_relationships` | `add_dependency_by_workspace_id` live | LOW | 31 |
| `services/windows_path_locks.py` | `log_path_lock_holders` | `audit_self_open_files` live | LOW | 46 |
| `services/offline/mhtml.py` | `is_mhtml_path` | `MHTML_SUFFIXES` live | LOW | 3 |
| `ui/offline_open.py` | `cancel_detail_offline_open` | no callers | LOW | 8 |
| `ui/popup_trace.py` | `install_popup_trace` | `log_popup` live | LOW | 21 |
| `ui/window_lifecycle.py` | `create_dialog` / `parented_widget` / `is_registered_toplevel` | `register_toplevel` live | LOW | 25 |

Do **not** treat scanner hits such as `normalize_witcher3_game_version`, `SyncWorker`, `_build_app_style`, or `SteamAPIError` as delete — those are false positives.

## Class B — LEGACY_COMPAT (do not delete now)

| Item | Keep because | Delete when |
|------|----------------|-------------|
| `info_asset_migration.py` | finalize still ingests leftover `.info/assets` | leftover trees stay 0 and finalize stops reading physical assets |
| `backup_asset_migration.py` | OPEN Store → `cache/offline_view` | never as a module |
| `mod_type_legacy_migration.py` | catalog still calls it | catalog drops legacy tokens |
| `manifest_migration_plan.py` | no-write Deploy plan + tests | product cancels or ships path migration |
| `tools/archive/legacy_*` | operator leftover GC | census stays 0 leftover buckets/trees |
| `read_entity_key` aliases | Identity freeze; tools still call | Identity follow-up PR only |
| `importers/image_scanner.py` | `IMAGE_*` re-export | callers use `image_picker` |
| `offline/readable_snapshot.py` | tests only; unfinished product | explicit product “never ship” |

## Class C — duplicate logic

See `_tmp/phase15_duplicate_logic.json`. Token-overlap ≥35% is a hint, not proof.

Real duplicates vs lookalikes:

- `validate_mod_files` in `mod_source_integrity` **is an alias** of `local_file_index.validate_mod_files` — keep the layer, do not merge into a new module.
- `materialize_manifest_to_dir` vs `materialize_imported_mod` — Store view vs import copy. **Not** a second CAS.
- Multiple `_sha256_file` helpers — Deploy/index hashing, **not** a second Asset Store.
- `EditModDialog` vs `ModEditDialog` — real UI duplication (see governance report).

## Dynamic import

No `importlib.import_module("services.deploy_conflict")` (or `provider_snapshot`) found. Qt `pyqtSlot` / `getattr` risk is marked HIGH on short method names; those stay REVIEW.
