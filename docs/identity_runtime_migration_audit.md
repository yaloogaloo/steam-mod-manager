# Identity Runtime Migration Audit (Phase 0)

Date: 2026-09-15  
Status: **Phase 0 complete — stop. No code changes in this phase.**

Target model for later phases:

```
Workspace ID          user interaction
Internal ID (UUID)    business entity
mod_id (SQLite PK)    SQL query / FK only
```

This document is a **migration inventory**. It lists current chains and leak sites so Phase 1+ can land without guessing. It does not propose a new identity model.

Related prior report: `docs/identity_runtime_boundary_audit.md` (compliance score 48/100). This file is the work list.

---

## Correct vs wrong (must keep this distinction)

### Correct (already true in SQLite / sidecar)

| Location | Field | Actual type |
|---|---|---|
| `mods.internal_id` | TEXT | Frozen UUID (when minted) |
| `mods.mod_id` | INTEGER | SQLite PK |
| `mods.workspace_id` | TEXT | user Workspace ID |
| `.info/metadata.json` `internal_id` (`services/info_sidecar.py` ~83) | TEXT | Frozen UUID (same entity) |
| `core/db_manager.py` ~3403 (path-audit / backup-row style SELECT) | `"internal_id"` | Frozen UUID from column |

### Wrong (runtime / DTO / API)

| Location | Field / API | Actual type |
|---|---|---|
| `list_mod_list_items` ~3267 | `"internal_id": mid` | **mod_id PK** |
| `ModListItem.internal_id` | field | **PK** |
| `ModCardData.id` / `.internal_id` / `.mod_id` | aliases | **PK** |
| `ModMetadata.internal_id` + `entity_internal_id()` | DTO | **PK** |
| `DeployContext.internal_id` | context | **PK** |
| `DeployFilePlan.internal_id` | plan | **PK** |
| `backup_root(mod_id)` | path | **`data/mod_backup/<PK>/`** |
| `allocate_internal_id()` | function | returns **PK** |
| `resolve_internal_id_from_workspace_id()` | function | returns **PK** |
| `[DEPLOY] internal_id=` logs | log | value is **PK** |

---

# 1. Current UI → Service identity chains

All production UI operation tokens are **digit `mods.mod_id`**, except: detail **display/copy** of `workspace_id`, and the dependency **input box** (Workspace ID text).

## 1.1 Library card → any action

```
list_mod_list_items
  "internal_id": str(row["mod_id"])          # WRONG name
        │
ModListItem.internal_id = PK
        │
list_item_to_card_data: ModCardData.id = item.internal_id
        │
ModCardWidget._mod_id() → data.id            # PK
        │
Signals (all PK):
  selection_requested
  detail_requested
  deploy_requested
  edit_requested
  open_folder_requested
  open_steam_requested
  favorite_toggle_requested
```

Entry points:

| UI | Line (approx) | Token | Service |
|---|---|---|---|
| `ui/mod_card.py` | 463–650, 726 | PK | LibraryView slots |
| `ui/library_view.py` | 3962–3976, 4067 | PK, `isdigit()` gate | `DeployWorker` |
| `ui/deploy_thread.py` | 37–82 | field `mod_id` = PK | `ModDeployer.deploy_mod/undeploy/redeploy` |

## 1.2 Detail panel → services / DAL

`current_mod_id()` (`ui/mod_detail_panel.py` 5405–5422) returns **digit PK** from `ModDisplayInfo.mod_id` (or digit `resolved.internal_id` / `entity_internal_id()`).

Used for (non-exhaustive; 40+ call sites in the same file):

| Action | Token | Downstream |
|---|---|---|
| Deploy / redeploy / undeploy | PK | `deploy_requested.emit` → Worker |
| Metadata refresh | PK + `isdigit()` | `ui/metadata_refresh_thread.py` |
| Favorite, tags, type, enable | PK + `isdigit()` | `get_db()` mutators |
| Cover / offline / files | PK | `services/info_asset_runtime`, cover loader |
| Edit info dialog | PK | `get_mod_display_info(mid)` |
| Add dependency | owner = PK; user types Workspace ID | `add_dependency_by_workspace_id` |
| Collection membership | PK | `services/collection` via `resolve_mod_pk` |

Display/copy (correct): `view_id` / copy buttons use `workspace_id` (~2556–2575). Debug line appends `"Internal Database ID: {mod_id PK}"` (~2578–2589) — **wrong label**.

