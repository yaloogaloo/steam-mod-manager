# Phase 17 — Default pytest entrypoint plan

Plan only. **No mass move in this phase.** No tests deleted.

Goal: cut default `pytest` collection cost without dropping coverage of frozen systems.

## Current default

`pytest.ini`:

```
testpaths = tests
```

That collects everything under `tests/` (~370 files), including operator leftover-GC tools and one-shot folder cleanups.

There is no `tests/legacy/` or `tests/archive/` yet.

## Keep on the default path (do not move)

| Bucket | Where it lives today | Why |
|--------|----------------------|-----|
| Contract | `tests/contract/` | Asset/OPEN/cache/migration freeze |
| Identity | `tests/test_id_architecture_contract.py`, `tests/test_identity_*` | Frozen entity identity |
| Deploy | `tests/test_deploy_*.py`, WH3/security | Frozen deploy pipeline |
| Asset unit | `tests/test_asset_store.py`, `tests/test_asset_manifest.py`, `tests/test_no_live_asset_leftover.py` | Store/manifest/leftover scan |
| Collection | `tests/test_collection_*.py` | Frozen DB model |
| Product regressions | see “Stay in `tests/regression/`” | Live product bugs, not operator GC |

## Candidate later move → `tests/legacy/`

Only **historical migration / leftover-GC / one-shot operator tools**. Files stay in git; default `pytest` would skip the directory.

| File | Why it is a candidate |
|------|------------------------|
| `tests/regression/test_info_asset_cleanup.py` | Phase 7 SAFE_DELETE leftover `.info/assets` operator tool |
| `tests/regression/test_fast_info_asset_purge.py` | Fast leftover GC; no OPEN/Store writes |
| `tests/regression/test_legacy_backup_asset_cleanup.py` | Backup leftover `offline/assets` operator tool |
| `tests/regression/test_backup_backlog.py` | Manifest-debt operator modes / checkpoints |
| `tests/regression/test_cleanup_legacy_suffix_folders.py` | One-shot `*_900000000000xxxx` folder tool |
| `tests/regression/test_cleanup_duplicate_workspace_mods.py` | One-shot workspace duplicate tool |
| `tests/regression/test_cleanup_stardew_workspace_duplicates.py` | One-shot Stardew workspace tool |
| `tests/regression/test_orphan_backup_safe_cleanup.py` | Orphan Backup pairing GC tool |

Still runnable as `pytest tests/legacy` after the move.

## Stay in `tests/regression/` (not leftover-GC)

| File | Why it stays on default |
|------|-------------------------|
| `tests/regression/test_redeploy_cleanup.py` | Live Deploy: disappeared source files must leave the deploy tree |
| `tests/regression/test_relative_status_cleanup.py` | Live library filter / relative status |
| `tests/regression/test_detail_legacy_cleanup.py` | Live detail panel surface |
| `tests/regression/test_identity_data_migration_review.py` | Identity review tool; identity freeze — keep collected |

## `tests/archive/`

Do **not** create in this phase.

Use later only if product archives a **retired capability** together with its tests (candidate: `tests/test_readable_snapshot.py` if `readable_snapshot.py` is archived). That is a product decision, not test-folder hygiene.

Do not dump random old contract files here. Phase-numbered contracts in `tests/contract/` are still live freeze records.

## Suggested `pytest.ini` change (not applied)

After the `tests/legacy/` move:

```
[pytest]
testpaths = tests
addopts = --ignore=tests/legacy --ignore=tests/archive
```

or `norecursedirs = … tests/legacy tests/archive`.

Keep `timeout = 90`. Do not exclude `tests/contract`, identity, deploy, asset, or collection.

Optional later: a CI job `pytest tests/legacy` on a slower schedule so operator tools still run.

## What this phase does **not** do

- Does not move files.
- Does not change `pytest.ini`.
- Does not delete tests.
- Does not merge Identity/Deploy/Collection suites.
