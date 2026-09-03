# P0-CROSS Data Hygiene Audit

**Track:** P0-CROSS Repository / Data Hygiene. **Not P0-6.** Does not interrupt P0-1–P0-5.

```text
STATUS: AUDIT_IN_PROGRESS
deleted_nothing: true
SAFE_DELETE_EXECUTED: false
CLEAN: false
CLEANUP_COMPLETE: false
```

Readonly inventory generated `2026-09-02T14:05:42Z` by `tools/p0_data_hygiene_audit.py` (walk + reference scan + size rollup). No files under `data/` were deleted, moved, or mutated. Business code was not changed.

Machine summary: `docs/p0_data_hygiene_inventory.json`. Full per-file listing is not kept in git (340k rows). Re-run the tool if a fresh walk is needed.

---

## 1. Inventory Summary

| Metric | Value |
|---|---|
| Top-level entries | 40 |
| Total files | **340,583** |
| Total directories | **29,358** |
| Total bytes | **111,588,643,717** (~**103.94 GiB**) |

### Largest directories (top-level)

| Path | Files | Bytes | ~GiB |
|---|---:|---:|---:|
| `identity_repair_production_backup/` | 300,626 | 107,228,601,061 | **99.86** |
| `asset_cache/` | 6,708 | 2,505,417,803 | 2.33 |
| `mod_backup/` | 32,001 | 1,750,369,073 | 1.63 |
| `browser_profile/` | 304 | 32,490,677 | 0.03 |
| `identity_repair_quarantine/` | 841 | 31,613,330 | 0.03 |
| `import_cache/` | 53 | 19,426,064 | 0.02 |
| `_smoke_ws2/` | 9 | 927,403 | <0.01 |
| `_smoke_ws/` | 9 | 927,398 | <0.01 |
| `headers/` | 1 | 69,631 | <0.01 |

**96% of `data/` is one pre-apply snapshot of the live `mod/` library**, not runtime cache and not test junk.

### Largest files

All of the top files live under `identity_repair_production_backup/20260902T094631Z/mod/…` (zip/rar/pak copies of the library). Examples: Anno `100.zip` (~3.07 GiB), several Baldur’s Gate 3 Mantis preset archives (~0.9–1.4 GiB). These are **historical snapshot copies** of user mods, not extra caches created by the app.

Production DB: `mod_manager.db` = 13,012,992 bytes, plus SQLite sidecars `mod_manager.db-wal` (4,148,872) and `mod_manager.db-shm` (32,768).

---

## 2. Category Summary

Exclusive classification of the **40 top-level** entries. Trees inherit the parent category. File/byte totals below are from the walk, not guesses.

| Category | Top-level items | Files (approx) | Bytes (approx) |
|---|---:|---:|---:|
| A PRODUCTION_RUNTIME | 6 | ~6,766 | ~2.54 GiB |
| B PRODUCTION_BACKUP | 2 | 332,627 | ~101.49 GiB |
| C P0_EVIDENCE | 18 | ~860 | ~0.03 GiB |
| D TEST_ARTIFACT | 11 | ~27 | ~2 MB |
| E TEMPORARY_ARTIFACT | 1 | 1 | ~1 MB |
| F CACHE_REGENERABLE | 4 | (overlap with A) | see notes |
| G DUPLICATE | 0 proven content-identical disposable sets | — | — |
| H UNKNOWN_REVIEW | 1 | 304 | ~31 MB |

Overlap notes:

- `asset_cache/` and `headers/` are **runtime-used caches** (A and F). Classified **KEEP** as production runtime caches. Regenerable, **not** SAFE_DELETE.
- `import_cache/` directory is a **runtime staging root** (A). Its leftover children are E/LIKELY_DELETE pending confirmation no in-flight deploy.
- `identity_repair_production_backup/` is B and also P0 rollback evidence. Classified **B KEEP**.
- G: `_smoke_ws` vs `_smoke_ws2` are **near-same size** (927,398 vs 927,403) but were **not** hashed as identical. Treat as likely duplicate test workspaces, not proven `DUPLICATE`.

