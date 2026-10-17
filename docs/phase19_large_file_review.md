# Phase 19 — Large file hotspot review

Audit only. **No file split. No import-boundary change.**

Physical line counts and largest functions (AST `end_lineno − lineno`). Nested methods count separately.

| File | Physical LOC | Largest function | Lines | Role |
|------|-------------:|------------------|------:|------|
| `core/db_manager.py` | 6797 | `list_mod_list_items` | 206 | SQLite facade (frozen Identity/Collection I/O) |
| `ui/mod_detail_panel.py` | 5936 | `_fill_view` | 235 | Live detail UI |
| `ui/library_view.py` | 5702 | `_build_ui` | 439 | Live library |
| `services/deploy.py` | 3467 | `_deploy_with_context` | 751 | Frozen deploy pipeline |
| `services/archive.py` | 3134 | `_archive_body` | 295 | Steam offline capture |
| `services/identity_repair.py` | 2282 | `_apply_merge` | 187 | Frozen Identity repair |

## Hotspots (do not extract this phase)

### `services/deploy.py`

`_deploy_with_context` (~751) is the deploy state machine. `_resolve_context` (~380) and `_undeploy_mod_body` (~212) sit on the same pipeline. Splitting would change the import/call boundary of frozen Deploy / WH3 (`_finish_wh3_activation_deploy` ~158). **Keep.**

Safe-looking later extract (not done): local path-normalize snippets inside `_resolve_context` — still a deploy-module private helper, not a new service.

### `core/db_manager.py`

`DatabaseManager` is one SQLite session facade. `upsert_mod` / `update_mod_identity_fields` / `update_mod_platform_info` share row-shape checks. Extracting them is an Identity/DB boundary change. **Keep.**

### `ui/mod_detail_panel.py`

`_fill_view`, `_build_metadata_section`, `_render_metadata_rich_block` overlap in field formatting. Moving them to a new module would be a UI architecture split. **Keep.**

### `ui/library_view.py`

`_build_ui` (~439) wires the window. `_apply_view_filter` (~242) is the filter state machine. Incremental viewport (`_try_incremental_mod_viewport`) is load-bearing. **Keep.**

### `services/archive.py`

`_http_get` vs `_http_get_asset` look similar (timeouts, UA, proxy). They are capture internals, not a second Asset Store. OPEN still finalizes via CAS. **Keep.**

### `services/identity_repair.py`

`_classify_*` + `_apply_merge` / `apply_identity_repair` are the repair plan. **Keep.**

## Summary

These files are complexity, not dead code. Phase 19 does not split them and does not add a service layer.
