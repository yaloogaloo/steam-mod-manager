# Visible-child lifecycle and user-owned cover

Two invariants. They are not style tips. Breaking either is a user-visible
regression.

## 1. Visible child QWidget detach

A `QWidget` that is still `isVisible()` and then receives `setParent(None)`
becomes a **top-level window** (HWND on Windows). That steals focus from
`MainWindow` for a few hundred milliseconds. It is not a `QMessageBox`.

The Import-Mod ownership guard (`show` / `setVisible`) does **not** catch this.
New Detail rebuilds (`show_mod` → relationship list replace) reintroduced it
because `DependencyListHost.set_items` copied `takeAt` + `setParent(None)`
without `hide()`.

Contract:

```
visible child
    → hide()
    → setParent(None)
    → deleteLater()
```

Use `ui.window_lifecycle.detach_owned_widget`. Do not re-learn "remember to hide".

Allowed `setParent(None)` without this sequence: `QObject` / `QThread` workers
(not widgets). Do not globally ban `setParent` or `show()`.

Success callbacks (`cover` / `refresh` / `offline save`) must not create
unrequested top-level UI. Status belongs on the Detail op-status line.

`show_mod` is the canonical Detail synchronization for one logical mutation:
data load plus UI rebuild (including relationships). It is not a generic
hammer. Library projection (`notify_mod_changed`) updates cards. While
`_canonical_detail_sync` is active, that flush records the id in
`_projection_skip_detail` and must not call `show_mod` again.

Cover change, metadata refresh, and offline save each call
`_reload_current_detail_from_projection` once inside that block.
`metadata_saved` / `tags_saved` / `offline_page_updated` still refresh
projection. They do not rebuild Detail a second time.

## 2. User-owned cover

`USER-SET COVER` ≠ `REFRESH-FETCHED COVER`.

`apply_cover_to_mod` copies the file into `.info/cover.<ext>`, writes
`mods.cover_path`, and sets `user_override_fields.cover`.

Generic metadata refresh / local sidecar rescan must **not** replace that with:

- a stale `metadata.json` `cover_path` (often an absolute path into another tree)
- an official Steam/Mod.io preview
- Backup / placeholder / 1×1 diagnostic images

`services.file_ops._build_unified_payload` must not clobber an in-memory
`cover_path` with a DB/sidecar snapshot taken **before** the cover row is
updated.

`apply_sidecar_to_db` must not write `cover_path` when the user cover override
is set.

Official cover download (`should_apply_official_field(FIELD_COVER)`) is allowed
only when there is no override **and** no local cover value **and** no live
`.info/cover.*` / `preview.*` file.

Domains for the single pointer `mods.cover_path` (not a second cover id):

- USER OWNED when `user_override_fields.cover` is set
- OFFICIAL / REFRESH OWNED when there is no override and no live cover
- DERIVED / CACHE is the decoded image cache only

An absolute `cover_path` outside the managed folder is foreign.
`cover_reference_is_foreign` rejects it. A relative `.info/cover.ext`, or an
absolute path inside that folder, is a valid managed reference.

Save-webpage success does not launch Explorer. `os.startfile` /
`QDesktopServices.openUrl` belong to the separate Open Folder / Open Offline
actions. A `CabinetWClass` titled `.info` during a capture is an external
shell window, not this success path.

## 3. What not to do

- Sleep / timers to hide the flash window
- Swap a blue placeholder for another placeholder and call cover "fixed"
- Disable all `QMessageBox` / `QDialog` / `show()` / `setParent(None)`
- Treat `mods.cover_path` pointing at a missing foreign file as "the cover"
- Add a second `show_mod` on `metadata_saved`, `tags_saved`, or
  `offline_page_updated` after a canonical reload

## 4. Test / forensic data versus production data

Cover mutation, metadata mutation, backup mutation, and deploy mutation are
production mutations. They call `services.mutation_context.assert_mutation_allowed`.

```
TEST / FORENSIC CODE
    MUST HAVE EXPLICIT TEMP DATA ROOT

PRODUCTION MOD MUTATION
    MUST REQUIRE EXPLICIT PRODUCTION CONTEXT

NO TEST FALLBACK TO CURRENT LIBRARY

NO DIAGNOSTIC FALLBACK TO REAL MOD
```

`activate_test_context(roots)` allows writes only under those roots.
`activate_production_context()` is called from `main._run_gui` only.
If neither is active, the write is refused. A diagnostic must not select a
card from the real library or call `default_mod_library()` when its fixture
root is missing.

## 5. Regression gate

Any edit under `show_mod`, `notify_mod_changed`, `set_items`, `setParent`,
`cover_path`, `_build_unified_payload`, or `apply_sidecar_to_db` must run:

```
python -m pytest tests/test_success_path_ui_invariants.py tests/test_cover_ownership_invariants.py tests/test_widget_detach_lifecycle.py tests/test_ui_lifecycle_static_guard.py
```

That command is the UI SAFETY / COVER OWNERSHIP REGRESSION GATE.
The same rule is in `AGENTS.md` so it is not Cursor-only.