### A. PRODUCTION_RUNTIME

| Path | Why |
|---|---|
| `mod_manager.db` | SQLite production DB (`core.paths.database_path()`). |
| `mod_manager.db-wal` / `mod_manager.db-shm` | Live SQLite sidecars. No string hits in repo because SQLite creates them. **Deleting looks like “temp” and would corrupt an open DB.** |
| `headers/` | `ui/game_info_worker.py` writes `headers/{app_id}.jpg`. One file: `1623730.jpg`. |
| `asset_cache/` | `core.paths.asset_cache_dir()`; `services/archive.py` reads/writes hashed Steam/CSS assets. mkdir on demand. |
| `import_cache/` (directory) | `services.importers.archive.import_cache_root()`; deploy + Anno staging. |
| `.gitkeep` | Repo placeholder. |

### B. PRODUCTION_BACKUP

| Path | Why |
|---|---|
| `mod_backup/` | `services/metadata_backup.py` → `data/mod_backup/<mod_id>/`. Written on every library reconcile. 1,758 mod trees, 32,001 files. **Runtime + recovery.** |
| `identity_repair_production_backup/20260902T094631Z/` | Manual/robocopy snapshot **before** identity apply: `mod_manager.db` copy + full `mod/` tree + `BACKUP_MANIFEST.txt`. Documented in `docs/P0_PRODUCTION_REPAIR_REPORT.md`. **Not created by current `identity_repair.py`.** Runtime does not read it. **Rollback for APPLY_UNVERIFIED identity repair. KEEP.** |

### C. P0_EVIDENCE

See §4. Includes identity apply dumps, quarantine, forensic JSON, GUI trace cited in `docs/P0_SYSTEM_FORENSICS.md`.

### D. TEST_ARTIFACT

Pytest stdout dumps (`_full_suite_*.txt`, `_fail_audit*.txt`, `_test_list.txt`), `_deploy_smoke_report.json`, `_smoke_ws/`, `_smoke_ws2/` (isolated smoke sqlite under `data/_smoke_ws*/data/mod_manager.db`).

### E. TEMPORARY_ARTIFACT

`relative_diag.log` (~1.0 MB, 2026-08-26). `[RELATIVE_DIAG]` writer **no longer exists** in `.py` sources. One-off UI badge diagnostic.

### F. CACHE_REGENERABLE

`asset_cache/` (also A), `headers/` (also A), `app_names.json`, `game_info.json` (`.gitignore` comments: “legacy JSON caches (no longer used)”; **no `.py` reader**). `browser_profile/` is Chromium user-data (see H).

### G. DUPLICATE

- No top-level JSON semantic duplicates (`json.dumps(..., sort_keys=True)`).
- `mod_backup` metadata.json hashes: **no two mod_ids share metadata**.
- Full-tree content hashing of backup/cache was **skipped** (too large). Same-size files **inside** the production-backup `mod/` copy (e.g. Mantis Vol I appearing twice, Mac/Windows `Icons.blp`) are **library history copied into the snapshot**, not extra app caches. **Historical snapshot ≠ disposable duplicate.**
- `_smoke_ws` ≈ `_smoke_ws2` size: **candidate duplicate workspaces**, not proven hash-identical.

### H. UNKNOWN_REVIEW

`browser_profile/nexus/` — Chromium profile (`Default`, `Local State`, Crashpad, GPU cache). `.gitignore` lists it. **Current Playwright code uses `chromium.launch()`, not `launch_persistent_context` / `user_data_dir`.** May still hold Nexus cookies. **REVIEW, not SAFE_DELETE.**

---

## 3. SAFE_DELETE (candidates only — **not executed**)

Must meet all gates: no runtime / test / config / dynamic / CLI reference; not production; not P0 evidence; regenerable or pure temp; delete does not change runtime semantics.

