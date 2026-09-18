# Identity Backup Storage Audit (Phase 4)

Date: 2026-09-15  
Status: **PASS — audit and migration design only. No directories renamed. No business code changed.**

Live sample used in earlier phases:

```
Frozen UUID  36834fcf-3cbb-4ffe-8b78-be1921638bd4
SQLite PK    296
```

On disk today:

```
data/mod_backup/296/metadata.json   internal_id = 36834fcf-3cbb-4ffe-8b78-be1921638bd4
data/mod_backup/296/                directory name = 296
```

Target after a later Phase 5 (not executed here):

```
internal_id (UUID)
        |
        v
data/mod_backup/<UUID>
```

SQLite `mod_pk` remains legal only for SQL / FK / temporary resolve.

This document answers:

1. Is Backup storage fully PK-dependent?
2. Which backups can be recovered via Frozen UUID?
3. Which backups have PK only?
4. What compatibility reads does a UUID directory need?
5. How to avoid duplicate backups and data loss?

Related: `docs/identity_runtime_migration_audit.md` §3.3 (persistence paths).

---

## Scope lock

This phase **did**:

- Read-only census of live `data/mod_backup/` and `data/deploy_backup/`
- Read-only analysis of `metadata_backup.py`, `backup_manager.py`, restore callers
- Write this file

This phase **did not**:

- Modify `metadata_backup.py`
- Modify `backup_manager.py`
- Modify `deploy.py` / `deploy_file_plan.py`
- Change Manifest schema
- Rename, copy, or delete any Backup directory

Temporary census scripts were executed then deleted. They are not part of the tree.

---

## Current identity chain (end of Phase 3)

```
Frozen UUID
 ↓
DeployContext(UUID + mod_pk)
 ↓
DeployFilePlan(UUID + mod_pk)
 ↓
BackupManager / metadata_backup
        |
        v
        mod_pk   ❌  storage key
```

Two storage trees exist. They must not be confused:

| Tree | Root | Purpose | Storage key today |
|---|---|---|---|
| A | `data/mod_backup/<key>/` | Metadata / cover / offline snapshot (`metadata_backup.py`) | **SQLite PK** |
| B | `data/deploy_backup/<key>/` | Pre-overwrite original **game** files (`BackupManager`) | **SQLite PK** |

Phase 5 text in the original migration plan refers to Tree A. Tree B uses the same PK leak and must follow the same identity, or Deploy restore remains PK-keyed.

---

# 1. Answers

## 1.1 Is Backup storage fully dependent on `mod_pk`?

**Yes, for directory names and all write/load APIs.** Frozen UUID is already inside most Tree A `metadata.json` payloads and in `mods.internal_id`, but it is **not** the folder name and **not** the storage-key return value.

Evidence:

- `backup_root(mod_id)` → `data/mod_backup/<str(mod_id)>`. Callers pass a proven **PK**.
- `prove_backup_storage_key()` documents and returns **current `mods.mod_id`**. A Frozen UUID hint is resolved through `resolve_mod_pk` / `find_mod_by_internal_id` and collapsed to PK before any path is built.
- `load_backup(mod_id)` **returns `None` unless `mid.isdigit()`**. A UUID token cannot load a backup today, even if a UUID directory existed.
- `validate_backup(mod_id)` same digit gate.
- `BackupManager.storage_key()` returns `self.internal_id` if set; `deploy.py` still constructs `BackupManager(..., internal_id=pk)`.
- `_infer_internal_id(managed)` reads `.info` Frozen UUID then **maps it to PK** so `data/deploy_backup/<mod_id>/` stays PK-aligned.

Workspace ID is never the folder name (good). `find_mod_by_workspace_id` is not used as a backup key (good). The leak is **PK as filesystem identity**.

## 1.2 Which backups can be recovered via Frozen UUID?

Tree A, class **A**: sidecar `metadata.json` already stores a hyphenated Frozen UUID. Join to `mods.internal_id` is 1:1 for this set.

Live census (2026-09-15):

