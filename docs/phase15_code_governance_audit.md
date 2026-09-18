# Phase 15 — Code governance audit

White-box only. **Do not execute cleanup until this report is approved.**

Identity, Asset Store, manifest schema, Deployment, WH3 rules, Collection, and `mod_manager.db` stay frozen. No new architecture layer.

Companion files:

- `_tmp/phase15_symbol_usage.json`
- `_tmp/phase15_duplicate_logic.json`
- `_tmp/phase15_complexity_report.json`
- `_tmp/phase15_confirmed_candidates.json`
- `docs/phase15_dead_code_audit.md`

## Estimated gain (if Class A is approved)

| Bucket | Now (non-blank non-`#`) | Safe cut | Stretch (needs product/test retarget) |
|--------|------------------------:|---------:|--------------------------------------:|
| `services/` | ~60.5k | **−0.7k to −1.5k** | −2.5k if `readable_snapshot` + unused Backup helpers go |
| `tests/` | ~76k | **0 deleted** | **−8k to −12k collected** if operator-tool tests move to `tests/legacy/` and drop out of default `pytest` |
| Repo Python | ~194k excl. `mod/` | **−0.8k** | −5k to −15k combined with test-path move, still far from a 15k-line fantasy cut of production |

Honest range vs the 5k–20k brief: **~0.8k confirmed production lines**. Reaching 5k+ requires (1) product kill of `readable_snapshot` (~1k + its test), (2) retiring `ModDetailDialog` after tests move to the panel (~0.7k), (3) **not deleting tests**, only excluding leftover-GC tests from default collection (~8–12k collected LOC, files kept).

`services/` **60k → 50–55k is not available** without splitting or deleting live Deploy / archive / Identity repair / `db_manager`. Those files are complexity, not dead code. Do not split them in this phase.

## 1. Delete candidates

```
DELETE:
  file: services/deploy_conflict.py
  reason: no callers of detect_deploy_conflicts
  risk: LOW

DELETE:
  file: services/offline/provider_snapshot.py
  reason: module never imported
  risk: LOW
```

Plus unused **functions** listed in `docs/phase15_dead_code_audit.md` Class A (keep the host modules). Risk overall: **LOW**, except metadata-backup / backup-manager / identity-validator helpers at **MEDIUM** (public API that might be used from a notebook or a future tool).

## 2. Kept debt (LEGACY_COMPAT)

```
KEEP:
  file: services/info_asset_migration.py
  reason: leftover LIVE ingest still used by finalize
  future removal: after leftover trees stay 0 and finalize ignores physical assets

KEEP:
  file: services/backup_asset_migration.py
  reason: OPEN materialize Store → cache/offline_view
  future removal: never as a module

KEEP:
  file: services/mod_type_legacy_migration.py
  reason: runtime catalog still migrates legacy type tokens
  future removal: after catalog drops the call

KEEP:
  file: services/manifest_migration_plan.py
  reason: no-write Deploy path plan locked by tests
  future removal: after path migration ships or is cancelled

KEEP:
  file: tools/archive/legacy_workspace_backup.py
  reason: Backup storage-key leftover GC + contract tests
  future removal: after census shows 0 workspace_id buckets

KEEP:
  file: services/offline/readable_snapshot.py
  reason: unfinished offline helper; tests only
  future removal: product “never ship” decision
```

Identity `read_entity_key` aliases: **KEEP**. Frozen Identity system.

## 3. Asset / Backup / Import

### Asset — no second CAS

Keep: `asset_store.py`, `asset_manifest.py`, `offline_view_cache.py`, `info_asset_runtime.py`.

| Suspect | Verdict |
|---------|---------|
| Second Asset Store class | **None.** Only `services.asset_store.AssetStore`. |
| Second manifest writer | **None** as SoT. Backup and LIVE both write `manifest.json` of the same schema via `asset_manifest`. |
| Second materialize | `materialize_manifest_to_dir` (Store → view) vs `importers/materialize.py` (import folder). Different jobs. |
| `DEFAULT_ASSETS_DIR = "assets"` in snapshot modules | Capture **relative URL prefix**, not a durable tree. Staging is `cache/temp/offline_staging`. |

### Backup — protocol vs leftover bytes

Live Backup should be: metadata + `offline/index.html` + `offline/manifest.json` + Store hashes.

| Check | Result |
|-------|--------|
| Backup as OPEN byte source | Production OPEN uses Store → `cache/offline_view`. `restore_backup_assets_from_store` refuses LIVE `.info`. |
| Copy `offline/assets` | Snapshot **clears** leftover `offline/assets`. Operator GC lives under `tools/archive/`. |
| Duplicate Backup manifest | `sync_backup_offline_manifest` builds the **same** manifest schema from Store objects, not a second format. |

`migrate_all_backup_offline_assets` is operator/CLI. Keep until archive tools retire.

### Import — `services/importers/`

| Piece | Role | Duplicate? |
|-------|------|------------|
| `local_scanner.py` | directory classify / skip history | **Keep** — used by Deploy rules and info sidecar |
| `steam.py` `SteamImporter` | Steam import | **Keep** |
| `nexus.py` / `github.py` / `modio.py` / `other.py` | platform importers | **Keep** — different providers |
| `archive.py` | extract | **Keep** |
| `image_scanner.py` | shim over `image_picker` | **Compat** — `find_cover_candidate` always `None` |
| `duplicate_check.py` | import duplicate | **Keep** module; delete unused `find_mod_by_source_url_relaxed` |

No second extract pipeline found.

