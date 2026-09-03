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
- This line of work has **not** mutated the production database, deleted mods, or quarantined mods.
- This line of work **did** re-archive existing Workshop `3786388428` into its existing `.info/` (overwrite). Mod row count unchanged (1766). No new identity.
- identity_repair audit fact (unchanged): CRITICAL=7 HIGH=0 — seven polluting Steam `source_url`s remain.
- 3451 / 3452 still present (KEEP; Nexus official identity).
- Related tests this round: **94 passed** (archive asset pipeline + cache + proxy fallback + rate limit + html parser + p0 governance + offline/modio archive tests). Not full-repo pytest.

P0-2: Network **PASS**. Asset pipeline performance **PASS** vs the 419s blocker. Overall P0-2 **can close**. **RUNTIME_VERIFIED** for Workshop `3786388428`.

This round: P0-2 asset-pipeline minimal fix + targeted tests + live re-archive. See `docs/P0_ARCHIVE_ASSET_PIPELINE_FORENSIC.md` §10.

---

## This round

P0-1: unchanged (CODE_IMPLEMENTED + TESTED; production repair waiting authorization).

P0-2: Asset pipeline fix **CODE_IMPLEMENTED + TESTED + RUNTIME_VERIFIED**. HTML 200. `.info/index.html` valid. First post-fix archive `assets=93.64s` (4.47× vs 419.01s); repeat archive `assets=0.16s`. `GLOBAL_ASSET_WORKERS` still 6. Residual: first CSS localize still 404s Steam-relative `url()` (~257 fails, not 15s timeouts). P0-2 **can close**.

P0-3: FORENSIC_COMPLETE. Scheme B **not** executed.

P0-4: FORENSIC + INSTRUMENTATION. Waiting one real startup `[BACKUP_RESULT] reason=reconcile`. No concurrency change.

P0-5: DRY_RUN_PLANNED. Waiting `--apply --yes`.

P0-CROSS Data Hygiene: **AUDIT_COMPLETE / CLEANUP_DEFERRED**. Not CLEAN. Not P0-6. `identity_repair_production_backup/` (~99.86 GiB) KEEP until P0-5 PRODUCTION_VERIFIED. SAFE_DELETE not executed.

---

## P0-1 Identity Governance

**STATUS:** CODE_IMPLEMENTED + TESTED (guards). Production leftovers not repaired this round.

**WHAT_IS_SOLVED:** Missing-row status persist cannot mint. Conflict scan cannot recreate 0349. Empty Mod mint rules from prior round remain.

**WHAT_IS_NOT_SOLVED:** 7 polluted URLs; 3451/3452 placeholder titles; production verification.

**EVIDENCE:** `test_update_mod_status_cannot_mint_missing_mod`, `test_post_deploy_conflict_scan_cannot_recreate_0349`.

**NEXT_STEP:** Do not apply URL scrub until authorized. Restart app is not an identity apply.

## P0-2 Archive

**STATUS:** CODE_IMPLEMENTED + TESTED + RUNTIME_VERIFIED (`3786388428`). Asset-pipeline 419s blocker **closed**. `GLOBAL_ASSET_WORKERS` remains **6**.

**WHAT_IS_SOLVED:** Windows LAN proxy; Steam HTML 200; valid `.info/index.html`; cache/seen no longer re-HTTP on hit; CSS localize-once (`/* smm-css-localized */`); asset timeout does not spend a second 15s direct retry; INFO distinguishes hit/miss/fail; nested CSS `url()` prefetch uses the existing 6-worker pool after the top-level pool.

**WHAT_IS_NOT_SOLVED (residual, not the 419s class):** Steam CSS relative `url()` often 404s (`.../public/css/skin_1/<hash>.png`). First localize of a page can still spend ~90s on those 404s. Repeat archive of the same `.info` is ~0.16s. Do **not** raise worker count for this.

