# Deploy System — Production Audit

**Date:** 2026-08-07  
**Scope:** read-only review of `ModDeployer` + `deploy_rules` (no code changes in this document)  
**Strategies:** `folder_copy`, `palworld_pak`

---

## 1. Current flow

### 1.1 `deploy_mod(mod_id)`

```
resolve source + app_id + GameDeployConfig
  → get_strategy(deploy_type)
  → strategy.deploy(ctx)          # copies files, builds DeployManifest in memory
  → save_manifest(.info/deploy_manifest.json)
  → SQLite: deploy_status=deployed, deploy_path, deploy_time
```

On failure: `deploy_status=failed` (path/time cleared). Error text is logged but **not persisted** in a dedicated column (pre–Phase 5).

### 1.2 `undeploy_mod(mod_id)`

```
resolve context
  → load_manifest(source)
  → strategy.undeploy(ctx, manifest)   # unlink each manifest.target only
  → delete_manifest
  → SQLite: not_deployed, clear path/time
```

Empty parent directories may be pruned upward, stopping at `mod_path` / `Paks` root — **never** deletes those roots.

### 1.3 `redeploy_mod(mod_id)` (as of upgrade)

```
undeploy_mod (best-effort)
  → if source missing: abort
  → else: deploy_mod
```

Intended semantics: remove old manifest targets, then copy current sources and write a new manifest.

---

## 2. Audit checklist

| # | Question | Finding |
|---|----------|---------|
| 1 | Does a **successful** deploy always write a complete manifest? | **Yes**, if `strategy.deploy` returns `success` with `result.manifest`. Facade asserts and `save_manifest` before marking `deployed`. If manifest write fails after copy, status → `failed` (orphan files possible). |
| 2 | Does manifest cover **generic** and **palworld**? | **Yes**. Both strategies build `DeployManifest` with every copied file (`source`/`target`). |
| 3 | Does undeploy delete **only** manifest targets? | **Yes**. Loops `manifest.files` and `unlink`s files only; no `rmtree` of deploy roots. Missing/empty manifest → no file deletes (status may still clear). |
| 4 | Whole-directory / other-mod / blind overwrite risks? | See §3. |

---

## 3. Risk points

### R1 — Redeploy vs. silent overwrite (medium → addressed in Phase 2)

`redeploy_mod` already sequences undeploy → deploy. Residual risk if undeploy fails partially (`success=False`) but redeploy still continues deploy (except when source is missing). Partial undeploy + deploy can leave **orphan targets** that are no longer in the new manifest.

**Recommendation:** Treat non-success undeploy as hard failure for redeploy (except “no manifest” / empty files, which is OK). Add regression test for shrinking file sets (A.pak+B.pak → A.pak).

### R2 — Copy overwrites unknown files (medium)

- **folder_copy:** `copytree(..., dirs_exist_ok=True)` overwrites files under `mod_path/<folder>/`.
- **palworld_pak:** `shutil.copy2` into shared `Paks/` / `~mods/` by **filename** — two Mods shipping `Cool.pak` will silently overwrite each other.

**Recommendation:** Pre-deploy conflict scan (Phase 6) — detect only, do not auto-resolve.

### R3 — Orphan files if DB commit fails after copy (low–medium)

Order is: copy → save manifest → update DB. If DB update fails, files + manifest exist but status is not `deployed`. Audit (Phase 3) should flag `broken` / inconsistent.

**Recommendation:** Prefer recording `deploy_error`; optional later: transactional “intent” row (out of scope).

### R4 — Status vs. reality drift (medium)

DB can show `deployed` while:

- managed Mod folder was deleted,
- `deploy_manifest.json` missing,
- listed target files deleted externally.

**Recommendation:** `audit_deploy_state` + startup scan of `deployed` rows only (Phases 3–4). Never auto-delete or auto-redeploy.

### R5 — Failed status loses previous deploy metadata (low)

`_mark_failed` clears `deploy_path` / `deploy_time`. After a failed redeploy, UI may not show last good path.

**Recommendation:** Persist `deploy_error`; keep last path optional (Phase 5 focuses on error text).

### R6 — Undeploy without manifest while status=`deployed` (medium)

Undeploy with missing manifest succeeds with **zero** deletes, then clears DB. Target files remain (orphans). Safer for “don’t delete unknown files,” worse for cleanup.

**Recommendation:** Audit marks `broken`; UI prompts manual cleanup. Do not invent deletes.

### R7 — `folder_copy` target folder name = source folder name (low)

Rename of managed folder between deploys changes target path; old tree may remain if undeploy used old manifest (OK if manifest had absolute targets). Absolute `target` paths in manifest mitigate this.

### R8 — Pruning empty dirs (low)

`remove_empty_parents` is bounded by `stop_at`. Correct if `stop_at` resolves; if install path wrong, prune may no-op (safe) or skip.

---

## 4. Modification suggestions (implemented in later phases)

| Phase | Action |
|-------|--------|
| 2 | Harden `redeploy_mod`: require successful undeploy (or empty/missing manifest); add `tests/test_redeploy_cleanup.py`. |
| 3 | Add `services/deploy_audit.py` → `audit_deploy_state()` (consistent / missing / broken). |
| 4 | On app start, scan mods with `deploy_status=deployed`; surface anomalies in UI; no auto-fix. |
| 5 | Add `mods.deploy_error`; store human-readable failure; show in DetailPanel. |
| 6 | Add `services/deploy_conflict.py` — detect overlapping targets owned by other manifests; warn only. |
| 7 | Consolidate tests in `tests/test_deploy_audit.py`. |

---

## 5. Safety conclusions (pre-hardening)

| Behavior | Safe for users? |
|----------|-----------------|
| Undeploy only manifest paths | Yes |
| Never `rmtree` game `Paks` / `mod_path` root | Yes |
| Successful deploy writes full manifest (both strategies) | Yes |
| Redeploy cleans removed files | Yes if undeploy fully succeeds; needs hard fail on partial undeploy |
| Cross-mod pak name collisions | Detect-only needed |
| DB/filesystem consistency | Needs audit + startup scan |

**Verdict:** Core undeploy model is sound for production. Hardening should focus on **redeploy completeness**, **status audit**, **persisted errors**, and **conflict warnings** — not a redesign of strategies.