| Recoverability | Count | Share of 3149 Tree A dirs |
|---|---:|---:|
| Sidecar Frozen UUID (direct migrate) | **2704** | **85.87%** |
| Same set also has `mods.internal_id` UUID | 2704 | 85.87% |
| PK 296 sample | UUID `36834fcf-3cbb-4ffe-8b78-be1921638bd4` in sidecar **and** DB | — |

Tree B: the single live overwrite dir `data/deploy_backup/1/` has **no** sidecar UUID. It **is** recoverable via DB: PK `1` → Frozen `87eec2a1-90f4-448d-8818-d31cd490704f`.

## 1.3 Which backups have PK only?

**All directory names are PK.** Content without a Frozen UUID:

| Class | Tree A | Tree B |
|---|---:|---:|
| B — PK dir, identity is digit / collapsed Frozen | **444** (14.10%) | **0** |
| C — no Frozen UUID; sidecar synthetic / DB empty | **1** (PK `3009`) | **0** |
| Tree B — PK dir, UUID only in DB | — | **1 / 1** |

Class B examples (Tree A): folder `2402`, sidecar `internal_id` = `683230077` (same digits as `workspace_id`). These cannot be renamed to a UUID directory until Frozen UUID is minted. That mint is **identity repair**, not a Backup rename, and is out of Phase 4/5 directory-move scope unless a prior gated mint exists.

Class C: PK `3009`, `mods.internal_id` empty, sidecar `internal_id` = `9000000000000083`, `workspace_id` = `178903990956050`. Folder name is still the live PK, so the tree is not anonymous, but it is **not UUID-migratable** without human / identity repair.

## 1.4 Compatibility reads for UUID directories

Required order after Phase 5 (design only):

```
caller token (UUID or legacy digit PK)
        |
        v
1. If token is Frozen UUID: find_mod_by_internal_id → mod_pk (SQL only)
2. Prefer  data/mod_backup/<UUID>/
3. Fallback data/mod_backup/<mod_pk>/
4. Accept payload only if backup.internal_id == mods.internal_id
        (when both are Frozen UUID)
5. Writes go only to the UUID directory once it exists
```

Tree B (overwrite) same order:

```
Prefer  data/deploy_backup/<UUID>/
Fallback data/deploy_backup/<mod_pk>/
Plus existing legacy .info/backups/ (already in BackupManager._allowed_backup_roots)
```

`load_backup` / `validate_backup` must drop the `isdigit()`-only reject. Digit remains a **legacy folder name**, not the business key.

## 1.5 How to avoid duplicate backups and data loss

- One canonical tree per entity: UUID directory.
- Never `copytree` PK → UUID if UUID dest already exists and sidecar UUID matches.
- If both trees exist: UUID wins; PK is leftover until verified GC.
- Atomic move: `os.replace` / Win32 `MoveFileEx` when dest absent; otherwise copy + hash verify + only then mark leftover.
- Do not delete the PK directory in the same step as the rename.
- Rewrite `mods.backup_cover_path` / `mods.backup_offline_path` only after the UUID tree verifies.
- Skip class B/C until Frozen UUID exists — do not invent a UUID from PK or workspace_id.

---

# 2. Task 1 — Backup root census

Census method: list top-level directories under live `core.paths.data_dir()`. Classify names with a Frozen UUID regex vs `str.isdigit()`. Emptiness is a shallow listing (do not walk hashed overwrite trees). Identity files read: `metadata.json`, `.info/metadata.json`, `info/metadata.json`, `deploy_manifest.json`, `deploy_transaction.json`.

Workspace glob of `data/mod_backup/**` returns nothing (gitignore). Disk is populated.

## 2.1 `data/mod_backup/`

| Metric | Count |
|---|---:|
| Total directories | **3149** |
| Digit / PK names | **3149** |
| UUID names | **0** |
| Empty directories | **0** |
| Unparseable names | **0** |
| Hidden | **0** |
| Orphan PK (no `mods` row) | **0** |
| Every dir has `metadata.json` | **3149** |
| Any dir has `.info/` | **0** |

Typical layout (PK `296` and the rest of class A):