## 4. UI

Live detail surface is **`ModDetailPanel`** (`ui/library_view.py`).

| Pair | Verdict |
|------|---------|
| `EditModDialog` vs `ModEditDialog` | **MERGE candidate.** Panel uses `EditModDialog`. `ModEditDialog` only from `ModDetailDialog`. |
| `ModDetailPanel` vs `ModDetailDialog` | Dialog is **test-only** in production navigation. After tests retarget the panel, dialog (~631 LOC) + `ModEditDialog` (~125) become DELETE. Do not delete while `tests/test_resolver_entrypoints.py` and `test_offline_open_path_canonical.py` instantiate the dialog. |
| Threads (`import_thread`, `deploy_thread`, `sync_thread`, …) | Distinct jobs. **Keep.** |
| `popup_trace.log_popup` | Live debug. `install_popup_trace` unused. |
| `setVisible` / `setEnabled` / `hide` / `show` | Hundreds of calls; static scan cannot prove a branch never runs. No “always false” widget proven. **Do not strip UI state blindly.** |

## 5. Tests (do not delete coverage)

Current layout:

```
tests/contract/      architecture (Phase 14)
tests/regression/    leftover GC / historical cleanup
tests/               everything else (integration + unit)
```

Recommended **next** layout (move, don’t delete):

```
tests/contract/      keep
tests/regression/    product bug regressions (redeploy, relative status, detail)
tests/integration/   import / deploy / OPEN full flows (classify later)
tests/legacy/        operator-tool leftover GC (info/backup cleanup, suffix folders, …)
```

Overlapping themes (same behavior, many files):

| Theme | Files | Action |
|-------|------:|--------|
| Asset Store exists / put / get | 31 | Keep `test_asset_store.py` as contract; do not merge into one megafile |
| Manifest valid | 32 | Overlap with Store tests; later fold **duplicate asserts** into `tests/contract/` only |
| Cache wipe → OPEN rebuild | 19 | Keep one contract (`test_cache_contract` + `test_offline_open_contract`); leave others as regression |
| Identity `internal_id`/`workspace_id` | 112 | Frozen — **do not merge** |
| Deploy | 62 | Keep `test_deploy_service` + security/WH3 |
| Collection | 12 | Keep |

To shrink **collected** test LOC toward 60–70k without dropping coverage of production:

1. Add `tests/legacy/` and exclude it from default `pytest.ini` `testpaths` (still runnable as `pytest tests/legacy`).
2. Move leftover-GC operator tests there (`test_info_asset_cleanup`, `test_fast_info_asset_purge`, `test_legacy_backup_asset_cleanup`, `test_backup_backlog`, `test_cleanup_*` folder tools).
3. Do **not** delete Identity / Deploy / Collection / Asset Store tests.

`76k → 60–65k` by **deleting** tests is rejected. `76k → ~64–68k collected` by path exclusion is the only safe lever.

## 6. Large files (do not split now)

| File | LOC | Max function | Note |
|------|----:|-------------:|------|
| `core/db_manager.py` | 6255 | class ~5785 | SQLite facade. Splitting = architecture. **Keep.** |
| `ui/mod_detail_panel.py` | 5260 | class ~5404 | Live detail UI. **Keep.** |
| `ui/library_view.py` | 4905 | class ~5174 | Live library. **Keep.** |
| `services/deploy.py` | 3162 | class ~2517 | Frozen Deploy. **Keep.** |
| `services/archive.py` | 2761 | class ~1854 | Steam capture. **Keep.** |
| `services/identity_repair.py` | 2077 | 187 | Frozen Identity repair. **Keep.** |
| `services/sync.py` | 1468 | ~1526 | Steam sync. **Keep.** |
| `ui/mod_card.py` | 1359 | ~1362 | **Keep.** |
| `ui/styles.py` | 1293 | 909 | QSS builders. Unused-looking `_build_*` are internal. **Keep.** |
| `services/offline/readable_snapshot.py` | 1004 | 172 | REVIEW — tests only |
| `services/offline/layout_snapshot.py` | 1046 | 314 | Live Nexus layout path; unused `run_layout_offline_snapshot` only |
| `services/library_reconcile.py` | 1022 | 648 | Live. **Keep.** |

Giant **classes** (`DatabaseManager`, `ModDetailPanel`, `ModLibraryView`, `ModDeployer`) are implicit state machines. Recording them is the audit. Splitting them is a later product decision, not Phase 15.

## 7. Pytest (this audit)

```
python -m pytest tests/contract tests/test_id_architecture_contract.py
  tests/test_asset_store.py tests/test_asset_manifest.py
  tests/test_collection_db.py tests/test_deploy_service.py
  tests/test_deploy_security_boundary.py
```

**184 passed**, 1 failed: `test_production_data_has_no_cache_type_directories` — local leftover `data/asset_cache/` (807 files). Not a code defect. Identity / Asset Store / Deploy / Collection contracts **PASS**.

## 8. Proposed execute order (after approval)

1. Delete `deploy_conflict.py` + `provider_snapshot.py` + unused wrappers (Class A LOW).
2. Delete unused functions in Backup/import/UI helpers (MEDIUM last).
3. Retarget detail-dialog tests → panel; then delete `mod_detail_dialog.py` / `mod_edit_dialog.py`.
4. Optionally `pytest.ini` exclude `tests/legacy/` after moving operator GC tests.
5. Product decision on `readable_snapshot.py`.

Stop before any of: Identity aliases, Asset Store, Deploy, WH3, Collection, `db_manager` split.
