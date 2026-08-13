# Deploy Readiness Report — Phase 8

**Date:** 2026-08-13  
**Scope:** read-only audit of existing Deploy / Game config / UI before Phase 8 changes  
**Rule:** prefer reuse; do not rewrite `services/deploy.py` if sufficient

---

## A. What `services/deploy.py` already implements

`ModDeployer` is a complete facade:

| Action | Behavior |
|--------|----------|
| `deploy_mod` | Resolve Library source + `GameDeployConfig` → strategy → copy → write manifest → SQLite `deploy_status=deployed` |
| `undeploy_mod` | Load manifest → delete **only listed target files** → delete manifest → `not_deployed` |
| `redeploy_mod` | Undeploy (best-effort / hardened) then deploy |
| Gates today | Disabled Mod; folder absent; `is_missing_content`; game config missing; target path missing |
| Archive path | Optional extract-to-stage then deploy content root |
| Dependencies | Deploys declared dependency Mods first |
| Conflicts | `check_conflict_preview` — **warn only, does not block** |

Strategies (via `services/deploy_rules/`):

- `folder_copy` (generic) — **Phase 8 scope**
- `palworld_pak`, `anno_1800`, `slay_the_spire`, `custom_path` — already present; Phase 8 must not remove them

`folder_copy` conventions (keep):

1. Target = `mod_path / <Library folder name>` (not title / backup / display_name)
2. Skips `.info` / `info` / skipped scanner parts / `历史版本`
3. Does not modify Library payload (only writes `deploy_manifest.json` under Library `.info`)
4. Manifest records per-file `source` / `target` for safe undeploy

---

## B. Existing database fields

### `games` (already present + migrated)

| Field | Default | Role |
|-------|---------|------|
| `install_path` | `''` | Game install root |
| `mod_path` | `''` | Deploy target root |
| `deploy_type` | `folder_copy` | Strategy key |
| `workshop_path` | `''` | Workshop (sync), not Deploy core |

API: `get_game_deploy_config` / `update_game_deploy_config` → `GameDeployConfig`

### `mods` (deploy columns)

| Field | Values / notes |
|-------|----------------|
| `deploy_status` | `not_deployed` \| `deployed` \| `failed` |
| `deploy_path` | Last target path |
| `deploy_time` | ISO timestamp |
| `deploy_error` | Human-readable failure |
| `custom_deploy_path` | Per-mod absolute override |

**Missing vs Phase 8 status model:** no persisted `outdated` / `conflict` as `deploy_status`. Audit helper uses `consistent|missing|broken` (read-only), separate from UI deploy state.

---

## C. Existing UI entry points

| Surface | Status |
|---------|--------|
| Nav **「游戏部署」** → `GameDeployView` | Full game settings: install / mod / workshop / deploy_type, browse, save, test paths |
| Detail Panel | **部署 / 重新部署 / 取消部署** + deploy type / path / conflict hint labels |
| `library_view` | Wires Detail deploy signals → `DeployWorker` |
| ModCard | Shows deploy badge from `deploy_status` |
| Library sidebar | Game health from **Library folder** `game_status`, not `install_path` |

There is **no** separate inline 「游戏设置」 dialog inside Library; configuration already lives on the Game Deploy page.

---

## D. Real Library Mod file source

Source of truth for deploy content:

```
<library_root>/<GameFolder>/<ModFolder>/
```

Resolved by `ModFileManager.find_by_published_id(mod_id)` (and related metadata under `.info/`).

- Deploy copies from that managed folder (or archive-extracted stage).
- Library path is **never moved** by Deploy.
- `.info` is manager metadata; strategies ignore it for payload copy.

---

## E. Project-agreed deploy conventions (do not invent)

1. **`deploy_type` default = `folder_copy`**
2. Target folder name = **managed directory name**
3. Manifest = Library `.info/deploy_manifest.json` (existing; **reuse** — do not invent target `.modmanager/` as primary)
4. Undeploy = unlink manifest targets only (not blind `rmtree` of unknown trees)
5. Game paths are **user-configured** in DB (no disk-wide game discovery)
6. Multi-strategy games (Palworld / Anno / STS) already exist — Phase 8 closes the **folder_copy lifecycle**, does not strip other strategies

---

## Current capability summary

| Capability | Ready? |
|------------|--------|
| Game path config (install / mod / type) | Yes |
| folder_copy deploy / undeploy / redeploy | Yes |
| Skip `.info` on copy | Yes |
| Preserve Library files | Yes |
| Detail deploy buttons | Yes |
| Game Deploy settings page | Yes |
| Block `content_missing` / folder absent | Partial (folder + missing-content flag) |
| Block `backup_invalid` / `identity_conflict` | **No** |
| `deployment_status`: `outdated` | **No** |
| `deployment_status`: `conflict` + hard refuse overwrite | **No** (detect-only) |
| `install_path` missing → Game warning (not Mod `folder_missing`) | **No** (Library-folder `missing_folder` only) |
| Phase 8 Chinese error strings | Partial / inconsistent |
| Lifecycle test suite `test_deploy_lifecycle.py` | **No** |

---

## Gaps (Phase 8 must close)

1. **Health gate** — refuse deploy when `content_status` ∈ `{folder_missing, backup_invalid, identity_conflict}`; keep existing `content_missing` rule.
2. **Runtime deployment status** — compute `not_deployed | deployed | outdated | conflict` without stuffing into `content_status`. Persist only when necessary; keep legacy `failed`.
3. **Outdated** — after successful deploy, if Library deployable content changes vs last deploy fingerprint → `outdated`; Redeploy restores `deployed`.
4. **Conflict hard-block** — if target folder/files exist and are not owned by this Mod’s manifest → refuse with clear error; status `conflict`.
5. **install_path warning** — if configured `install_path` does not exist, surface Game-level warning; do not mark Mods `folder_missing`.
6. **Library 「游戏设置」 entry** — minimal: open / focus existing Game Deploy page for current game (reuse `GameDeployView`, no second form).
7. **Error normalization** — map to Phase 8 messages where applicable.
8. **Tests** — `tests/test_deploy_lifecycle.py` (Cases 1–10 + real temp dirs).

---

## Minimal modification plan

**Do not rewrite** `ModDeployer` or strategies. Incremental only:

| Area | Change |
|------|--------|
| `services/deploy_status.py` (new, small) | Compute `deployment_status` + content fingerprint; map audit/ownership |
| `services/deploy.py` | Gate on `content_status`; block foreign target; set fingerprint on deploy; normalize errors |
| `services/deploy_rules/manifest.py` | Optional `content_fingerprint` / `source_path` fields (backward compatible) |
| `core/db_manager.py` | Add `outdated` / `conflict` constants if persisted; no column drops |
| `services/game_status.py` / library header | `install_path` missing → warning text |
| `ui/library_view.py` + Detail | 「游戏设置」 jump; show outdated / disable deploy on bad content_status |
| `ui/game_deploy_view.py` | Optional: select game by app_id when navigated from Library |
| `tests/test_deploy_lifecycle.py` | New lifecycle coverage |

**Explicit non-goals (Phase 8):**

- Move manifest to game `.modmanager/` (reuse Library `.info` manifest)
- Symlink / load order / priority
- Rewrite Resolver / Backup / Identity / Import / Reconcile
- Remove Palworld/Anno/STS strategies

---

## Verdict

**Existing deploy stack is sufficient as the base.** Phase 8 is a **lifecycle closure** (status + gates + conflict policy + Game install warning + tests), not a greenfield Deploy rewrite.