```
data/mod_backup/<pk>/
    metadata.json     # copy of .info metadata; Frozen internal_id when minted
    cover.png|jpg|…   # optional
    offline/          # optional index + assets
```

No nested `.info`, no deploy manifest, no sidecar folder. Identity is **`metadata.json` + directory name**.

## 2.2 `data/deploy_backup/`

| Metric | Count |
|---|---:|
| Total directories | **1** |
| Digit / PK names | **1** (`1`) |
| UUID names | **0** |
| Empty | **0** |
| Unparseable | **0** |
| Payload identity files | **none** |
| DB Frozen UUID for that PK | **yes** (`87eec2a1-90f4-448d-8818-d31cd490704f`) |

Layout:

```
data/deploy_backup/1/
    a.txt.<hash>.<ts>.<nonce>.original
```

Identity is **folder name = PK** plus `mods` join. Manifests store posix paths relative to `data/` (`deploy_backup/<pk>/...`) via `BackupManager.relative_backup_path`.

## 2.3 SQLite join (same moment)

| `mods` column | Count (3149 rows) |
|---|---:|
| Frozen UUID `internal_id` | 2704 |
| Digit collapsed `internal_id` | 444 |
| Empty `internal_id` | 1 (PK `3009`) |
| `backup_cover_path` contains `mod_backup` | 3093 |
| `backup_offline_path` contains `mod_backup` | 1978 |
| Empty `backup_cover_path` | 56 |

Cover/offline columns store **absolute PK paths**, e.g. `D:\project\steam-mod-manager\data\mod_backup\1\cover.png`. Phase 5 must rewrite or dual-resolve these or MISS cover/offline OPEN breaks after rename.

---

# 3. Task 2 — Backup internal identity classes

Classification rules (per user):

- **A** — payload or directory name contains Frozen UUID → direct migrate.
- **B** — only PK (dir name and/or digit `internal_id`) → needs DB mapping, and needs UUID mint if Frozen is missing/collapsed.
- **C** — no usable identity → human handling.

## 3.1 Tree A (`mod_backup`)

| Class | Count | Share | Meaning |
|---|---:|---:|---|
| **A** | 2704 | 85.87% | `metadata.json.internal_id` is Frozen UUID. Matches `mods.internal_id`. |
| **B** | 444 | 14.10% | PK folder; sidecar + DB `internal_id` are **digits** (collapsed Frozen / workshop-shaped). |
| **C** | 1 | 0.03% | PK `3009`: DB Frozen empty; sidecar `internal_id` = `9000000000000083` (not UUID, not PK). |

UUID-ready for directory rename **without identity mint**: **2704 / 3149 = 85.87%**.

PK-only (cannot use Frozen UUID as folder name yet): **445 / 3149 = 14.13%** (B+C).

Samples:

| Class | Folder | Sidecar `internal_id` | Sidecar `workspace_id` | DB Frozen |
|---|---|---|---|---|
| A | `1` | `87eec2a1-90f4-448d-8818-d31cd490704f` | `1854056682` | same UUID |
| A | `296` | `36834fcf-3cbb-4ffe-8b78-be1921638bd4` | `3308841144` | same UUID |
| B | `2402` | `683230077` | `683230077` | `683230077` |
| C | `3009` | `9000000000000083` | `178903990956050` | empty |

No Tree A directory is anonymous (every name is a live PK). Class C is “no Frozen UUID”, not “lost folder”.

## 3.2 Tree B (`deploy_backup`)

| Class | Count | Meaning |
|---|---:|---|
| A | 0 | No UUID in payload or dirname |
| **B** | **1** | PK `1`; Frozen UUID recoverable **only** via DB |
| C | 0 | — |

Overwrite backups have no `metadata.json`. Migration **must** use `mods.mod_id → mods.internal_id`. Do not invent UUID from hashes or filenames.

---

# 4. Task 3 — `metadata_backup.py` (analysis only)

## 4.1 `backup_root`

```
backup_root(mod_id) -> data_dir() / "mod_backup" / str(mod_id).strip()
```

