# P0 SYSTEM HARDENING PLAN

**Status:** PLAN ONLY. Not implemented in the forensic session.  
**Depends on:** `docs/P0_SYSTEM_FORENSICS.md`  
**Forbidden in this plan’s first implementation slice:** UI redesign, schema rewrite, Deploy/Archive architecture rewrite, deleting tests, mocking away environment failures, applying further production repair without the verification protocol.

---

## Goal

Stop cross-module identity minting. Make archive, conflict, and deploy failures produce an evidence chain. Make “plan / test / apply / production” impossible to conflate in reports.

Success is **not** “the 22 mods are gone”. Success is:

> Refresh, Offline Archive, Deploy, Reconcile, Metadata Update, and Sidecar Apply cannot mint a new Mod identity. Every deploy/archive/conflict decision is reconstructable from logs.

---

## 0. Fact-state protocol (do this first in code)

Introduce a single typed result used by CLI, tests, and docs:

```json
{
  "code_status": "CODE_IMPLEMENTED|CODE_MISSING",
  "test_status": "TESTED|UNTESTED|TEST_FAILED",
  "plan_status": "DRY_RUN_PLANNED|NO_PLAN",
  "apply_status": "NOT_APPLIED|APPLY_EXECUTED|APPLY_FAILED",
  "production_status": "UNCHANGED|MUTATED|UNKNOWN",
  "verification_status": "NOT_VERIFIED|APPLY_UNVERIFIED|PRODUCTION_VERIFIED|PRODUCTION_DELETED_AND_VERIFIED"
}
```

Rules:

- `TESTED` never implies `PRODUCTION_VERIFIED`.
- `DRY_RUN_PLANNED` never implies `APPLY_EXECUTED`.
- Quarantine is `MUTATED` filesystem, not `PRODUCTION_DELETED_AND_VERIFIED`.
- `PRODUCTION_DELETED_AND_VERIFIED` requires: DB row absent **and** active library path absent **and** no dangling live references **and** post-repair audit CRITICAL=0 HIGH=0.

Implementation: one module, e.g. `services/verification_result.py`, consumed by identity-repair CLI and future mutation commands.

---

## 1. Identity Authority (close remaining mint leaks)

### 1.1 Keep `allocate_mod_id` at one caller

Already true (`identity_service.allocate_internal_id`). Add a contract test that AST/grep-fails if a second production caller appears.

### 1.2 Make Steam-range stub insert refuse CREATE outside IdentityService

- `_ensure_mod_stub` and `update_mod_deploy_status` INSERT must **not** invent a mods row for a missing id.
- Missing row → error / no-op, never `platform=steam` + `workspace_id=mid`.

### 1.3 Reconcile CREATE gate

Reconcile may **bind** an existing identity. It may call `create_mod_identity` only when:

- platform is known, **and**
- official external id is present and passes platform validation, **and**
- no existing row matches, **and**
- operation is explicitly `import_discovered` (logged).

Unresolved folders stay unresolved. No `Unknown Mod` mint.

### 1.4 Sidecar / metadata

- `published_file_id` on non-Steam rows must not store internal PK.
- `url`/`source_url` must not call `steam_workshop_url(internal_id)` (already empty for internal; leftover historical strings must be scrubbed, not regenerated).
- Persist workspace_id to DB when sidecar has a real platform workspace id (Anno), without copying internal PK.

### 1.5 Empty Mod / Nexus placeholder

`Empty Mod <hex>` folders must not allocate a new identity on refresh/offline/reconcile. Either bind to existing Nexus external_id or stay unresolved.

---

## 2. Lifecycle Authority (contract tests)

Encode the matrix as tests in `tests/test_lifecycle_authority.py` (new; do not delete existing tests):