| Path | Size | Proof |
|---|---|---|
| `_full_suite_clean.txt` | 14,086 | Pytest log dump. Zero code refs. |
| `_full_suite_phase5.txt` | 1,644 | Same. |
| `_full_suite_phase5_final.txt` | 1,408 | Same. |
| `_full_suite_phase5_timeout.txt` | 1,282 | Same. |
| `_full_suite_v2.txt` | 7,376 | Same. |
| `_fail_audit.txt` | 11,727 | Same. |
| `_fail_audit_phase5.txt` | 22,786 | Same. |
| `_test_list.txt` | 100,240 | Pytest `--collect-only` dump. Zero code refs. |
| `_deploy_smoke_report.json` | 7,622 | Smoke runner leftover. Zero code refs. Default smoke workspace is `tempfile`, not `data/`. |

**Total ~168 KB.** Deleting them would not shrink the 104 GiB tree in any meaningful way.

**This round did not delete them.** Future cleanup must be a separate, explicit confirmation and must only touch this set.

---

## 4. LIKELY_DELETE (needs human confirm)

| Path | Size | Why likely | Why not SAFE yet |
|---|---|---|---|
| `_smoke_ws/` | 927,398 | Isolated smoke library + sqlite. | Nested `data/mod_manager.db`; confirm not reused as a manual workspace. |
| `_smoke_ws2/` | 927,403 | Second smoke run, almost same size. | Same. |
| `relative_diag.log` | 1,055,640 | Writer removed from code. | Might still want for historical UI-badge forensics. |
| `app_names.json` | 62 | Legacy Steam name cache; only `.gitignore` mentions it. | Tiny; confirm no out-of-repo script. |
| `game_info.json` | 482 | Same; Palworld header metadata only. | Same. |
| `import_cache/*` leftover UUID / `deploy_*` / `_modio_live_verify` / `_trace_rar_test` | ~19 MB | Staging should be cleaned after deploy. 26 child dirs remain. | Last mtime **2026-09-02T09:51:25Z** (same morning as Anno deploy forensic). Could be a crashed/in-flight extract. **Do not wipe during an open deploy.** The **directory itself** must remain. |

---

## 5. REVIEW

| Path | Why |
|---|---|
| `browser_profile/nexus/` | Stale Chromium profile; possible login cookies. No current code path. |
| `import_cache/` leftover children | See LIKELY_DELETE; timing overlaps P0-3/deploy. |
| `identity_repair_idempotent.json` | 2026-08-29 `applied: true` from **older** `mod_identity_repair` (31 `scrub_polluted_external_id` actions). Not the 10:10:49Z apply. Keep until sure it is not cited as “the” apply. |
| `identity_audit_before.json` | 2026-08-29 integrity scan (`library: E:\project\...`). No current writer. Historical. Keep as evidence. |
| `identity_audit_after.json` | Written by `scripts/_finalize_audit.py` (mutates production if run). Snapshot only. |

**Rule used:** if purpose is not proven, REVIEW. Never guess-delete.

---

## 6. KEEP

Everything not in SAFE_DELETE / LIKELY_DELETE, including all of A, B, C and:

- Production SQLite + WAL/SHM
- `mod_backup/` (1,758 mods)
- `identity_repair_production_backup/` (~99.86 GiB)
- `identity_repair_quarantine/2026-09-02T101049+0000/`
- `asset_cache/`, `headers/`
- All `p0_*.json` and identity-repair JSON listed in §7
- `.gitkeep`

---

## 7. P0-related `data/` items (who writes / who reads)

Dynamic construction (not string-searchable as a fixed filename):

```text
data_dir() / "mod_backup" / <mod_id>
data_dir() / "headers" / f"{app_id}.jpg"
asset_cache_dir() / f"{sha256}{ext}"
import_cache_root() / <uuid>
import_cache_root() / f"deploy_{uuid}"
import_cache_root() / f"anno_stamps_{uuid}" / f"anno_plan_{uuid}"
data_dir() / "identity_repair_quarantine" / <utc timestamp>
database_path() → data/mod_manager.db   (+ SQLite WAL/SHM)
```

### Known P0 files

