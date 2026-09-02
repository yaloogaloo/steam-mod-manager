# P0 Master Progress

Living progress file. Not a production-verified claim.

```json
{
  "code_status": "CODE_IMPLEMENTED",
  "test_status": "TESTED",
  "plan_status": "DRY_RUN_PLANNED",
  "apply_status": "APPLY_EXECUTED",
  "production_status": "MUTATED",
  "verification_status": "APPLY_UNVERIFIED"
}
```

Evidence:

- Prior identity repair apply: `2026-09-02T10:10:49Z` (parallel session).
- This line of work has **not** run `--apply --yes`.
- This line of work has **not** mutated the production database.
- identity_repair audit fact (unchanged): CRITICAL=7 HIGH=0 — seven polluting Steam `source_url`s.
- Readonly SQLite this session: ghosts `3438`–`3450` absent; nine invalid duplicates absent; `3451`/`3452` present; max_internal still `9000000000003452`; seven polluted URLs still present.
- identity_invariants DB-only scan (empty tmp library, no FS walk): CRITICAL=11 HIGH=21 REQUIRES_REVIEW=10. This is a **different, broader scanner** than identity_repair `--audit`. Extra findings are pre-existing; they are not apply authorization.

---

## This session (code, not production apply)

P0-1: Empty Mod **without** official identity still cannot mint. Empty Mod **with** official Nexus/Steam identity is allowed; placeholder title is stripped. Scanner flags leftover Empty Mod `title` even when `display_name` is real (`REQUIRES_REVIEW` + keep entity, not delete).

P0-4: Deploy stages include `extract` / `hash` / `manifest` / `persist`; `tests/test_deploy_stage_profiling.py` records stage names. Not a production bottleneck proof.

P0-CROSS: SAFE_DELETE three unreferenced scratch files. Dead duplicate statements after `return` removed from `allocate_internal_id`.

Tests (not full-repo):

- `tests/test_p0_system_governance.py` — 19 passed after the Empty Mod title/display_name scanner fix
- Related set (identity lifecycle/governance/repair, sidecar, conflict, deploy_db, platform_persistence, import identity gate, deploy stage profiling) — 79 passed immediately before that last scanner tweak

---

## P0-1 Identity Governance

**Status:** CODE_IMPLEMENTED + TESTED for mint guards. Production leftover entities not cleaned.

**Done:** IdentityService gates; lifecycle scopes; `_ensure_mod_stub` / deploy / metadata missing-row refuse; Empty Mod without official identity cannot mint; Empty Mod **with** official identity strips placeholder title (import still allowed); scanner; runtime `[IDENTITY_GUARD]`.

**Not done:** Production scrub of 7 URLs (needs apply auth). 3451/3452 title/folder hygiene (KEEP entity; do not delete).

## P0-2 Archive

**Status:** CODE_IMPLEMENTED observability. Network not “fixed”.

**Done:** ARCHIVE_* logs; curl 28 → NETWORK_FAILURE; failed stub marker; `python -m scripts.archive_diagnostics`.

**Not done:** Environment/DNS/proxy recovery (not a code claim).

## P0-3 Conflict

**Status:** CODE_IMPLEMENTED traces + UI distinction.

**Done:** FILE_OVERWRITE retained; decision trace; both owners persist; internal / workshop / workspace labels.

**Not done:** Production sidecar/DB workspace_id drift for Anno 0362 (reconcile persist exists; production row still empty until a reconcile pass).

## P0-4 Deploy

**Status:** CODE_IMPLEMENTED instrumentation. Profiling started in tests, not production.

**Done:** DEPLOY_START/STAGE/RESULT; stages include resolve, extract, plan, backup, copy, validate, hash, manifest, persist, conflict_scan.

**Not done:** Production bottleneck proof from a real slow run. Do not optimize from guesses.

## P0-5 Production Repair & Verification

**Status:** DRY_RUN_PLANNED for 7 URLs. APPLY not authorized this line. APPLY_UNVERIFIED overall.

**Done:** Dry-run list in `data/p0_url_scrub_dry_run.json`. 3451/3452 forensic (KEEP).

**Not done:** Apply 7 URL scrubs. Full DB/FS/reference verification protocol. PRODUCTION_VERIFIED.

## P0-CROSS Repository Hygiene

**Status:** Phase-local only. Full audit not started.

**This round (hygiene, phase-local):**

SAFE_DELETE:

- `_e2e_nexus_real_html.py` — unreferenced scratch; opened production DB
- `_scan_imports_tmp.py` — unreferenced import-walker scratch
- `_schema_dump_tmp.py` — unreferenced schema dump; hardcoded `E:\` path

LIKELY_DELETE / REVIEW (not deleted): `scripts/_finalize_audit.py` (unreferenced; mutates production if run).

KEEP: `tools/_p0_forensics_*.py`, `tools/deploy_smoke_runner.py`, all `__init__.py` / `_compat` style modules.

---

Do not read this file as FIXED / CLEAN / PRODUCTION_VERIFIED.