| Operation | Can Create | Can Bind | Can Modify Identity | Can Delete |
|---|---:|---:|---:|---:|
| Import | YES | YES | controlled | NO |
| Refresh | NO | YES | controlled | NO |
| Offline Archive | NO | NO | NO | NO |
| Deploy | NO | NO | NO | NO |
| Metadata Edit | NO | NO | user metadata only | NO |
| Reconcile | NO blind mint | YES | controlled | NO |
| Repair | NO allocation | YES | controlled | YES |
| Uninstall | NO | NO | NO | YES deployment |
| Rename | NO | NO | NO identity | NO |

Each test patches/spies `allocate_mod_id`, `create_mod_identity`, `upsert_mod` and asserts the cell.

Required new tests from the forensic brief:

- `test_plan_does_not_equal_apply`
- `test_apply_reports_actual_mutation`
- `test_failed_apply_reports_not_verified`
- `test_deleted_row_absent_after_apply`
- `test_deleted_folder_absent_after_apply` (active library; quarantine allowed)
- `test_canonical_untouched`
- `test_no_dangling_reference`
- `test_no_allocate_during_repair`
- `test_post_repair_audit_zero_critical`
- `test_post_repair_audit_zero_high`

---

## 3. Invariant scanner (CI + `audit --readonly`)

New CLI, read-only SQLite (`mode=ro`, no `DatabaseManager` backfill), e.g. `python -m services.identity_invariants --audit`.

Codes:

- `INVALID_INTERNAL_STEAM_ID`
- `INVALID_STEAM_WORKSPACE`
- `INVALID_STEAM_PUBLISHED_FILE_ID`
- `INVALID_STEAM_SOURCE_URL`
- `DUPLICATE_EXTERNAL_ID`
- `DUPLICATE_CANONICAL_URL`
- `DUPLICATE_SOURCE_URL`
- `ORPHAN_FOLDER`
- `ORPHAN_DB_ROW`
- `ORPHAN_SIDECAR`
- `UNKNOWN_MOD_INTERNAL_VISIBLE`
- `UNRESOLVED_ENTITY_WITH_PLATFORM`
- `INTERNAL_ID_EXPOSED_AS_PLATFORM_ID`

CI runs this against fixture DBs, not production. Production uses the same binary with `--readonly`.

---

## 4. Archive Authority (diagnostics, not fake success)

Do **not** “fix timeout” by lengthening 15s or skipping Steam. Diagnose then configure proxy.

Required logs (never put internal id into a Steam URL field):

```
[ARCHIVE_START] mod_id= platform= external_id= url= proxy= timeout= attempt=
[ARCHIVE_CONNECT] host= port= elapsed_ms=
[ARCHIVE_RESPONSE] status= elapsed_ms=
[ARCHIVE_RETRY]
[ARCHIVE_FAILURE] error_code= exception_type= elapsed_ms=
[ARCHIVE_FALLBACK] action=
[ARCHIVE_STUB]
```

Classify failures: `ENVIRONMENT_FAILURE | NETWORK_FAILURE | STEAM_FAILURE | DEPENDENCY_FAILURE | CONFIG_FAILURE | APPLICATION_FAILURE`.

On `curl:(28)` with empty proxy: **do not** present as “Steam page unavailable” only; say proxy/DNS/connect failed.

Optional later: startup probe of Steam via configured proxy; warn before batch archive.

---

## 5. Conflict Authority (trace + type split)

### 5.1 Persist `ConflictDecisionTrace`

Fields: `mod_id`, `conflict_type`, `rule`, `candidate_mod_id`, `candidate_path`, `matched_file`, `matched_rule`, `decision`, `reason`, `timestamp`.

Write to sidecar `.info/conflict_trace.json` and/or a SQLite table. UI must show: why, with whom, which file, which rule.

### 5.2 Split types

Never promote FILE_OVERWRITE to IDENTITY_CONFLICT.

- `IDENTITY_CONFLICT` — conflicting platform identity evidence only
- `FILE_OVERWRITE` / `PATH_CONFLICT` / `DEPLOYMENT_CONFLICT`
- `DEPENDENCY_CONFLICT` / `GAME_RULE_CONFLICT`

### 5.3 Anno stamps policy (product, small change)

Shared `Documents/Anno 1800/stamps` overlap is **expected** for layout packs. Options (pick one, document it):