Today `mod_id` is PK. Future: `backup_root` must accept Frozen UUID as the **canonical** path segment, with PK fallback (see §6).

## 4.2 `prove_backup_storage_key`

Documented return: **current Backup storage key = `mods.mod_id`**.

Order today:

1. `resolve_mod_pk(hint)` → digit PK
2. `.info/internal_id` → `find_mod_by_internal_id` → PK
3. Unresolved → `""` (must not write)

Future: prove **Frozen UUID** as the storage key; PK is an internal resolve step only.

## 4.3 Write / load / restore

| Function | Input | Path | Digit gate |
|---|---|---|---|
| snapshot write (`dest = backup_root(mid)`) | proven PK | PK dir | `mid.isdigit()` before mkdir |
| `load_backup` | token | `backup_root(mid)` | **rejects non-digit** |
| `restore_info_sidecar_from_backup` | UUID or PK via `resolve_mod_pk` | `backup_root(mid)` PK | restore matches `backup.internal_id == mods.internal_id` |
| `mark_missing` | token | SQL by PK | `isdigit()` |

Restore already compares Frozen UUID **inside** `metadata.json`. It still **opens** the PK directory. After migration it must open UUID first.

## 4.4 Callers — future UUID path support

Production (must change in Phase 5):

| File | Role | UUID path needed |
|---|---|---|
| `services/metadata_backup.py` | `backup_root`, prove, snapshot, load, restore | **write + read** |
| `services/metadata_backup_sync.py` | sync dest; `prove_backup_storage_key`; `mid.isdigit()` | **write + read** |
| `services/metadata_backup_validator.py` | `validate_backup` dest; digit gate | **read** |
| `services/mod_presence.py` | presence, snapshot dest, `load_backup` | **read + write** |
| `services/info_asset_runtime.py` | offline dest + `load_backup` | **read + write** |
| `services/backup_asset_migration.py` | offline dest | **write** |
| `services/mod_metadata_resolver.py` | offline index + `load_backup` (digit gate) | **read** |
| `services/offline/backup_offline_repair.py` | `dest_offline` | **write** |
| `services/file_ops.py` | `prove_backup_storage_key` then `isdigit()` before sync | **write gate** |
| `services/library_reconcile.py` | `restore_info_sidecar_from_backup`, `load_backup` | **read** |
| `core/db_manager.py` | `iter_mod_backup_key_rows` docs folder as `<mod_id>`; `backup_cover_path` / `backup_offline_path` | **path rewrite** |
| `services/cover_projection.py` | reads `backup_cover_path` | **read** (if columns stay absolute) |

Tests (Phase 5 contract, not this phase): `tests/test_backup_storage_key_contract.py`, `tests/test_metadata_backup*.py`, `tests/test_mod_presence_miss.py`, `tests/conftest.py` (`_backup_root` helper), others that assert `backup_root(pk).name == pk`.

Tools (not Phase 5 production unless re-run): `tools/census_backup_consistency.py`, `tools/archive/legacy_workspace_backup.py`, `tools/archive/legacy_backup_finalize.py`, `tools/archive/legacy_asset_tools/*`.

---

# 5. Task 4 — `BackupManager`

## 5.1 Today

| Member | Actual value |
|---|---|
| Constructor `internal_id=` | **PK**, because `deploy.py` passes `pk` (deploy ~2350, ~3430) |
| `storage_key()` | `self.internal_id` if set, else `_infer_internal_id`, else `_orphan_<hash16>` |
| `_infer_internal_id` | Reads Frozen UUID from `.info`, **returns PK** |
| `backups_root()` | `data/deploy_backup/<storage_key>/` |
| `relative_backup_path` | posix under `data/`, e.g. `deploy_backup/1/file` |
| `resolve_backup_file` | `data/deploy_backup/...`, or legacy `.info/backups/` |
| Transaction JSON `mod_id` | PK (lifecycle) |

Comment on `_infer_internal_id` already admits the PK alignment: *“Resolve it to the SQLite PK so `data/deploy_backup/<mod_id>/` stays aligned with Deploy's `ctx.internal_id`.”* That comment is stale vs Phase 2 (`ctx.internal_id` is UUID); the **call site** still passes PK, so on-disk behavior is unchanged.