**EVIDENCE:** Live `3786388428` 2026-09-03: HTML 200, proxy `http://127.0.0.1:12450`. Run 1 `assets=93.64s` hit=38 miss=0 fail=257 top_ok=49 top_fail=0 nested_ok=0 nested_fail=257. Run 2 `assets=0.16s` hit=38 fail=0. vs old 419.01s. Tests: `tests/test_archive_asset_pipeline.py` + related archive tests, 94 passed. Forensic addendum: `docs/P0_ARCHIVE_ASSET_PIPELINE_FORENSIC.md` §10.

**NEXT_STEP:** P0-2 can close. Optional later: CSS relative-url 404 path correction. Do not raise `GLOBAL_ASSET_WORKERS` first.

## P0-3 Conflict

**STATUS:** FORENSIC_COMPLETE. Scheme B **not** executed. Detector still auto-persists FILE_OVERWRITE as `conflict`.

**WHAT_IS_SOLVED:** Full semantic audit. Ghost mint already blocked (P0-1).

**WHAT_IS_NOT_SOLVED:** Auto persist of FILE_OVERWRITE. No load-order/game rules for scheme A.

**EVIDENCE:** `docs/P0_CONFLICT_DETECTOR_AUDIT.md`; `data/p0_anno_conflict_forensics.json`.

**NEXT_STEP:** Authorize scheme B (diagnostic only) — not this round.

## P0-4 Deploy / Backup

**STATUS:** FORENSIC + INSTRUMENTATION. Production 5min+ **not** measured.

**WHAT_IS_SOLVED:** Reconcile already emits `[BACKUP_START] reason=reconcile` and `[BACKUP_RESULT] reason=reconcile total_elapsed_ms=...`. Per-mod `backup synced ... copy_ms= persist_ms=`. No extra instrumentation this round. Not multithreaded.

**WHAT_IS_NOT_SOLVED:** Which stage dominates a real 5min run.

**EVIDENCE:** `docs/P0_BACKUP_FORENSIC.md`; `test_reconcile_emits_aggregate_backup_result`.

**NEXT_STEP:** One real startup log with reconcile `[BACKUP_RESULT]`.

## P0-5 Production Repair

**STATUS:** DRY_RUN_PLANNED. APPLY_UNVERIFIED. No apply this round.

**WHAT_IS_SOLVED:** Dry-run list still valid for 7 URL scrubs. 3451/3452 forensic KEEP.

**WHAT_IS_NOT_SOLVED:** Production apply + verification protocol.

**EVIDENCE:** `data/p0_url_scrub_dry_run.json` (`applied: false`, CRITICAL=7).

**NEXT_STEP:** Wait for explicit `--apply --yes` authorization.

## P0-CROSS Hygiene

One horizontal track (repo + data). **Not P0-6.**

### Repository hygiene (prior)

**STATUS:** Phase-local.

**WHAT_IS_SOLVED:** Resolver/archive/main_window/sync_view have no 7890/7897/12450 literals.

**WHAT_IS_NOT_SOLVED:** `tools/_p0_forensics_readonly.py` still historically probes 7890/7897 (KEEP forensic). `scripts/_finalize_audit.py` still REVIEW.

### Data Hygiene

**STATUS:** **AUDIT_COMPLETE / CLEANUP_DEFERRED**. Not CLEAN. Not CLEANUP_COMPLETE.

**WHAT_IS_SOLVED:** Audit report exists. No files deleted.

**WHAT_IS_NOT_SOLVED:** Cleanup deferred. `identity_repair_production_backup/` (~99.86 GiB) KEEP until P0-5 is PRODUCTION_VERIFIED. SAFE_DELETE candidates untouched.

**EVIDENCE:** `docs/P0_DATA_HYGIENE_AUDIT.md`.

**NEXT_STEP:** Do **not** start Data Cleanup. P0-2 closed. Continue P0-4 runtime capture / P0-5 authorization.

---

Do not read this file as FIXED / CLEAN / PRODUCTION_VERIFIED for Identity apply. P0-2 Archive asset pipeline is RUNTIME_VERIFIED for `3786388428`.