## 1.3 Other UI → Service

| UI | Token | Service |
|---|---|---|
| `ui/mod_edit_dialog.py` | `self.mod_id` PK | `get_mod_display_info`, `update_mod_user_metadata` |
| `ui/mod_detail_dialog.py` | digit PK into `meta.internal_id` | resolver / `get_mod_display_info` |
| `ui/mod_picker_dialog.py` | `selected_mod_id()` | relationship picker (PK) |
| `ui/collection_membership_dialog.py` | member ids from collection API | `compute_membership_edits` — FK PKs |
| `ui/collection_cover_dialog.py` | `choice.internal_id` | cover choice; name Internal, value typically PK |
| `ui/library_query.py` | `normalize_record_mod_id` + `isdigit` | projection index keys = PK |
| `ui/game_deploy_view.py` | `isdigit` on app/game ids | **game App ID**, not Mod identity (do not migrate as Mod PK) |

## 1.4 Chain summary

```
User copies:     workspace_id                    CORRECT
User clicks:     PK via card/detail              WRONG
UI isdigit():    rejects Frozen UUID             WRONG
Service public:  named internal_id, value PK     WRONG
DAL:             WHERE mod_id = ?                CORRECT (SQL)
```

---

# 2. All `internal_id` fields — actual runtime type

Legend: **UUID** = Frozen `mods.internal_id`; **PK** = `mods.mod_id`; **mixed** = UUID or PK depending on caller.

## 2.1 Database / disk (correct UUID when populated)

| Site | Type |
|---|---|
| `mods.internal_id` column | UUID |
| `.info/internal_id` file / sidecar `InfoSidecar.internal_id` | UUID |
| `ensure_durable_internal_id` write | UUID |
| `find_mod_by_internal_id` **input** | UUID |
| `mod_identity_authority.internal_id` comment | UUID (intent) |

## 2.2 DTOs / cache / session (wrong PK)

| Site | File | Type |
|---|---|---|
| Layer-1 list dict `"internal_id"` | `core/db_manager.py` 3267 | **PK** |
| `ModListItem.internal_id` | `services/mod_list_item.py` 77 | **PK** |
| `ModCardData.id` / `.internal_id` / `.mod_id` | `services/mod_library_cache.py` 51, 93–99 | **PK** |
| `ModMetadata.internal_id` | `core/models.py` 149 | **PK** (stubs empty) |
| `entity_internal_id()` | `core/models.py` 151–159 | **PK** |
| `ModDisplayInfo.mod_id` | `core/db_manager.py` 451 | **PK** (honest name) |
| `fetch_mod_list_item(internal_id)` | `mod_library_cache.py` 308–327 | mixed in, session key **PK** out |
| Library selection / `_card_for_mod_id` | `ui/library_view.py` | **PK** |

## 2.3 Deploy / FilePlan / lock / result (wrong PK)

| Site | File | Type |
|---|---|---|
| `deploy_mod(internal_id)` after resolve | `services/deploy.py` 1572+ | **PK** |
| `DeployContext.internal_id` | `deploy_rules/base.py` 60; assigned `deploy.py` 1344 | **PK** |
| `DeployFilePlan.internal_id` | `deploy_file_plan.py` 90, 238 | **PK** |
| `DeployResult.internal_id` | `deploy_result.py` 24 | **PK** |
| `deploy_timing_session(internal_id=)` | `deploy_stage_log.py` | **PK** |
| `deploy_operation_lock(mid)` | `deploy_lock.py` | **PK** |
| `BackupManager.internal_id` / `storage_key()` | `backup_manager.py` 178–185 | **PK** (deploy overwrite backups under `data/deploy_backup/<key>/`) |
| WH3 `ref.internal_id` | `wh3_activation.py` | **PK** (typical) |
| `deploy_path_audit` dataclass `internal_id` | comment: “mods.mod_id PK as string” | **PK** (honest comment, wrong name) |

## 2.4 Identity service (mixed / wrong)

| Function | Claimed | Actual |
|---|---|---|
| `resolve_mod_pk(internal_id)` | Frozen → PK | UUID **or digit PK** → PK |
| `resolve_deploy_identity` | Frozen → PK | same |
| `resolve_entity_internal_id` | Internal | **PK** |
| `resolve_internal_id_from_workspace_id` | Workspace → Internal | Workspace scoped → **PK** |
| `allocate_internal_id()` | Internal | **INTEGER PK** |
| `find_mod_by_internal_id` | UUID in | **PK** out (DAL OK) |