Stale-txn recovery (`deploy.py` ~1536) uses `BackupManager(folder)` with empty `internal_id`, so `storage_key()` infers PK from `.info`.

## 5.2 Future modification point (Phase 5, not now)

```
storage_key() -> Frozen UUID
backups_root() -> data/deploy_backup/<UUID>/
_infer_internal_id() -> return Frozen UUID (stop collapsing to PK)
deploy.py BackupManager(..., internal_id=ctx.internal_id)  # UUID, not pk
```

`mod_pk` stays on `DeployContext` for SQL only.

Manifest **schema** need not change if `backup.path` remains a relative file path. Phase 5 **must** dual-resolve:

1. path as written (`deploy_backup/<pk>/...`)
2. rewritten (`deploy_backup/<UUID>/...`)
3. legacy `.info/backups/`

Rewriting stored manifest paths is optional and riskier than dual-read.

---

# 6. Task 5 — Restore flow compatibility

## 6.1 Tree A — `.info` restore (`restore_info_sidecar_from_backup`)

Today:

```
token (UUID or PK)
    → resolve_mod_pk → mid (PK)
    → backup_root(mid)           # data/mod_backup/<PK>
    → read metadata.json
    → refuse unless backup.internal_id == mods.internal_id
    → write .info on an existing DB entity only
```

After UUID directories:

```
token
    → resolve Frozen UUID (find_mod_by_internal_id)
    → mod_pk = SQL handle only
    → try  data/mod_backup/<UUID>/metadata.json     # preferred
    → else data/mod_backup/<PK>/metadata.json       # legacy
    → same Frozen UUID equality check
    → never create a new entity from backup
```

`load_backup` used by MISS UI / resolver must use the same prefer-UUID then PK order. Today a UUID argument is dropped on the floor (`not mid.isdigit()`).

## 6.2 Tree A — MISS cover / offline

Readers use:

1. SQLite `backup_cover_path` / `backup_offline_path` (absolute PK paths)
2. `backup_root(mid) / cover.*` and `offline/index.html`

Compatibility:

1. If the SQLite path exists, use it (covers unmigrated rows).
2. Else `mod_backup/<UUID>/...`
3. Else `mod_backup/<PK>/...`

After a successful rename, rewrite the two SQLite columns. Leaving stale PK paths is a **medium** risk (broken OPEN until fallback is coded).

## 6.3 Tree B — undeploy restore (`BackupManager.verify_backup_hash` / restore)

Today:

```
BackupManager(internal_id=pk)
    → backups_root = data/deploy_backup/<pk>
    → resolve_backup_file(manifest.backup.path)
```

After UUID directories:

```
BackupManager(internal_id=UUID)
    → allowed roots:
         data/deploy_backup/<UUID>/
         data/deploy_backup/<mod_pk>/     # fallback
         managed/.info/backups/
         managed/info/backups/
    → resolve relative deploy_backup/<pk>/... by also trying <UUID>
```

Undeploy of an in-flight deploy that wrote PK paths must keep working after the folder is renamed.

## 6.4 Recommended read order (both trees)

1. Frozen UUID directory  
2. Legacy PK directory  
3. (Tree B only) managed `.info/backups/`  
4. Reject workspace_id / folder-name / `external_id` as storage keys  

Write order after cutover:

1. UUID directory only  
2. Never create a new PK directory for an entity that already has Frozen UUID  

---

# 7. Task 6 — Phase 5 migration strategy (do not execute)

## 7.1 Goal

```
data/mod_backup/296
        →
data/mod_backup/36834fcf-3cbb-4ffe-8b78-be1921638bd4
```

Same for every class A row. Tree B:

```
data/deploy_backup/1
        →
data/deploy_backup/87eec2a1-90f4-448d-8818-d31cd490704f
```

Legacy PK directories remain **readable** until an operator GC after verification.

## 7.2 Preconditions (per entity)

Eligible for rename:

