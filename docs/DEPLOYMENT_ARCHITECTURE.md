# Deployment Architecture Contract

**Status:** Phase 4 contract / architecture lock complete.  
**Implementation:** Phase 2 Core + Phase 3 Strategy adapters + Phase 4 code-level contracts + guards.  
Strategies are path-mapping only (`plan()` → FilePlan); `deploy()` is an inert shell.  
I/O lives in `deploy_apply.py` (sole Deploy `ArchiveExtractor` caller).  
Architecture guards: `tests/test_deploy_architecture_guards.py`.  
Contract tests: `tests/test_deploy_phase4_contracts.py`.  

**Code-level contracts** (read these even without this doc):

- `services/deploy_rules/base.py` — Strategy = path-mapping adapter
- `services/deploy_file_plan.py` — FilePlan.files = single source of truth
- `services/deploy_apply.py` — sole Deploy filesystem mutation / extract ownership
- `services/deploy_verifier.py` — Verify = success authority
- `services/deploy.py` (`ModDeployer`) — Core pipeline ownership

**Do not claim “Deploy solved” until ARCHITECTURE PASS + TEST PASS + REAL PRODUCTION DEPLOY PASS (Phase 5).**

Future AI / developers modifying Deploy **must** inspect the code-level contracts and architecture guards before changing deployment behavior. Docs alone are not sufficient.

This document is the long-term Deploy specification. New work on Deploy must follow it. New AI / contributors must read this file first, then the modules listed in §14.

Related (historical, not authoritative for the target design):

- `docs/DEPLOY_PRODUCTION_AUDIT.md` — older production notes (2026-08-07)
- Prior forensic finding: Anno archive success via `after − before` filesystem diff is **forbidden** under this contract

---

## 1. Deploy unique entry

**Only public orchestration APIs** (names may evolve; semantics must not):

| API | Purpose |
|-----|---------|
| `DeployManager.deploy(mod_id)` | Deploy one Mod |
| `DeployManager.undeploy(mod_id)` | Remove only FilePlan / manifest targets |
| `DeployManager.redeploy(mod_id)` | Undeploy then deploy |
| `DeployManager.status(mod_id)` | Runtime deployment status (not content_status) |

Today these live on `ModDeployer` in `services/deploy.py`. Phase 2 may rename the facade to `DeployManager` but must preserve a single entry.

**Callers allowed:**

- `ui/deploy_thread.py` (UI must not run Deploy on the UI thread)
- `services/mod_remove.py` (best-effort undeploy before delete)
- `services/library_reconcile.py` (**only** stale transaction recovery — never redeploy)
- `tools/deploy_smoke_runner.py` and contract tests

**Forbidden:** any other module implementing extract / copy / manifest write / “deploy success” accounting.

---

## 2. Deploy core module

Canonical pipeline (one pipeline, all Mod kinds):

```text
DeployManager
    ↓
Resolve Source          (identity → managed path → source unit)
    ↓
Build FilePlan          (strategy contributes path mapping only)
    ↓
Backup                  (BackupManager over FilePlan targets)
    ↓
Apply FilePlan          (copy / extract_member — core owned)
    ↓
Verify                  (every required plan entry exists at target)
    ↓
Persist Manifest / Status
```

Stage timing (`deploy_stage_log`) must wrap real work. If extraction runs inside Apply, it must be reflected in diagnostics (never silent `extract_elapsed_ms=0` while extract actually ran).

---

## 3. Source Contract

Deploy core owns source discovery and materialization.

| Source kind | Authority |
|-------------|-----------|
| `folder` | Enumerate files under content root (respect ignore / allow-list) |
| `zip` | Enumerate archive members (no Strategy extract) |
| `rar` / `7z` | Same as zip via single extract primitive |

**Rules:**

1. Archive discovery is core-owned (`collect_deploy_archives` / successor) — Strategies must not import deploy helpers to re-discover archives.
2. **One** extract primitive: `ArchiveExtractor` (wrapping `importers.archive.extract_archive`). Strategies must not call extractors.
3. For archives, FilePlan is built from **archive entry enumeration** (or from a single staging extract owned by core), not from a post-hoc game-tree diff.
4. Folder and archive share the same FilePlan → Backup → Apply → Verify chain.

---

## 4. Target Contract

```text
current_target_root(root_kind, GameDeployConfig, custom_deploy_path)
  + target_relative
  = target_absolute
```

| Rule | Requirement |
|------|-------------|
| Current deploy target | Always from **current config** + canonical `root_kind` + `relative` |
| Historical absolute `target` in manifest | Audit / migration aid only — **never** sole source of truth for where to write |
| Undeploy after path change | Remap via `deploy_paths.remap_manifest_targets` / equivalent, then delete remapped paths |
| Allowed roots | `deploy_security` — do not widen for convenience |

Existing helpers to keep: `services/deploy_paths.py` (`root_kind`, `relative`, `attach_canonical_targets`, `resolve_deploy_managed_path`).

---

## 5. FilePlan Contract