| Path | Created by | Read by | Runtime? | Tests? | Still needed for P0? | Regenerable? | Delete blocks verification? | Permanent audit? |
|---|---|---|---|---|---|---|---|---|
| `p0_url_scrub_dry_run.json` | Forensic CLI / one-off (not in `tools/` as a named writer). `applied: false`, `CRITICAL=7`, 7 `SCRUB_POLLUTING_SOURCE_URL` candidates. | Humans / this audit. **No `.py` string hit.** | No | No | **Yes — unapplied P0-5 plan** | Only by re-running dry-run against live DB | **Yes** | Yes until apply + AFTER/VERIFIED |
| `p0_3451_3452_forensics.json` | Readonly forensic session. KEEP entities. | Humans. No `.py` hit. | No | No | Yes (P0-1 leftovers) | Re-query DB | Yes if 3451/3452 still open | Yes |
| `p0_anno_conflict_forensics.json` | Readonly P0-3 forensic. | Docs (`P0_CONFLICT_DETECTOR_AUDIT.md`, master progress) | No | No | Yes until scheme B decision | Re-scan overlap | Weakens P0-3 record | Yes |
| `p0_archive_proxy_diagnostics.json` | Layered curl/proxy probe. | Master progress | No | No | Yes until P0-2 runtime verification | Re-probe network | Weakens P0-2 baseline | Yes |
| `p0_forensics_snapshot.json` | `tools/_p0_forensics_readonly.py` | Docs | No | No | Identity apply BEFORE/AFTER | Re-run tool | Yes | Yes |
| `p0_forensics_followup.json` | `tools/_p0_forensics_followup.py` | Docs | No | No | Same | Re-run | Yes | Yes |
| `p0_forensics_verify.json` | `tools/_p0_forensics_verify.py` | Docs | No | No | Same | Re-run | Yes | Yes |
| `p0_post_apply_readonly_audit.json` | Post-apply readonly | Docs | No | No | APPLY_UNVERIFIED record | Re-run audit | Yes | Yes |
| `identity_repair_preflight.json` | `scripts/identity_repair_preflight.py` | Protocol + repair report | No | No | BEFORE snapshot | Re-run preflight (DB has changed) | Yes | Yes |
| `identity_repair_dry_run.json` | `identity_repair --audit` (`applied: false`) | Protocol table | No | No | Plan vs later apply | Re-audit | Yes | Yes |
| `identity_repair_apply.json` | Apply CLI at **2026-09-02T10:10:50Z**. `applied: true`, `removed_invalid=22`, `scrubbed_url=19`, `quarantined=40`, `allocations=0`. | Forensics docs (older warning about a misleading dump — **this file now is the 10:10 apply**) | No | No | **Yes** | Cannot recreate without mutating again | **Yes** | Yes |
| `mod_backup/` | `metadata_backup` / reconcile / identity_repair moves | Runtime UI, deploy, refresh, **and** `tests/test_nexus_offline_scraper.py` (`REAL_OFFLINE_HTML = Path("data/mod_backup/9000000000000406/offline/index.html")`) | **Yes** | **Yes (production path as fixture)** | Yes (P0-4) | Recreated on reconcile **if** `.info` still exists | **Yes** | Operational + evidence |
| `identity_repair_quarantine/2026-09-02T101049+0000/` | `apply_identity_repair` | Forensic verify tool; recovery | No (not live library) | Tests use tmp quarantine | Yes until PRODUCTION_VERIFIED | No | **Yes** | Yes |
| `identity_repair_production_backup/` | External robocopy (not in `identity_repair.py`) | `_p0_forensics_followup.py`, repair report | **No** | No | **Yes — only full pre-apply rollback of `mod/` + DB** | Only by copying `mod/` again (live tree already mutated) | **Catastrophic if apply needs revert** | Yes until verification complete |

`p0_url_scrub_dry_run.json` is **not** garbage because it is a dry-run. It is the outstanding P0-5 plan (`applied: false`, 7 URL scrubs).

---

## 8. Looks unused but runtime uses it (dangerous)