---

# 3. `mod_id` leak inventory (outside DAL)

SQL `WHERE mod_id` / FK columns in `core/db_manager.py` are **in scope to keep**. Below are leaks into UI, public service APIs, paths, and logs.

## 3.1 UI (must leave in Phase 1)

- `ModCardData.id` = PK
- Entire `LibraryView` card index / selection / `_card_for_mod_id` / `_row_mod_id` / `normalize_record_mod_id`
- `current_mod_id()` + dozens of `isdigit()` gates in `mod_detail_panel.py`
- `DeployWorker.mod_id`
- `LibraryView._on_deploy_action` `if not mid.isdigit()`
- `metadata_refresh_thread` `if not mid.isdigit()`
- Projection records `index.mod_id`

## 3.2 Service public APIs (Phases 2–7)

- `ModDeployer.deploy_mod` / `undeploy_mod` / `redeploy_mod` — accept and then **are** PK
- `collection.add_mod_to_collection(internal_id)` — resolve to PK; UI passes PK
- `mod_relationships.add_dependency_by_workspace_id(owner_internal_id)` — owner must `.isdigit()`
- `mod_library_cache.get_card_data` / `invalidate` / `refresh_projection` — session key PK
- `notify_mod_changed(internal_id)` — typically PK
- `mod_presence` / `metadata_backup_sync` — `backup_root(mid)` with PK
- `path_lifecycle.resolve_mod_folder_by_internal_id` — digit token treated as PK (~182–185)

## 3.3 Persistence paths (Phase 5)

- `data/mod_backup/<mod_id>/` via `backup_root()`
- Deploy overwrite backups `data/deploy_backup/<BackupManager.internal_id>/` when that field is PK
- Timing file is under managed `.info/` (path is library folder, not PK) — identity **inside JSON** may still log PK

## 3.4 Logs (Phase 8)

- `[DEPLOY] internal_id={PK}`
- `[DEPLOY_FAILED] internal_id=`
- `[MOD_REFRESH_FAILED] mod_id=` (honest PK name in some logs — still a leak if PK must not appear in business logs)

---

# 4. Deploy / FilePlan / Backup identity use

## 4.1 Deploy

| Step | File | Identity used |
|---|---|---|
| UI click | `mod_card` / `mod_detail_panel` | PK |
| Worker | `deploy_thread.py` | PK as `self.mod_id` |
| Entry | `deploy.py` `deploy_mod` | resolve → **PK** `mid` |
| Context | `DeployContext(internal_id=mid, workspace_id=workspace_id)` | `internal_id`=PK; `workspace_id`=display/registration (WH3/Duckov still read it) |
| Plan | `strategy.plan(ctx)` | folder name from library path + capability; **not** PK as folder (after Civ6 cleanup) |
| FilePlan | `internal_id=ctx.internal_id` | PK |
| Apply/verify | file list only | no extra identity |
| Persist status | `update_mod_deploy_status(ctx.internal_id, …)` | SQL by PK — OK **if** `ctx.internal_id` were mapped at DAL; today the business field **is** the PK |
| Lock / timing | `deploy_lock`, `deploy_stage_log` | PK labeled `internal_id` |

`workspace_id` on context is **not** the Deploy entry key. It is copied from display/registration for game rules.

## 4.2 FilePlan

| Field | Today | Target |
|---|---|---|
| `DeployFilePlan.internal_id` | PK | Frozen UUID |
| Manifest `mod_id=` from plan (`deploy_file_plan.py` ~312) | same PK string | SQL/manifest key policy is **out of this task** (Manifest not to be redesigned); do not conflate with runtime FilePlan business field |

Phase 4: split `internal_id` (UUID) vs `mod_pk` (SQL only) on the plan object. Do not change Manifest schema in this program.

## 4.3 Backup (two trees)

### A. Metadata backup (user-data survival)

| | |
|---|---|
| API | `services/metadata_backup.py` `backup_root(mod_id)` |
| Path | `data/mod_backup/<mods.mod_id>/` |
| Prove | `prove_backup_storage_key` → current PK |
| Callers | `mod_presence.py`, `metadata_backup_sync.py`, `metadata_backup_validator.py`, `info_asset_runtime.py`, `mod_metadata_resolver.py`, `backup_asset_migration.py` |
| Target | `data/mod_backup/<internal_id UUID>/` |
| Compat | read old PK dir; migrate to UUID dir; do not delete old in one shot |