- Treat stamp overlaps as **warning**, not hard `conflict`, **or**
- Namespace stamp deploy by managed folder, **or**
- Keep hard conflict but label it `STAMP_TREE_OVERWRITE` with partner names.

Fix persist so **all** owners of an overlapping target get the same status (0360 currently `none`).

Fix encoding of stamp paths (zip CP437/GBK) so notes are readable.

---

## 6. Deploy Authority (timing is first-class)

Every deploy already has `deploy_stage`. Extend to the required shape:

```
[DEPLOY_START] mod_id= mod_name= game= source= target=
[DEPLOY_STAGE] stage=resolve|verify_source|prepare_archives|plan|manifest|backup|copy|extract|validate|persist|conflict_scan|cleanup
  elapsed_ms= files= bytes= source= target=
[DEPLOY_RESULT] status=SUCCESS|FAILED error_code= stage= source= target= files= bytes= total_ms= exception_type=
```

Persist a per-deploy JSON under `.info/deploy_timing.json` so a minutes-long run is reconstructable without the console.

Performance follow-ups (after logs exist; do not rewrite architecture first):

- Hash each unique source path once (Anno stamps currently hash the same zip per file).
- Conflict preview: index manifests incrementally; do not `Path.resolve` 10k targets on every deploy if a cache exists.
- Never run library-wide backup/robocopy concurrently with user deploy (document / lock).
- Post-deploy conflict scan already async; log its elapsed_ms separately so it is not blamed on copy.

---

## 7. Filesystem Authority

Quarantine, backup, and active library are different namespaces.

- Repair moves invalid unique folders to `data/identity_repair_quarantine/<run>/`.
- Active library listing must ignore quarantine.
- Reconcile must **not** import from quarantine.
- Reports must say QUARANTINED vs DELETED.

---

## 8. Production repair (next apply slice)

Do **not** re-apply 13+9. They are already row-absent.

Next **gated** apply, after backup:

1. `SCRUB_POLLUTING_SOURCE_URL` for the remaining **7** CRITICAL rows (`0354, 0360, 0361, 0362, 3031, 3054, 3225`).
2. Scrub sidecar `url` / illegal `published_file_id` on those entities. **Do not** regenerate Steam URLs. **Do not** delete the entities.
3. Bind Anno `workspace_id` from sidecar into DB where it is a real platform workspace id (e.g. `17863521013284165` on `0362`).
4. Decide policy for `Empty Mod` 3451/3452 (bind or unresolved) — do not merge into ghosts.
5. Run verification protocol; refuse to print FIXED if CRITICAL≠0.

---

## 9. Architecture governance (module rule)

Business modules may report: “I observed Steam `external_id=…`.”  
They may not: `INSERT mods`, `allocate_mod_id`, invent `workspace_id`/`published_file_id`.

Authorities:

- IdentityService — create/bind/update/reject/conflict of identity
- Lifecycle — which operations may call IdentityService
- Filesystem — managed folder, sidecar, quarantine
- Deploy — copy/extract/backup only
- Archive — HTTP/page only

Lint/test: production code outside `identity_service` / `mod_identity_authority` / importers’ `create_mod_identity` call must not call `allocate_mod_id` or raw `INSERT INTO mods`.

---

## 10. Implementation order

1. Verification result type + CLI output (prevents report lies).
2. Invariant scanner `--readonly` + CI fixture tests.
3. Lifecycle contract tests + allocate caller = 1 test.
4. Close `_ensure_mod_stub` / deploy INSERT create leak.
5. Reconcile create gate + Empty Mod guard.
6. Archive structured logs + failure class.
7. ConflictDecisionTrace + UI copy of why/who/rule (minimal UI text, not redesign).
8. Deploy structured START/STAGE/RESULT + timing sidecar.
9. Remaining 7 URL scrubs under protocol.
10. Anno stamps conflict policy (decision + persist symmetry).

Each step ships with tests. No step is “green tests by weakening production semantics”.