1. **`identity_repair_production_backup/` (~100 GiB)** — looks like a duplicate of `mod/`. It **is** a point-in-time copy. Deleting it removes the only documented pre-apply rollback. Not cache.
2. **`mod_manager.db-wal` / `mod_manager.db-shm`** — look like temp files. They are the live SQLite write-ahead log. No source string references.
3. **`asset_cache/`** — looks like disposable CDN cache. Archive **reads and writes** it on every offline page build (`services/archive.py`). Deleting changes performance, not identity semantics; startup `mkdir`s it. Still **not** this-round SAFE_DELETE.
4. **`mod_backup/`** — looks like optional backup. Reconcile **always** syncs `.info` → here. UI reads it when the folder is missing. Tests parse a **real** backup HTML path.
5. **`import_cache/`** — looks like trash. Deploy/Anno **stage extracts** here. Leftover children may be trash; the root is not.
6. **`headers/`** — looks like HTTP debug dumps. It is Steam header artwork for GameInfo.

---

## 9. Multiple writers / readers

| Path | Writers | Readers |
|---|---|---|
| `mod_manager.db` | `DatabaseManager` (UI, reconcile, deploy, repair, …) | Entire app + forensic tools (`mode=ro`) |
| `mod_backup/<id>/` | `metadata_backup.py`, `metadata_backup_sync.py`, `library_reconcile.py`, `identity_repair.py` (move/delete with quarantine) | deploy, mod_refresh, mod_identity, detail/card UI, identity_repair, `test_nexus_offline_scraper.py` |
| `asset_cache/` | `services/archive.py` | same |
| `import_cache/` | `importers/archive.py`, `services/deploy.py`, `deploy_rules/anno.py` | cleanup only after stage |
| `headers/` | `ui/game_info_worker.py` | UI (cover path signal) |
| `identity_repair_quarantine/` | `apply_identity_repair` | forensic tools; not runtime library |
| `p0_forensics_*.json` | `tools/_p0_forensics_*.py` | docs / humans |
| `identity_audit_after.json` | `scripts/_finalize_audit.py` (**also mutates DB if executed**) | humans |

No evidence that two production writers randomly clobber the same P0 JSON except humans/tools re-running with `--out` to the same path.

---

## 10. Impact on P0 master line

**None of P0-1–P0-5 is blocked or rewritten by this audit.**

- Disk “full of files” is explained: **~100 GiB identity-repair production snapshot**, then **2.3 GiB asset cache**, then **1.6 GiB metadata backup**.
- Do **not** delete the snapshot to “clean data/” before P0-5 verification / possible rollback.
- `p0_url_scrub_dry_run.json` stays until authorized apply.
- Hygiene cleanup is a **later P0-CROSS** step, one SAFE_DELETE set at a time. **Not P0-6.**

---

## 11. What this round did / did not do

Did:

- Recursive inventory (path, size, mtime, ctime, extension, dir rollups).
- Code + docs + gitignore reference scan, including `data_dir() / <variable>` patterns.
- Lifecycle classification A–H.
- Duplicate checks on top-level JSON and `mod_backup` metadata.json (not full 100 GiB hashing).
- Readonly tool `tools/p0_data_hygiene_audit.py`.
- This report + master-progress Data Hygiene = `AUDIT_IN_PROGRESS`.

Did **not**:

- Delete anything under `data/` (including SAFE_DELETE candidates).
- `rm -rf data/*` or equivalent.
- Modify Identity / Archive / Conflict / Deploy / Reconcile / schema / UI / Proxy Resolver.
- Mark CLEAN or CLEANUP_COMPLETE.

---

## NEXT SINGLE HIGHEST PRIORITY

**P0-2 Runtime Verification:** restart the app and archive Workshop `3786388428`. Capture `[ARCHIVE_*]` proxy logs.

That same restart also serves **P0-4** (`[BACKUP_RESULT] reason=reconcile`). It does **not** wait on data cleanup.

Still waiting, independently: P0-3 scheme B authorization; P0-5 `--apply --yes` for the 7 URL scrubs; P0-1 production leftovers remain frozen.

Data hygiene next (later, not now): confirm the ~168 KB SAFE_DELETE set only — **never** the 100 GiB backup.
