# Phase 19 — Codebase consolidation

Governance + low-risk cleanup. Not a redesign. No new service layer. No large-file split. No schema change.

Frozen and behavior-unchanged: Identity (`internal_id` / `workspace_id` / `mod_id` + lifecycle), Asset Store + `manifest.json` + `cache/offline_view` OPEN, Deploy / WH3 / conflict, Collection DB, Backup storage protocol.

Did not modify `core/db_manager.py`, `services/deploy.py`, or `services/identity_service.py`.

## 1. Deletion list

| Symbol | File | LOC | Why |
|--------|------|----:|-----|
| `managed_path_from_backup_row` | `services/metadata_backup.py` | 7 | No production / tests / tools / importlib caller. Not Backup protocol. |
| `find_cover_candidate_in_roots` | `services/importers/image_scanner.py` | 11 | Deprecated stub; tests lock `find_cover_candidate` only. |
| `__all__` entry for that stub | same | 1 | |
| `shutil` unused import | `services/offline/browser_snapshot/resource_rewriter.py` | 1 | |
| `shutil` unused import | `services/offline/snapshot.py` | 1 | Re-exports of Nexus types kept. |
| `PLATFORM_MODIO` unused import | `services/metadata_refresh.py` | 1 | |
| `ModMetadata` unused import | `services/importers/steam.py` | 1 | |
| `Path` unused import | `services/offline/modio_browser_snapshot.py` | 1 | |
| **Total** | | **24** | |

Audits: `_tmp/phase19_managed_path_audit.json`.

Did **not** delete `__init__.py` re-exports (no confirmed Class C). Did **not** merge or delete contract test files.

## 2. Keep list

| Item | Why |
|------|-----|
| `services/offline/readable_snapshot.py` | Tests-only; ARCHIVE recorded, not executed. See `docs/phase19_readable_snapshot_final_audit.md`. |
| `github.py` `readable_provider=` | Discarded kwarg; goes with readable ARCHIVE. |
| `tests/test_readable_snapshot.py` | 9 tests still own the module. |
| `CANONICAL_OFFLINE_REL` / `LEGACY_STEAM_OFFLINE_REL` | Named OPEN path contract; `resolve_offline_page` inlines the same parts. |
| `find_cover_candidate` / `install_cover_from_source` | Tests still call them. |
| `is_mod_folder_absent` | Live Backup helper (`services/deploy.py`). |
| Package `__init__.py` barrels (`core`, `services`, `offline`, `importers`, `deploy_rules`) | A/B; not historical leftovers. |
| Remaining contract OPEN/cache/asset tests | Different boundaries (Phase 17 Class B). |
| Large files (`db_manager`, detail, library, `deploy`, `archive`, `identity_repair`) | Complexity, not dead code. See `docs/phase19_large_file_review.md`. |

## 3. Unresolved debt

| Item | Notes |
|------|-------|
| `readable_snapshot.py` (~1.1k) | ARCHIVE candidate only. Needs product “never OPEN backend”. |
| Package facades unused in-repo (`from core import DatabaseManager`) | Keep as public API. |
| `cas_only_info` vs `cas_only_backup` missing-CAS tests | Keep both surfaces. |
| `_steam_mod` fixture copies | Hygiene, not coverage. |
| Deploy `_deploy_with_context` (~751) | Do not split. |
| Phase 15 Class B modules | `info_asset_migration`, `backup_asset_migration`, `mod_type_legacy_migration`, `manifest_migration_plan`. |

## 4. LOC change

Production this phase: **−24** physical lines (unused helper + unused stub + unused imports).

Did not chase 50–200. Further cuts would have been `__init__` barrels, named OPEN constants, or large-file splits — all refused.

## 5. Tests

`QT_QPA_PLATFORM=offscreen`. There is no `tests/identity/` / `tests/deploy/` / `tests/collection/` directory; freeze files were run explicitly.

| Suite | Result |
|-------|--------|
| `tests/contract` + `tests/regression` + Identity + Deploy + Collection + Asset unit | **226 passed** (27.73s) |
| `tests/legacy` | **77 passed** (8.47s) |

Identity: `test_id_architecture_contract.py`, `test_identity_lifecycle_strict.py`, `test_identity_lifecycle_contract_freeze.py`, `test_identity_boundary_contract.py`.

Deploy: `test_deploy_service.py`, `test_deploy_security_boundary.py`.

Collection: `test_collection_db.py`.

Asset unit (Store freeze): `test_asset_store.py`, `test_asset_manifest.py`.

### FAIL classification

None.

| Class | This phase |
|-------|------------|
| A — introduced here | **0** |
| B — environment | **0** |
| C — historical test | **0** |

No Identity / Asset Store / Deploy / Collection behavior change.

## Task notes

1. Readable snapshot: tests-only; **KEEP**; ARCHIVE recorded, not executed.
2. `managed_path_from_backup_row`: Class A, deleted.
3. `__init__.py`: no Class C deletes.
4. Contract overlap: no remaining merge-safe duplicate functions.
5. Large files: review only.
6. Dead-symbol second pass: 24 LOC; no AST auto-delete.
