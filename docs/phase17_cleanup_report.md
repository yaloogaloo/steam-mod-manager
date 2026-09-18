# Phase 17 — Contract consolidation & symbol hygiene

Governance, not a refactor. No production behavior change. No new abstraction. No database change. No large-file split.

Frozen and untouched: Identity (`internal_id` / `workspace_id` / `mod_id`), Asset Store + manifest schema, OPEN (`manifest → Asset Store → cache/offline_view`), Deploy / WH3, Collection DB, Backup storage protocol.

Did not modify `core/db_manager.py`, `services/deploy.py`, or `services/identity_service.py`.

## 1. What changed

| Item | Role |
|------|------|
| `tests/contract/test_asset_zero_contract.py` | **New** home for leftover-tree inventory + finalize/OPEN/repair zero-tree |
| `tests/contract/test_offline_open_contract.py` | Stronger OPEN source lock (`offline_view` + leftover gone + sha256) |
| `tests/contract/test_asset_final_contract.py` | Dropped duplicate leftover inventory (kept staging / cache-wipe / planted-stale-bytes) |
| `tests/contract/test_asset_lifecycle_contract.py` | Dropped duplicate cache-dir / cache-wipe / open-repair tests |
| `tests/contract/test_cas_only_info_contract.py` | Dropped duplicate OPEN-source / leftover-without-manifest tests |
| `docs/phase17_contract_consolidation_audit.md` | Task 1 + Task 2 record |
| `_tmp/phase17_symbol_audit.json` | Task 3 (no deletes) |
| `docs/phase17_readable_snapshot_final_review.md` | Task 4 KEEP |
| `docs/phase17_test_entrypoint_plan.md` | Task 5 plan only |

Production `core/` `services/` `ui/` feature code: **unchanged**.

## 2. Deleted / merged LOC

Goal was a smaller **maintenance surface**, not a LOC trophy.

| | |
|--|--:|
| Whole test files deleted | **0** |
| Duplicate test **functions** removed | **7** |
| New contract file | `test_asset_zero_contract.py` **+94** loc / **3** tests |
| Invariants dropped | **0** |
| Production LOC deleted | **0** |

Relocation vs deletion:

- 2 leftover-inventory tests **moved** into `asset_zero`.
- 1 finalize/OPEN/repair zero-tree test **moved** into `asset_zero`.
- 4 tests **removed** because a richer lock already existed (`asset_final` cache-wipe, `cache_contract` cache helpers, `offline_open` leftover fail-closed + view source).

Net this pytest set: **307 → 303** collected tests (−4 functions). Coverage of each merged invariant remains.

## 3. Tests

`QT_QPA_PLATFORM=offscreen`.

```
python -m pytest tests/contract tests/regression
  tests/test_id_architecture_contract.py
  tests/test_identity_lifecycle_strict.py
  tests/test_identity_lifecycle_contract_freeze.py
  tests/test_identity_boundary_contract.py
  tests/test_collection_db.py
  tests/test_asset_store.py tests/test_asset_manifest.py
  tests/test_deploy_service.py tests/test_deploy_security_boundary.py
```

| Suite | Result |
|-------|--------|
| `tests/contract` | **PASS** (includes new `asset_zero`) |
| `tests/regression` | **PASS** |
| Identity (4 files above) | **PASS** |
| Deploy (`test_deploy_service`, `test_deploy_security_boundary`) | **PASS** |
| Asset (`test_asset_store`, `test_asset_manifest`, lifecycle in contract) | **PASS** |
| Collection (`test_collection_db`) | **PASS** |
| **Total** | **303 passed, 0 failed** (34.35s) |

### FAIL classification

None this run.

| Class | Meaning | This phase |
|-------|---------|------------|
| A — introduced here | merge broke an invariant | **0** |
| B — environment | leftover cache/data on disk | **0** |
| C — historical test | path/ROOT/encoding flake | **0** |

## 4. Remaining debt

| Item | Class | Notes |
|------|-------|-------|
| `services/offline/readable_snapshot.py` (~1.1k) | C | **KEEP**. Tests-only provider. See `docs/phase17_readable_snapshot_final_review.md`. |
| `github.py` `readable_provider=` | C | Discarded kwarg; leave until readable snapshot is archived |
| Scanner high-confidence unused symbols | A (pending human grep) | `EMPTY_ARCHIVE_MSG`, `local_directory_external_id`, `live_path_leak_refs`, `iter_local_html_refs`, `process_layout_html`, `iter_offline_page_candidates`, `_parse_srcset` — **not deleted** |
| `managed_path_from_backup_row` | C | Phase 15.1 KEEP public helper; still no caller |
| `CANONICAL_OFFLINE_REL` / `LEGACY_STEAM_OFFLINE_REL` | B | Named path contract; `resolve_offline_page` inlines the same parts |
| Package `__init__.py` “unused imports” | B | Re-exports |
| `cas_only_info` vs `cas_only_backup` `missing_cas_repair_fails` | B | Same story, different surface — keep both |
| Operator leftover-GC tests still on default `pytest` | plan | See `docs/phase17_test_entrypoint_plan.md` |
| Phase 15 Class B modules | B | `info_asset_migration`, `backup_asset_migration`, `mod_type_legacy_migration`, `manifest_migration_plan` |

## 5. Next phase suggestions

1. **Do not** auto-delete from the symbol scanner. Human-confirm the short high-confidence list, then a tiny dedicated pass.
2. Product call on `readable_snapshot.py`: KEEP / ARCHIVE / REMOVE. Until then, leave it.
3. Execute the `tests/legacy/` move + `pytest.ini --ignore` from the entrypoint plan. Keep contract / identity / deploy / asset / collection on the default path. Add a slower CI job for `tests/legacy` if those tools still matter.
4. Do not merge remaining cas_only missing-CAS tests until one test asserts **both** LIVE and Backup paths.
5. Do not split `db_manager.py` / `deploy.py` / detail / library.

## Task checklist

1. Contract duplicate audit written. Class A merged only.
2. Duplicate OPEN / leftover-tree tests consolidated; files kept.
3. Dead-symbol JSON written; **no AST delete**.
4. Readable snapshot reviewed; **not deleted**.
5. Test entrypoint plan written; **no mass move**.