- `mods.internal_id` matches Frozen UUID regex
- Tree A: sidecar `metadata.json.internal_id` equals that UUID (class A)
- Tree B: DB UUID only is enough (no sidecar); still refuse if PK row missing
- Dest UUID path does not exist, **or** exists and verifies as the same entity

Not eligible (leave PK dir; do not guess UUID):

- Class B collapsed digit Frozen (444)
- Class C PK `3009`
- Any row whose sidecar UUID ≠ DB Frozen UUID (conflict → stop that row)

Identity mint for the 445 is a **separate** program. Backup rename must not mint.

## 7.3 Per-entity Tree A algorithm

1. Resolve `pk`, `uuid` from SQLite.  
2. `src = mod_backup/pk`, `dst = mod_backup/uuid`.  
3. If `src` missing: skip (nothing to move).  
4. If `dst` missing: **atomic rename** `src → dst` (same volume). On Windows, treat in-use files as abort for that row (no partial rename).  
5. If `dst` exists:  
   - Verify `dst/metadata.json.internal_id == uuid`  
   - If `src` also exists: compare metadata UUID + cover/offline hashes; if equal, leave `src` as leftover; if not equal, **abort row** (duplicate conflict).  
6. Verify: `dst/metadata.json` readable and UUID matches.  
7. Rewrite `backup_cover_path` / `backup_offline_path` to `dst` paths when those files exist.  
8. Do **not** delete `src` in this step if rename was a copy; leftover GC is a later dry-run.

Rollback: if verify fails after copy, delete only the incomplete `dst` and keep `src`. Never delete `src` on verify failure.

## 7.4 Per-entity Tree B algorithm

Same rename `deploy_backup/pk → deploy_backup/uuid`, plus:

- Dual-read in `resolve_backup_file` **before** any rename, so a crash mid-migration still undeploys.
- Do not rewrite Manifest JSON in the first pass (schema unchanged). Dual-read is enough.
- Hash-verify files listed in the current deploy manifest after rename.

## 7.5 Runtime compatibility (ship before or with first rename)

Must land **before** directories move, otherwise MISS/restore blackout:

1. `backup_root` / `load_backup` / `validate_backup`: UUID preferred, PK fallback.  
2. `prove_backup_storage_key`: return Frozen UUID.  
3. `BackupManager.storage_key`: UUID; infer UUID not PK.  
4. `deploy.py`: pass `ctx.internal_id` into `BackupManager`.  
5. Dual-read `deploy_backup/<uuid>` and `deploy_backup/<pk>`.

Then migrate directories. Then (optional later) stop creating PK dirs.

## 7.6 Duplicate / split-brain policy

| Situation | Action |
|---|---|
| Only PK dir | Rename to UUID |
| Only UUID dir | Already done; update SQLite paths if stale |
| Both, same UUID, same hashes | UUID canonical; PK leftover |
| Both, UUID mismatch | **Stop that entity**; no delete |
| UUID dir exists, PK dir other entity’s files | **Stop**; never merge |

Writes during migration: if UUID dir exists, all new snapshots go there. PK dir becomes read-only leftover.

## 7.7 Ordering

1. Code dual-read + UUID write key (still no rename).  
2. Dry-run census: eligible / skip B/C / conflicts.  
3. Tree A class A rename (majority).  
4. Tree B overwrite dirs (tiny live set: 1).  
5. Leftover PK GC after a soak, with backup of the census list.  
6. Class B/C only after Frozen UUID mint (separate spec).

---

# 8. Task 7 — Risk

## High

| Risk | Why | Mitigation |
|---|---|---|
| **Lost backup** | Rename/copy without verify; delete PK too early; Windows file lock | Atomic rename; verify sidecar UUID + hashes; never delete PK in the same step |
| **UUID missing** | 444 collapsed + 1 empty Frozen | Skip; do not derive UUID from PK/workspace_id |
| **PK rebuild** | Treating folder name as Frozen; `load_backup` digit-only after mixed tree | Dual-read; prove UUID from DB/sidecar; PK is resolve-only |

## Medium

