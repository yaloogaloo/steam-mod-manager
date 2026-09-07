# Architecture Cleanup Report

**Date:** 2026-09-07  
**Scope:** Identity / Library Projection / Viewport / Deploy residual + temporary artifacts  
**Policy:** Audit first → delete only Category **A** with confirmed zero call chains  
**Commit:** not created (per request)

---

## Summary

| Category | Meaning | Action this pass |
|----------|---------|------------------|
| **A** | Confirmed dead / unreferenced | **Deleted** |
| **B** | Still referenced or intentional compat | **Kept** |
| **C** | Uncertain / high risk / needs follow-up | **Kept + listed** |

This pass focuses on **safe dead-code and scratch-artifact removal**. High-risk items (deploy txn status/phase split, detail-panel hidden legacy widgets, identity repair module merge) stay in Category C for a later dedicated PR.

---

## 1. Deleted files

| Path | Reason | Replacement | Risk |
|------|--------|-------------|------|
| `services/offline/generator.py` | Zero imports outside the file; docstring says do not extend | `services.offline.snapshot.WebSnapshotDownloader` + provider snapshots | Low |
| `_tmp_civ6_*.json` (7 files) | Scratch Civ6 audit dumps | None (regenerable) | Low |
| `_tmp_refresh_*.json` (4 files) | Scratch refresh repro dumps | None | Low |
| `_tmp_edit_probe*/t.db` (3 dirs) | Scratch DB probes | None | Low |

---

## 2. Deleted functions / symbols

| File | Symbol | Reason | Replacement | Risk |
|------|--------|--------|-------------|------|
| `services/mod_library_cache.py` | `_missing_content_fast` | Defined, never called | `file_ops.read_is_missing_content` | Low |
| `services/mod_identity.py` | `_lookup_workspace` | Stub always `""`; never called; contract forbids workspace entity lookup | `find_mod_for_registration` / `find_mod_by_internal_id` | Low |
| `services/mod_identity.py` | `_lookup_relocated_folder` | Never called; contains forbidden `workspace_id` entity match | `path_lifecycle` / offline repair tooling | Low |
| `services/mod_identity.py` | `_legacy_token_matches_row` | Only used by `_lookup_relocated_folder` | N/A | Low |
| `services/deploy_status.py` | `as_public_status` | Zero callers | `resolve_deployment_status` / direct dict | Low |
| `ui/mod_detail_panel.py` | `_render_content_status_badge` | No-op; never called | Content axis shown elsewhere | Low |
| `ui/library_view.py` | unused import `filter_and_sort` | Import only; view uses `filter_sort_entries` | `filter_sort_entries` | Low |
| `ui/styles.py` | `QLabel#detailFilesLegacyHint` CSS | Orphan objectName — no widget uses it | — | Low |
| `ui/library_viewport.py` | `CARD_SLOT_WIDTH` | Unused alias | `CARD_WIDTH + CARD_H_SPACING` | Low |

---

## 3. Category B — keep (still used / intentional)

| Item | Why keep |
|------|----------|
| `file_ops` LEGACY `.info` / `migrate_numeric_mod_folders` | Active FS + sync |
| `deploy_paths.derive_legacy_relative` | Manifest remapping |
| `deploy_result` legacy dict bridge | Qt signal compat |
| `find_cover_candidate` no-op stubs | Explicitly asserted disabled by tests |
| `MetadataRefreshWorker` aliases | Tests patch aliases |
| `find_mod_by_external` / `find_by_published_id` | Still in upsert / repair / pollution paths |
| `manifest_migration_plan.py` | Plan-only but has dedicated tests |
| `SysCommandProbeFilter` / `install_syscommand_probe` | Wired from `main.py` when UI trace on |
| `popup_trace.log_popup` breadcrumbs | Still called from card/library selection |
| `PalworldPakStrategy` alias | Public export back-compat |
| `_selection_anchor` widget mirror | Still written; UX polish test asserts it — authority is `_selection_anchor_mod_id` |
| `_card_for_path` | Transitional Path payloads + tests |
| Identity repair / pollution / orphan modules | Active tooling + reconcile |

---

## 4. Category C — do not delete this pass (follow-up)

| Item | Issue | Suggested follow-up | Risk |
|------|-------|---------------------|------|
| Deploy txn `status` vs `phase` | Dual lifecycle; recovery only understands `prepared`/`backup_done` | Unify writers | **High** |
| Detail panel `_legacy_host` / hidden `view_*` / `meta_name_line` | Test/compat attribute surface | Migrate tests → visible widgets, then delete | Med |
| `_show_status_banner` deprecated alias | Tests still call it | Update tests → `_show_error_banner` | Low |
| `_card_entries` used as full-list (platform chips / peers / categories) | Viewport-only data treated as full game | Switch to `_game_row_entries` | Med |
| `mod_identity_repair` vs `identity_repair` overlap | Parallel repair stacks | Merge | Med |
| `identity_pollution` vs `status_recovery` | Overlap | Merge | Med |
| Strategy `.deploy()` overrides + stale tests | Inert shell; some tests still call `.deploy()` | Migrate tests to plan/apply | Med |
| `published_file_id` naming as internal PK carrier | Wide UI/services debt | Rename to `internal_id` in adapters | Med |
| Root `_tmp_*` not in `.gitignore` | Scratch files reappear | Add `/_tmp_*` to `.gitignore` | Low |

---

## 5. Architecture invariants preserved

- **Identity:** `identity_service` create gate; reconcile/orphan bind-only  
- **Projection:** `_game_row_entries` / `_filtered_row_entries` + viewport bind  
- **Selection:** `_selected_mod_ids` / `_selection_anchor_mod_id` authority  
- **Deploy:** prepare → backup → copy → hash → manifest → persist → clear  
- **Metadata layout:** name → desc frame → source → Workspace ID (fixed)

---

## 6. Verification plan

```text
pytest tests/test_civ6_deploy.py \
       tests/test_steam_refresh_identity.py \
       tests/test_library_shift_selection_viewport.py \
       tests/test_detail_metadata_long_description.py \
       tests/test_identity_authority_final.py \
       tests/test_startup_identity_resolve_architecture.py -q

pytest -q
```

---

## 7. Execution log

*(filled after delete + pytest)*
