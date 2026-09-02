# P0 VERIFICATION PROTOCOL

Any mutation command (`apply`, `delete`, `repair`, `merge`, `quarantine`, `migration`) must emit **BEFORE / ACTION / AFTER / VERIFIED** and a verification result object. Tests and production reports are separate artifacts.

---

## 1. Result object

```json
{
  "code_status": "CODE_IMPLEMENTED | CODE_MISSING",
  "test_status": "UNTESTED | TESTED | TEST_FAILED",
  "plan_status": "NO_PLAN | DRY_RUN_PLANNED",
  "apply_status": "NOT_APPLIED | APPLY_EXECUTED | APPLY_FAILED",
  "production_status": "UNCHANGED | MUTATED | UNKNOWN",
  "verification_status": "NOT_VERIFIED | APPLY_UNVERIFIED | PRODUCTION_VERIFIED | PRODUCTION_DELETED_AND_VERIFIED"
}
```

### Forbidden phrases unless the matching status is true

| Phrase | Allowed only when |
|---|---|
| 已删除 / deleted | `PRODUCTION_DELETED_AND_VERIFIED` |
| 已修复 / FIXED | `PRODUCTION_VERIFIED` and post-audit CRITICAL=0 HIGH=0 |
| 已应用 | `apply_status=APPLY_EXECUTED` |
| 测试通过所以生产好了 | never |
| quarantine = delete | never |

If ACTION succeeded and VERIFIED failed → **`APPLY_UNVERIFIED`**. Still say ACTION succeeded. Do not say FIXED.

---

## 2. `PRODUCTION_DELETED_AND_VERIFIED`

All four must hold for **each** targeted entity:

1. `mods` row absent (query by integer PK, not only text CAST).
2. Active library path absent (`mod/<game>/…`, not quarantine).
3. Live reference graph has no dangling PK (`mod_tags`, `mod_relations`, `mod_relationships`, `deployment_records*`, `last_known_path` of other rows). Audit tables may retain history.
4. Post-mutation `identity_repair --audit` and `audit_mod_library_integrity()` report CRITICAL=0 and HIGH=0 for the scoped invariants.

Quarantine present is compatible with (2) if the path is outside the active library and reconcile ignores it.

---

## 3. Mutation command protocol

### 3.1 BEFORE

Record:

- DB path, size, checksum (SHA256 of `mod_manager.db`)
- mods row count
- invalid entity counts (ghosts, duplicate groups, polluting URLs)
- targeted id list
- filesystem snapshot of targeted folders (exists, file count)
- reference graph counts for targeted ids

Never open `DatabaseManager` for a read-only BEFORE if it backfills. Use `sqlite3` URI `mode=ro`.

### 3.2 ACTION

- Require `--yes` for apply.
- Refuse allocate (`repair_no_allocate_scope`).
- Write `identity_repair_audit` (or equivalent) per entity: before_state, after_state, operation.
- On exception: rollback SQL; do not claim AFTER.

### 3.3 AFTER

Re-read the same queries as BEFORE (new connection, readonly).

Print counts, not prose:

```
AFTER:
  invalid_rows=…
  quarantined=…
  canonical_present=…
```

### 3.4 VERIFIED

Run:

- `identity_repair.py --audit` (readonly)
- `audit_mod_library_integrity()` on a **non-backfilling** path or documented exception
- integer PK absence checks
- active-library path absence
- canonical identity equality (platform, external_id, source_url, workspace_id) vs BEFORE snapshot
- allocate count during ACTION == 0

Set `verification_status` from those predicates, not from ACTION exit code.

---

## 4. Test vs production reports

| Artifact | May claim |
|---|---|
| pytest log | TESTED / TEST_FAILED on **fixtures** |
| `identity_repair_dry_run.json` | DRY_RUN_PLANNED |
| `identity_repair_preflight.json` | READY_FOR_APPLY, production_mutated=false |
| `P0_PRODUCTION_REPAIR_REPORT.md` | production_* only from live DB/FS |
| CI invariant scanner | fixture DBs unless explicitly a production job |

A passing `test_deleted_row_absent_after_apply` does not update production_status.

---

## 5. Identity repair specific gates

Before `--apply --yes`:

- Backup DB + library (checksum recorded).
- Plan has no MERGE for confirmed invalid ghosts.
- Plan has no MARK_CONFLICT for confirmed invalid duplicates.
- `allocation_count` in dry-run is 0.

After apply:

- Print BEFORE/ACTION/AFTER/VERIFIED block to stdout and JSON.
- If CRITICAL or HIGH remain → `APPLY_UNVERIFIED` even if 22 rows were deleted.

---

## 6. Archive / Deploy / Conflict (non-identity mutations)

Same protocol, different predicates.

**Archive:** BEFORE offline_status + stub/live hash; AFTER status/bytes; VERIFIED = live HTML or explicit ENVIRONMENT_FAILURE class, never “success” on stub.

**Deploy:** BEFORE empty; ACTION must log `[DEPLOY_START]`…`[DEPLOY_RESULT]`; VERIFIED = target files exist + sizes + timing sidecar. Minutes-long SUCCESS without stage timings is APPLY_UNVERIFIED for observability.

**Conflict persist:** BEFORE statuses; AFTER must include ConflictDecisionTrace; VERIFIED = UI-available why/who/file/rule. Badge-only “冲突” is not verified.

---

## 7. Current production snapshot (2026-09-02T10:20Z)

Use this as the worked example of the protocol:

```
BEFORE (09:45Z preflight): invalid_rows=22 (13+9), polluting_url=19, CRITICAL=32, HIGH=9
ACTION (10:10:49Z): deleted=22, scrubbed=19, quarantined=unique folders
AFTER: ghost_rows=0, duplicate_rows=0, CRITICAL=7, HIGH=0, new_internal=2
VERIFIED: db_rows_absent=22 true; active_fs_absent=true; canonical_untouched=true;
          post_audit_critical_zero=false
verification_status=APPLY_UNVERIFIED
apply_status=APPLY_EXECUTED
```

This session’s forensics did not run ACTION.

---

## 8. Automation checklist (to implement later)

- [ ] `VerificationResult` dataclass + JSON serializer
- [ ] CLI always prints the four blocks
- [ ] `test_plan_does_not_equal_apply`
- [ ] `test_apply_reports_actual_mutation`
- [ ] `test_failed_apply_reports_not_verified`
- [ ] integer PK queries in verification (avoid CAST-only false negatives)
- [ ] CI invariant scanner `--readonly`
- [ ] Ban report language in a small unit test over fixture JSON (optional)

Until those exist, humans filling `docs/P0_*` reports must follow this protocol manually.