| Risk | Why | Mitigation |
|---|---|---|
| **Duplicate directories** | Crash between copy and delete; new snapshot written to PK while UUID dest exists | UUID-first writes; leftover GC with hash compare |
| **Stale SQLite paths** | 3093 cover / 1978 offline absolute PK paths | Dual-read + rewrite columns after verify |
| **Stale manifest backup.path** | `deploy_backup/<pk>/file` after Tree B rename | Dual-resolve in `resolve_backup_file`; optional later rewrite |
| **In-flight deploy** | Active `BackupManager` holding PK root | Refuse migrate for mods with active deploy txn |
| **Class C 3009** | Sidecar synthetic id ≠ PK ≠ Frozen | Manual identity repair before any rename |

## Low

| Risk | Why | Mitigation |
|---|---|---|
| **Log names** | Logs still print PK as `mod_id=` / old `internal_id=` | Phase 8 log cleanup; not a data-loss path |
| **Test fixtures** | Many tests assert `backup_root(pk).name == pk` | Update in Phase 5 with the code change |
| **Archive tools** | `tools/archive/*` still PK | Out of production path |

---

# 9. Files Phase 5 must change (not changed now)

## Must (Tree A metadata backup)

- `services/metadata_backup.py`
- `services/metadata_backup_sync.py`
- `services/metadata_backup_validator.py`
- `services/mod_presence.py`
- `services/info_asset_runtime.py`
- `services/backup_asset_migration.py`
- `services/mod_metadata_resolver.py`
- `services/offline/backup_offline_repair.py`
- `services/file_ops.py`
- `services/library_reconcile.py`
- `core/db_manager.py` (path columns + `iter_mod_backup_key_rows` contract)

## Must (Tree B overwrite backup)

- `services/backup_manager.py` (`storage_key`, `_infer_internal_id`, allowed roots)
- `services/deploy.py` (`BackupManager(..., internal_id=ctx.internal_id)`)

## Likely

- `services/cover_projection.py` (absolute backup cover paths)
- Deploy tests that pass `internal_id=pk` into `BackupManager`

## Must not in Phase 5 unless separately specified

- Manifest schema (`services/deploy_rules/manifest.py` field types)
- Minting Frozen UUID for the 445 class B/C rows
- Deleting leftover PK directories in the same PR as the first rename

---

# 10. Acceptance

## Phase 4 Backup Storage Identity Audit: PASS

### 1. Current Backup directory identity

| Tree | Total | Digit PK | UUID | Empty | Unparseable |
|---|---:|---:|---:|---:|---:|
| `data/mod_backup/` | 3149 | 3149 | 0 | 0 | 0 |
| `data/deploy_backup/` | 1 | 1 | 0 | 0 | 0 |

**Storage is 100% PK-named.** Payload Frozen UUID exists in Tree A class A only.

### 2. UUID recoverable proportion

- Tree A **direct** (sidecar Frozen UUID): **2704 / 3149 = 85.87%**
- Tree A via DB Frozen UUID: same 2704 (no extra)
- Tree B via DB Frozen UUID: **1 / 1 = 100%** (no sidecar UUID)

### 3. PK-only proportion

- Directory names: **100%** of both trees
- Tree A content without Frozen UUID: **445 / 3149 = 14.13%** (444 collapsed + 1 empty/synthetic)
- Tree B payload: **1 / 1** PK-only files (UUID only in SQLite)

### 4. Files to modify later (Phase 5)

See §9. Primary: `metadata_backup.py`, `backup_manager.py`, `deploy.py`, plus every `backup_root` / `load_backup` production caller listed in §4.4.

### 5. Future Phase 5 migration

Ship UUID-prefer / PK-fallback **reads** first; write to UUID dirs; atomic rename of class A (and Tree B via DB map); never mint UUID during rename; never delete PK until verify; rewrite SQLite cover/offline paths after success.

### This phase did not modify

- `metadata_backup.py`
- `backup_manager.py`
- any Backup directory
- Manifest schema
- `deploy.py` / `deploy_file_plan.py`

No code change was required to complete the audit. Phase 5 is the first implementation slice.
