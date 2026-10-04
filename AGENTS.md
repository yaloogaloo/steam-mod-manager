# Agent / contributor guardrails

These invariants are enforced by tests, not by the editor you use.

## UI SAFETY / COVER OWNERSHIP REGRESSION GATE

If you touch any of:

- `ui/mod_detail_panel.py` (`show_mod`, success handlers)
- `ui/dependency_item_widget.py` (`set_items`)
- `ui/window_lifecycle.py` (`setParent`, `detach_owned_widget`)
- `ui/library_view.py` (`notify_mod_changed` flush)
- `services/file_ops.py` (`_build_unified_payload`, `cover_path`)
- `services/info_sidecar.py` (`apply_sidecar_to_db`)
- `services/metadata_refresh.py`

run:

```
python -m pytest tests/test_success_path_ui_invariants.py tests/test_cover_ownership_invariants.py tests/test_widget_detach_lifecycle.py tests/test_ui_lifecycle_static_guard.py
```

Do not treat the task as done if that gate fails.

## CANONICAL MEMBERSHIP REGRESSION GATE

Deployed Filter and Sorting Mode both call ``entry_is_deployed`` on the
library snapshot (``mods.deploy_status = deployed``). Order files only
sequence that set.
Launcher descriptors, playsets, ``dlc_load.json``, ``used_mods.txt``, pak
UUIDs, and ``modsettings.lsx`` are external projection: unresolved must not
hide a deployed Mod.

If you touch any of:

- `services/deploy.py`
- `services/order_backend.py`
- `services/canonical_membership.py`
- `services/paradox_activation.py`
- `services/bg3_activation.py`
- `services/wh3_activation.py`
- `ui/library_view.py`
- `ui/library_query.py`

run:

```
python -m pytest tests/test_canonical_membership_contract.py tests/test_canonical_membership_static_guard.py tests/test_canonical_identity_contract.py tests/test_canonical_identity_static_guard.py tests/test_ck3_sort_mode_ui.py tests/test_stellaris_activation.py tests/test_bg3_sort_mode_ui.py tests/test_wh3_activation.py
```

Do not treat the task as done if that gate fails. A non-empty sort list, a
changed order file, or a successful drag is not this contract.

## Invariants

VISIBLE CHILD LIFECYCLE: a visible child is `hide` → detach → `deleteLater`
via `detach_owned_widget`. `setParent(None)` on a visible `QWidget` maps a
top-level window.

USER COVER OWNERSHIP: `user_override_fields.cover` means generic refresh,
official sync, and sidecar rescan must not change `mods.cover_path` or the
live cover bytes. A foreign absolute path (another tree) must not be stored.

SUCCESS PATH UI: cover upload, cover refresh, metadata refresh, and offline
save must not open an unsolicited modal, top-level widget, or Explorer window.
Open Folder and Open Offline are separate explicit actions.

DETAIL SYNC: one logical mutation → one `show_mod` (`_canonical_detail_sync`).
Projection flush updates cards and must not rebuild Detail again for that id.

## Production mutation boundary

Cover, metadata, backup, and deploy writes go through
`services.mutation_context.assert_mutation_allowed`.

- Test / forensic code must activate an explicit temporary root
  (`activate_test_context`). Every target path has to sit under that root.
- Production writes are allowed only after `activate_production_context()`
  in the application entry point (`main._run_gui`). A pytest process
  (`PYTEST_CURRENT_TEST` or `SMM_TEST_DB`) cannot activate that context.
- No context means refuse. Do not fall back to the current library, the
  first visible card, or a drive-letter prefix.

A diagnostic that opens the real `MainWindow` against `data/mod_manager.db`
and clicks cover change is a production-data bug, not a test convenience.

Longer notes: `docs/architecture/UI_LIFECYCLE_AND_COVER_OWNERSHIP.md`.