### 5.1 Types (target shape)

```text
DeployFilePlan
    mod_id
    deploy_type
    source_kind          # folder | zip | rar | 7z | mixed
    content_root         # folder root or staging root (if any)
    managed_path         # library folder
    target_root          # primary root for diagnostics
    target_root_kind     # game_mods | install | stamps | custom | …
    archives[]           # optional archive paths when source is archive
    files[]              # DeployFilePlanEntry — THE only file list
    diagnostics          # counts filled as pipeline progresses
```

```text
DeployFilePlanEntry
    source_relative      # under content_root OR archive member path
    target_relative      # under target_root_kind
    target_absolute      # projection for this run (config + relative)
    source               # abs file path OR archive path (for type=archive)
    op                   # copy | extract_member
    type                 # "", archive, pak, smapi, jar, stamps, …
    root_kind
    required             # default True
```

Bridge from today’s `ManifestFileEntry`: keep `source`, `target`, `type`, `root_kind`, `relative`, `source_relative`; add explicit `op` and plan-level identity. Manifest persistence remains `DeployManifest` + `ManifestFileEntry` after Apply/Verify (backup metadata attached by `BackupManager`).

### 5.2 Single-list rule

```text
Build FilePlan
    ↓
SAME FilePlan
    ↓
Backup(planned targets)
    ↓
Apply(plan entries)
    ↓
Verify(plan entries)
    ↓
Manifest(plan entries + backup metadata)
```

**Forbidden:**

- Plan scans once, Copy scans again, Manifest scans again
- `after_files − before_files` to invent the deployed set
- Strategy `deploy()` calling `self.plan()` again as a second authority
- CustomPath “rebuild file list after copy” as the success source of truth

---

## 6. Strategy Contract

Strategies are **game rule adapters**, not mini DeployManagers.

**Allowed:**

- Map content / archive layout → `target_root_kind` + `target_relative`
- Game-specific layout rules (Anno zip root → `mods/`; stamps → Documents; Palworld pak roots; Stardew SMAPI roots; STS jars; Duckov `info.ini` root; custom absolute root)
- Contribute to FilePlan building (path mapping only)

**Forbidden for Strategies:**

- Archive discovery
- Archive extraction
- File enumeration as a second pipeline
- Deployment accounting / success semantics
- Manifest generation as authority (core builds from FilePlan)
- Backup
- DB status persistence
- `before/after` directory snapshots

Phase 3 complete: Strategies are adapters; Core Apply owns copy/extract; no Strategy `self.plan` / extract / after-before / post-scan deploy accounting.

---

## 7. Backup Contract

- Owner: `BackupManager` (`.info/backups/`, `deploy_transaction.json`)
- Input: **FilePlan** `target_absolute` list only
- Semantics unchanged: backup existing files before overwrite; reuse valid prior backups on redeploy; rollback on failure
- Distinct from `metadata_backup` / `data/mod_backup` — never conflate

---

## 8. Verify Contract

Success condition (fixed):

```text
Every required FilePlan entry’s target_absolute exists as a file
(and optional size/hash checks per entry type)
```

All of these are success when Verify passes:

| Situation | Result |
|-----------|--------|
| Target missing → copy/extract | success |
| Target exists → overwrite | success |
| Target exists, identical content | success |

**Forbidden:** judging success by directory set-difference.

Archive entries (`type=archive`): do not compare target size to zip file size (keep current verifier rule).

---

## 9. Manifest Contract

- Written only after Verify passes
- Files list = FilePlan entries (+ `backup` fields from BackupManager)
- Schema v2: `root_kind` + `relative` required for new writes; absolute `target` is projection
- Undeploy deletes **only** manifest targets (never wipe whole trees)
- `attach_canonical_targets` / security validation before save — keep

---

## 10. Failure Contract

A failed Deploy result **must** include enough fields to answer “where did it die?”:

| Field | Meaning |
|-------|---------|
| `source` / `managed_path` | Library Mod folder |
| `source_kind` | folder / zip / … |
| `archives` | Archive paths attempted |
| `archive_entry_count` | Members enumerated (if archive) |
| `planned_file_count` | `len(FilePlan.files)` |
| `target_root` | Primary target root (never blank if known) |
| `planned_target_count` | Same as planned files for 1:1 plans |
| `backup_count` | Targets that had a pre-existing file backed up |
| `copied_count` / `extracted_count` | Apply progress |
| `verified_count` | Entries that passed Verify |
| `failed_count` | Entries that failed |
| `failed_files[]` | Paths + reason |
| `error` / `error_code` / `stage` | Human + machine |

**Forbidden failure shapes:**

```text
files=0
target=
error=没有可部署的文件
```

when a non-empty FilePlan existed or when overwrite made `after − before == 0`.

---

## 11. Security Contract

Keep and do not weaken:

- `deploy_security.collect_allowed_target_roots` / protected roots
- `validate_planned_sources` (workspace-bound sources)
- `validate_manifest_for_save` (planned targets from **current FilePlan**, never self-approve from the new manifest alone)
- Zip-slip / absolute member rejection in extract
- Symlink / junction safe iteration via `deploy_fs`
- No deploy into library as game target; no silent allow-root expansion

---

## 12. Modules forbidden from implementing Deploy logic

| Module area | May | Must not |
|-------------|-----|----------|
| Identity | Resolve IDs for DeployManager input | Extract, copy, write deploy manifests |
| Reconcile | Call `recover_stale_deploy_transactions` only | Redeploy / invent deploy status |
| Metadata / cover / offline | Own metadata domain | Deploy file I/O |
| UI | Start `DeployWorker`; display status/manifest | Call extract/copy/plan directly |
| DB schema | Store deploy_status fields written by Deploy core | Encode deploy algorithms |
| Conflict detection | Warn on overlapping targets from FilePlan/manifests | Block or rewrite Deploy accounting |
| Library loading | Show cards / status | Parallel deploy pipeline |
| `deployment_record` | Snapshot deployed IDs | Call deploy/undeploy |

Architecture guards (Phase 4) must fail CI if these modules grow forbidden imports/calls.

---

## 13. When Deploy Contract Tests must run

Any change touching:

- `services/deploy.py` / future `DeployManager`
- `services/deploy_rules/**`
- `services/deploy_paths.py`, `deploy_security.py`, `deploy_fs.py`
- `services/backup_manager.py` (overwrite backups)
- `services/archive_extractor.py` / extract used by Deploy
- `services/deploy_verifier.py`, `deploy_status.py`
- Manifest schema / save path

…must run the Deploy Contract Test suite (Phase 4). Game-specific fixtures alone are not enough; include the **existing-target overwrite** regression.

---

## 14. Required reading for new AI / maintainers

1. `docs/DEPLOYMENT_ARCHITECTURE.md` (this file)
2. `services/deploy.py` — current facade / pipeline
3. `services/deploy_rules/base.py` — `DeployContext` / `StrategyResult`
4. `services/deploy_rules/manifest.py` — manifest schema
5. `services/deploy_paths.py` — canonical targets
6. `services/deploy_security.py` — boundaries
7. `services/backup_manager.py` — overwrite backup
8. `services/archive_extractor.py` — extract primitive
9. Workspace rules: ID architecture; Witcher 3 `game_version` (unrelated to Deploy FilePlan)

---

## 15. Phase plan (binding)

| Phase | Scope | Business code? | Status |
|-------|--------|----------------|--------|
| **1 — Audit** | Current map, duplicates, FilePlan design, boundaries, migration | **No** | Done |
| **2 — Core** | DeployManager + FilePlan + unified Apply/Verify/diagnostics | Yes | **Done** |
| **3 — Strategies** | Adapters only; remove Strategy extract/enumerate/accounting | Yes | **Done** |
| **4 — Guards + tests** | Code-level contracts + architecture guards + contract tests | Yes (docs/tests/comments) | **Done** |
| **5 — Production acceptance** | Real Folder/ZIP/overwrite/redeploy/undeploy on production Mods | Run only; no schema pollution | Pending |

### Permanent ownership (summary)

| Role | Owner |
|------|--------|
| Path mapping | Strategy (`plan()` only) |
| File list authority | `DeployFilePlan.files` |
| Backup | BackupManager over FilePlan targets |
| Filesystem mutation / ArchiveExtractor | `deploy_apply.py` |
| Deploy success | `verify_file_plan` |
| Manifest files | Derived from FilePlan only |
| Strategy.deploy | Inert compatibility shell — Core must not call it |
| after-before / post-scan accounting | **Forbidden** |

### Phase 2–3 delivery (landed)

- `services/deploy_file_plan.py` — `DeployFilePlan` / entries / diagnostics
- `services/deploy_apply.py` — Core Apply (`copy` / `extract_member` via `ArchiveExtractor`)
- `services/deploy_verifier.verify_file_plan` — Verify consumes FilePlan only
- `ModDeployer._deploy_with_context` — Backup → Apply → Verify → Manifest from same FilePlan
- All Strategies: `plan()` mapping; `deploy()` inert
- Guards: `tests/test_deploy_architecture_guards.py`
- Contracts: `tests/test_deploy_phase4_contracts.py` (existing/partial/empty/verify-fail + Anno shapes)

---

## 16. Phase 1 findings summary (historical — remediated in Phases 2–3)

The following described the **pre-refactor** codebase. It is retained as forensic
context. Current code must match §§1–8 and the Phase 4 ownership table — not this section.

### Pre-refactor problems (fixed)

- Strategies owned plan + copy + often extract + manifest construction → now path mapping only.
- Anno archive success via `_snapshot_files(after) - before` → **removed**; FilePlan authority.
- `self.plan(ctx)` inside `deploy()` → **removed**; `deploy()` inert.
- CustomPath post-copy rescan → **removed**.
- Strategy-owned `ArchiveExtractor.extract` → **removed**; Core Apply owns extract.