### B. Deploy overwrite BackupManager

| | |
|---|---|
| API | `BackupManager(managed, internal_id=ctx.internal_id)` |
| Path | `data/deploy_backup/<storage_key>/` |
| Today key | PK (because `ctx.internal_id` is PK) |
| Target | UUID once context is fixed |

Phase 5 text in the task refers to **A** (`data/mod_backup/`). **B** must follow the same identity or it remains a PK leak.

---

# 5. Resolver map (for Phase 2 new boundary)

Do **not** use `resolve_mod_pk()` as the **business** entry after Phase 2.

| Current | Input | Output | Phase 2+ |
|---|---|---|---|
| `resolve_mod_pk` | UUID or PK | PK | rename/split: `resolve_mod_pk_from_internal_id(UUID)→PK` only; **no digit PK passthrough** |
| `resolve_deploy_identity` | same | PK | Deploy must first have UUID |
| `resolve_internal_id_from_workspace_id` | ws+platform+app_id | **PK** | must return **UUID** (`mods.internal_id`) |
| `resolve_entity_internal_id` | mixed | PK | replace with `resolve_internal_id()` → UUID |
| `find_mod_by_internal_id` | UUID | PK | keep as **DAL** |
| `find_mod_by_workspace_id` | — | always None | keep (not an entity key) |
| `find_mod_for_registration` | platform+app_id+ws | PK | keep as **DAL/registration**; UI must not call it as session key |
| `allocate_internal_id` | — | **PK** | mint **UUID** into `mods.internal_id`; PK stays autoincrement |

New boundary (task):

```
resolve_internal_id(workspace_id | UUID) → UUID
resolve_mod_pk_from_internal_id(UUID) → PK     # DAL only
```

---

# 6. Phase mapping (do not start until Phase 0 accepted)

| Phase | Goal | Primary files |
|---|---|---|
| 1 UI | DTO carries UUID + workspace_id; no PK in signals | `db_manager.list_mod_list_items`, `mod_list_item`, `mod_library_cache`, `mod_card`, `mod_detail_panel`, `library_view`, `deploy_thread` |
| 2 Deploy entry | `deploy_mod(UUID)`; optional workspace resolve; reject PK | `deploy.py`, `deploy_thread.py`, `identity_service.py`, `library_view` isdigit |
| 3 Context | `internal_id=UUID`, add `mod_pk` for SQL | `deploy_rules/base.py`, `deploy.py` `_resolve_context` |
| 4 FilePlan | UUID + optional `mod_pk` | `deploy_file_plan.py` |
| 5 Backup | UUID directory + old PK read/migrate | `metadata_backup.py` + callers |
| 6 Naming | `allocate_internal_id`, `entity_internal_id`, workspace resolver | `identity_service.py`, `core/models.py` |
| 7 PK scope | public services stop taking PK | collection/relationships/cache/presence — **no Collection table schema change** |
| 8 Docs/tests | strip “internal_id = PK”; add `tests/test_identity_runtime_boundary.py` | comments, logs, tests |

Out of scope (task): Asset Store, Manifest schema, Collection **table** structure, Deploy strategies, WH3/Stellaris/Civ6 capability, folder-name normalizer.

---

# 7. Risks if Phase 1 starts without this list

1. `isdigit()` is the load-bearing gate for **every** detail mutation, not only Deploy. Replacing it with UUID without updating `get_mod(mid)` callers will break edit/favorite/tags.
2. Tests construct `DeployContext(internal_id='1')` and `backup_root(pk)` extensively — Phase 2–5 will fail a large pytest surface unless tests switch to UUID.
3. Steam historical coincidence: PK digits can equal Workspace digits. Rejecting PK at `deploy_mod` is required so Workshop numbers cannot be mistaken for entities (already true for resolve-by-workspace; UI currently **is** PK so coincidence is how some tests call `deploy_mod("289070101")`).
4. Layer-1 `"internal_id"` key is consumed as cache/session identity. Changing it to UUID without updating `fetch_mod_list_item` / projection events will desync the library.

---

# 8. Phase 0 stop

No production code, tests, or identity implementation was modified in this phase.

Next authorized step: **Phase 1 UI identity boundary**, using the inventories in sections 1–3.
