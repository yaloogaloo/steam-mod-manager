# P0 SYSTEM FORENSICS

**Generated:** 2026-09-02T10:20:00Z  
**Mode:** static audit + call graph + read-only SQLite + filesystem read-only + targeted environment probes  
**This session did not apply production repair.**  
**Evidence dumps:** `data/p0_forensics_snapshot.json`, `data/p0_forensics_followup.json`, `data/p0_forensics_verify.json`, `data/p0_post_apply_readonly_audit.json`

---

## Fact-state (mandatory)

| Surface | Status |
|---|---|
| `code_status` | CODE_IMPLEMENTED for identity repair planner/apply; archive/deploy/conflict observability incomplete |
| `test_status` | TESTED for identity repair (prior session: 31 tests). This forensic session did not re-run the suite |
| `plan_status` | DRY_RUN_PLANNED at 09:45Z (`data/identity_repair_preflight.json`, READY_FOR_APPLY=true) |
| `apply_status` | APPLY_EXECUTED at 10:10:49Z by a **parallel identity-repair session**, not by this forensic session |
| `production_status` | 13 ghost + 9 duplicate **mods rows absent**; folders **quarantined** (not destroyed); 7 CRITICAL `source_url` remain |
| `verification_status` | **APPLY_UNVERIFIED** — post-apply readonly audit is CRITICAL=7, HIGH=0. Not PRODUCTION_VERIFIED |

Do not read “quarantine” as “deleted from disk forever”.  
Do not read “audit log row” as “mods row still exists”.  
Do not read “31 tests passed” as “production closed”.

---

## Executive answers (section 24)

### Archive (`3786388428` Duck Tracks)

**Answer: F. 多因素.**

Not “reinstall broke archive code”. Not “Steam changed the HTML”. Not a single-line application bug that timeouts by itself.

Measured:

| Check | Result |
|---|---|
| curl_cffi | 0.16.2 in `D:\project\steam-mod-manager\.venv` |
| impersonate | `chrome131` (`services/archive.py`) |
| QSettings `network/proxy_url` | **empty** |
| process proxy env | all empty |
| SOCKS5 `127.0.0.1:7897` | **not listening** (connect timeout) |
| DNS `steamcommunity.com` | `108.160.165.8` (Facebook/WhatsApp ASN) + IPv6 `2a03:2880:f131:…` (Facebook). Not a Valve edge |
| TCP `:443` to that A record | timeout 5s |
| curl_cffi GET, impersonate chrome131, 8s, direct | `curl: (28)` at 8017ms |
| Stub on disk | 1322 bytes, embeds curl:(28) 15006ms, workshop id **3786388428** (canonical, not a new identity) |
| `offline_status` | `failed` |

Classification mix:

- **ENVIRONMENT_FAILURE / CONFIG_FAILURE:** reinstall dropped Clash/proxy; QSettings empty; GUI logs still mention `E:\project\steam-mod-manager` Python 3.11.4 while this tree uses `D:\` Python 3.11.9.
- **NETWORK_FAILURE:** DNS does not reach Steam; TCP/TLS never starts.
- **APPLICATION_FAILURE (observability + fallback policy):** timeout → `write_fallback_page` stub; logs are `Live Workshop archive failed… writing minimal stub`; **no** `[ARCHIVE_START]` / `[ARCHIVE_CONNECT]` / `[ARCHIVE_FAILURE]` chain. Archive does **not** allocate a new `mod_id`.

HTTP connection timeout is **not** an archive-logic identity bug.

### Conflict (`17863521013284165`)

**Answer: FILE_OVERWRITE on Anno stamps. Not identity conflict. Rule matches paths. UI/product presentation is misleading.**

User-cited id is **sidecar `workspace_id`**, not `mods.mod_id`.

| Field | Value |
|---|---|
| User id | `17863521013284165` |
| Where it lives | `mod/Anno 1800/游戏初期的【布局模板】/.info/metadata.json` `workspace_id` |
| DB `mods.workspace_id` | **empty** (sidecar/DB drift) |
| Internal `mod_id` | `9000000000000362` |
| Producer | `ConflictDetector.check_all_mods` / post-deploy `_schedule_post_deploy_conflict_scan` |
| Rule | identical `Path.resolve()` deploy target claimed by ≥2 **enabled** mods → `ConflictType.FILE_OVERWRITE` |
| Partner | `9000000000000360` 「全产业模板」 |
| Matched files | **141** overlapping stamp targets under `Documents\Anno 1800\stamps\…` |
| `conflict_note` | garbled basename `┼Σ╠╫í╛… ← 9000000000000360, 9000000000000362 等 141 处` |
| Identity conflict? | **No.** `identity_repair` ACTION_CONFLICT was not used |

**Misjudgment vs correct:**

- Path rule is **correct**: both packs write into the **shared** Anno stamps tree; 141 resolved targets are identical.
- Product/UI is **wrong for the user**: badge is `[Conflict] 冲突`; no type/rule/partner/path list unless the user opens a secondary detail. Partner row `0360` is `conflict_status=none` (asymmetric persist). Stamp encoding is mojibake. User identified the mod by workspace id, which the DB no longer stores.

This is **DEPLOYMENT / FILE_OVERWRITE**, not IDENTITY_CONFLICT.

### Deploy (KB-class / minutes)

**Answer: no production per-stage log for the slow run. Isolated copy/hash of the 156KB stamp zip is milliseconds. Wall-clock minutes coincide with a 698s full-library robocopy. Observability cannot currently answer “which stage” from user logs.**

Today’s production deploys:

| Time (UTC) | mod_id | Title | Target |
|---|---|---|---|
| 09:41:55 | 9000000000000345 | 城市变化 | `Anno 1800\mods` |
| 09:50:39 | 9000000000001030 | stamps / 全产业布局 | `Documents\Anno 1800\stamps` |
| 09:51:24 | 9000000000000362 | 游戏初期的【布局模板】 | `Documents\Anno 1800\stamps` |

Concurrent: identity-repair **production backup robocopy** of `mod/` (≈300k files, 698s, 09:46–09:58). Stamps deploys overlap that IO storm.

Isolated timings (this session, read-only, idle disk):

| Measurement | Result |
|---|---|
| Layout zip | 159 637 bytes, 167 manifest files |
| SHA256 zip once | 8.7 ms |
| SHA256 zip 167 times (current persist loop) | 16.9 ms |
| Walk 113 manifests / 10 010 targets + `Path.resolve` | **3054 ms** |
| Smoke Palworld 256KB (prior fixture) | 132.5 ms total |

Without `[DEPLOY_STAGE] elapsed_ms` from the 09:51 run, **do not invent** “copy took 5 minutes”. Ranked causes with evidence:

1. **Disk contention** from full-library backup robocopy (environment, timed).
2. **Anno stamps extract + backup of existing overlapping stamp files** (141 overlaps; each backup SHA256s source and dest).
3. **Library-wide conflict scan** (~3s idle; worse under antivirus/IO).
4. **Persist hashing the same zip once per stamp entry** (cheap here: 17ms).
5. Unlikely: copying 156KB with empty target and no overlap.

### Identity (ghosts `9000000000003438`…`3450`)

**Answer: sequential internal allocations `NON_STEAM_MOD_ID_BASE + 3438…3450`, then Steam-field pollution. Historical mint path is old allocate-on-unresolved. Current `ensure_mod_identity` no longer allocates. Other create paths still exist; two new internals `3451`/`3452` were minted at 10:15Z after apply.**

### Production (13+9)

**This forensic session did not run `--apply --yes`.**

A parallel session **did** apply at `2026-09-02T10:10:49Z`:

- ACTION: `REMOVE_INVALID_DUPLICATE` ×22 (13 ghosts + 9 duplicates), `SCRUB_POLLUTING_SOURCE_URL` ×19
- AFTER: those 22 `mods` rows **absent**; unique folders **quarantined** under `data/identity_repair_quarantine/2026-09-02T101049+0000/`
- Canonical 13 Steam rows **present**, workshop ids unchanged
- Remaining refs to ghost ids: **audit tables only** (`identity_audit_log`, `identity_repair_audit`) — expected history, not live entities
- Post-apply readonly plan: **CRITICAL=7 HIGH=0** leftover internal Steam URLs (including the conflicted Anno layout mod)

**Not PRODUCTION_DELETED_AND_VERIFIED.** Closest honest label: **APPLY_EXECUTED + PARTIAL_ROW_ABSENCE + APPLY_UNVERIFIED**.

---

## 1. Archive root cause

### Call graph

```
UI offline / sync enrich
  → services/offline/steam.py SteamOfflineProvider.update_offline_page
  → services/archive.py OfflinePageArchiver.ensure_offline_page
       → archive() → _archive_body
       → WORKSHOP_PAGE_URL.format(id=published_file_id)
       → _fetch_main_html (up to 3 attempts)
       → _http_get → curl_cffi Session GET impersonate=chrome131 timeout=15
       → on failure and no valid page: write_fallback_page (stub)
```

`services/importers/archive.py` is zip/rar import. It is not this path.

### Why reinstall correlates

- Sidecar `cover_path` still points at `E:\project\steam-mod-manager\mod\逃离鸭科夫\Unknown Mod 3786388428\...`
- `data/gui_runtime_trace.log` records `E:\...\.venv` Python 3.11.4
- This workspace is `D:\` Python 3.11.9
- QSettings org/app `SteamModManager/WorkshopLibrary` has **no** `network/proxy_url`
- Local SOCKS5 7897 (documented working path in `docs/ARCHIVE_FAILURE_DIAGNOSIS.md`) is down

Reinstall explains **lost proxy + DNS/VPN posture + path split**. It does not change `DEFAULT_TIMEOUT=15` or impersonate.

### Stub policy

Stub is written when live fetch fails **and** no successful existing page. It uses the workshop id already on the entity. It does not call `allocate_mod_id`.

### Log gap vs required

Present: `[STEAM ARCHIVE] HTTP request…`, `Live Workshop archive failed… writing minimal stub`.  
Absent: `[ARCHIVE_START]`, `[ARCHIVE_CONNECT]`, `[ARCHIVE_RESPONSE]`, `[ARCHIVE_RETRY]`, `[ARCHIVE_FAILURE]`, `[ARCHIVE_FALLBACK]`, `[ARCHIVE_STUB]`.

---

## 2. Conflict root cause

### Identity of the cited object

`17863521013284165` is an Anno/Ubisoft **workspace** token on a **local/other** blueprint pack. It is not a Steam workshop id and not the SQLite PK.

Sidecar also contains identity pollution:

- `published_file_id`: `9000000000000362` (internal)
- `url`: `https://steamcommunity.com/sharedfiles/filedetails/?id=9000000000000362`

DB after apply still has that polluted `source_url` (one of the 7 leftover CRITICAL scrubs).

### ConflictDecisionTrace (reconstructed; runtime did not persist a trace object)

```text
timestamp          : 2026-09-02T10:10:49Z (mods.updated_at / last conflict persist)
mod_id             : 9000000000000362
user_visible_id    : workspace_id 17863521013284165 (sidecar only)
conflict_type      : FILE_OVERWRITE
rule               : ConflictDetector — same resolved target, enabled mods, deploy_manifest.json
candidate_mod_id   : 9000000000000360
candidate_path     : D:\project\steam-mod-manager\mod\Anno 1800\全产业模板
matched_file       : Documents\Anno 1800\stamps\Enbesa\<mojibake>\…  (141 paths)
matched_rule       : Path.resolve() equality
decision           : conflict_status=conflict
reason             : 141 overlapping stamp targets
persist_asymmetry  : partner 0360 conflict_status=none
```

Producers (production):

| Producer | File | Writes `conflict_status`? |
|---|---|---|
| `ConflictDetector.check_all_mods` | `services/conflict.py:135` | Yes |
| Post-deploy async scan | `services/deploy.py:805-827`, `1857-1862` | Yes |
| Manual UI mark | `ui/mod_detail_panel.py` | Yes |
| Tag relations `set_mod_conflict_targets` | `core/db_manager.py:3830` | **No** (tags only) |
| Identity repair `ACTION_CONFLICT` | `services/identity_repair.py:1951` | Yes (`identity_conflict`, then normalized) |

UI (`mod_detail_panel.py:3677-3682`) maps status to the word **冲突**. It does not print FILE_OVERWRITE / partner / rule.

`ConflictType.PAK_OVERLAP` is never generated.

---

## 3. Deploy performance root cause

### Stage map (`ModDeployer._deploy_with_context`)

| Stage | Location | Scans | Hash | Can dominate a small payload? |
|---|---|---|---|---|
| lock / gates | `deploy.py` ~1289 | DB | no | no |
| `prepare_archives` | ~1077 | extract | no | yes (RAR/7z) |
| `verify_source` | ~1581 | `safe_iter_files` | no | large trees |
| **deps recursive deploy** | ~1421 | nested full deploy | — | **yes** |
| `plan` | ~1617 | Anno archive **extracts again to temp** (`deploy_rules/anno.py:366`) | no | yes for archives |
| conflict preview | ~1638 | **all library manifests** | no | yes at 10k targets |
| `backup` | ~1681 | existing planned targets | **SHA256 twice per file** | yes if stamps already present |
| `copy` / strategy.deploy | ~1695 | extract/copy | no | extract cost, not 3KB |
| `validate` | ~1739 | exist+size | no | no |
| `persist` | ~1783 | fingerprint + **SHA256 each source path** | yes | 167× same zip = 17ms here |
| post-deploy conflict | async | all manifests again | no | ~3s idle; does not block SUCCESS |

Existing logs: `[DEPLOY_STAGE] stage=… event=started|finished elapsed_ms=…` and `[DEPLOY_SLOW]` ≥1000ms (`services/deploy_stage_log.py`). Missing from user-facing completeness: `[DEPLOY_START]` with source/target/files/bytes, `[DEPLOY_RESULT]` with error_code/stage on failure.

### Why minutes can happen without a 3KB copy

The 09:51 layout deploy is a **167-file stamp unpack from a 156KB zip into a shared stamps tree**, not a 3KB single file. It ran while robocopy hashed/copied the entire library. Windows Defender + disk queue can stretch backup/extract/scan into minutes even when CPU-side hashing is 17ms.

---

## 4. Identity root cause

### What the 13 ghosts were

| Ghost | Canonical Steam | Folder leftover |
|---|---|---|
| 9000000000003438 | 3591453758 | Unknown Mod 3591453758 |
| 9000000000003439 | 3592539424 | Unknown Mod 3592539424 |
| … | … | … |
| 9000000000003446 | **3786388428 Duck Tracks** | Unknown Mod 3786388428 |
| … | … | … |
| 9000000000003450 | 3790849356 | Unknown Mod 3790849356 |

IDs are `9_000_000_000_000_000 + 3438…3450`. They **cannot** come from current `upsert_mod` (rejects `mod_id >= BASE`).

Strongest historical chain:

1. Folder/import lacked a recoverable workshop identity.
2. Old `ensure_mod_identity` / reconcile **allocated** an internal PK (today’s function documents this as the bug it no longer does).
3. Sidecar/`published_file_id`/`workspace_id`/`source_url` copied the internal id into Steam-shaped fields.
4. Title/folder `Unknown Mod <workshop_id>` while PK was 900….

Offsets 3438–3450 imply thousands of prior successful internal allocations in this DB.

### Remaining create paths **today** (after governance work)

| Path | Can CREATE? | Notes |
|---|---|---|
| `identity_service.allocate_internal_id` → `allocate_mod_id` | YES | **sole** `allocate_mod_id` production caller |
| `create_mod_identity` (importers + **reconcile**) | YES | intended import; reconcile is a lifecycle violation vs CREATE=NO |
| `upsert_mod` / `upsert_mods` | YES | Steam workshop PK |
| `_ensure_mod_stub` / `update_mod_deploy_status` INSERT | YES for Steam-range missing rows | CREATE=NO leak |
| `apply_sidecar_to_db` | NO | refuses missing row |
| `ensure_mod_identity` | NO | unresolved stays unresolved |
| identity repair apply | NO | `repair_no_allocate_scope` |

**Live proof the funnel is not closed:** at 10:15:05Z, after ghost apply, the DB minted:

- `9000000000003451` title `Empty Mod ee0974ce` (Nexus, 巫师三)
- `9000000000003452` title `Empty Mod b6f2547e`

`allocate_mod_id` caller count in production = **1** (`allocate_internal_id`). That does **not** mean create count = 1.

---

## 5. Lifecycle violations vs required matrix

| Operation | Required CREATE | Actual |
|---|---|---|
| Import | YES | YES via IdentityService |
| Refresh | NO | no allocate; may `upsert_mod` Steam PK |
| Offline Archive | NO | no allocate; may write stub HTML |
| Deploy | NO | Steam-range stub INSERT if row missing (**violation**) |
| Metadata Edit | NO | sidecar persist; apply_sidecar refuses create |
| Reconcile | NO blind mint | **YES** `create_mod_identity` when platform+url/ext present (**violation vs “no blind mint”**) |
| Repair | NO allocation | holds (`repair_no_allocate_scope`) |
| Uninstall | NO identity delete | deployment only |
| Rename | NO identity | folder/display only |

Principle still broken in code: **MISSING ≠ must not invent Steam identity**, but Steam-range `_ensure_mod_stub` still copies `external_id=workspace_id=mid` and `source_url=steam_workshop_url(mid)`.

---

## 6. Identity writers (production)

| Writer | File:anchor | Identity fields touched |
|---|---|---|
| `upsert_mod` / `upsert_mods` | `core/db_manager.py:1177-1284` | PK=workshop, external_id, workspace_id, source_url Steam URL |
| `_ensure_mod_stub` | `db_manager.py:4106` | same for Steam-range; stub:internal otherwise |
| `update_mod_deploy_status` INSERT | `db_manager.py:2934` | platform=steam, ids=mid |
| `create_mod_identity` / authority | `identity_service.py:231`, `mod_identity_authority.py:165` | create/bind |
| `persist_identity` / `bind_platform` | `identity_service.py` | update existing only |
| `persist_unified_metadata_dict` | `file_ops.py:665` | sidecar JSON only |
| `apply_sidecar_to_db` | `info_sidecar.py:430` | DB update, no create |
| `library_reconcile` | `library_reconcile.py:141-210` | create + sidecar |
| `metadata_refresh` | `metadata_refresh.py` | Steam upsert if workshop id resolved |
| `identity_repair` apply | `identity_repair.py` | delete/scrub/quarantine; no allocate |

---

## 7. Mod creation paths

1. User import (Steam/Nexus/Mod.io/GitHub/Other) → `create_mod_identity`
2. Steam catalog `upsert_mods` (`core/steam_api.py`)
3. Reconcile import branch
4. Reconcile orphan backup upsert (Steam-range folder names)
5. Incidental Steam-range stub on deploy/tag/status writes
6. Nexus “Empty Mod ********” materialize (observed 3451/3452)

---

## 8. Conflict producers

See §2 table. Automatic path conflict is the live Anno case. Identity conflict channel exists but was **not** applied to this mod (`ACTION_CONFLICT` count in repair audit = 0).

---

## 9. Deploy stages

See §3 table. User-visible gap: logs do not always include source path, target path, file count, bytes, and failing stage together.

---

## 10. Production data risks (open)

1. **7 CRITICAL** internal Steam `source_url` values remain (Anno layout packs + others). Sidecar `url`/`published_file_id` may still be polluted even when DB is scrubbed later.
2. **Sidecar vs DB workspace_id drift** on `0362` (sidecar has `17863521013284165`, DB empty) — user and detector use different ids.
3. **Anno stamps FILE_OVERWRITE** will recur for any two layout packs sharing the stamps tree.
4. **Create-after-repair:** Empty Mod 3451/3452 proves mint still happens outside a single governed import UX.
5. **19 `Unknown_Mod_*` titles** remain (Steam workshop PKs with unknown titles, plus Anno workspace-only stubs 3395–3404 with empty `last_known_path`).
6. **Dual Python/path environment** (`E:` vs `D:`) can make “it worked before reinstall” undiagnosable if the GUI is not the same venv.
7. **Quarantine ≠ purge:** ghosts exist under `data/identity_repair_quarantine/…`. Restore/copy-back can resurrect them if reconcile creates from leftovers.
8. **Report language risk:** `data/identity_repair_apply.json` (`applied: true`, `planned_actions=0`) is an **older** `mod_identity_repair` dump, not this apply. Treat it as misleading.

---

## Identity Invariant Matrix (audit snapshot)

| Invariant | Production now |
|---|---|
| Steam workshop id from official Steam identity | Ghosts gone; leftover URLs still embed internal ids on **non-Steam** rows |
| `workspace_id ≠ internal mod_id` unless workshop-validated | DB empty for 0362; sidecar still has Ubisoft workspace id (legal) vs internal published_file_id (illegal) |
| `published_file_id ≠ internal` for Steam | Sidecar of 0362 still uses internal as published_file_id |
| `source_url ≠ Steam URL(internal)` | **7 rows fail** |
| `allocate_mod_id` callers = 1 | **True** |
| No create from refresh/archive/deploy/sidecar | Deploy stub INSERT and reconcile create remain |
| Scanner `audit --readonly` | Partial (`identity_repair.py --audit`); not a full invariant matrix CLI |

---

## What this session did / did not do

**Did:** read-only DB (`mode=ro`), filesystem listing, DNS/TCP/HTTP probes, conflict manifest overlap count, hash simulation, identity-repair `--audit` (readonly).

**Did not:** `--apply --yes`, drop quarantine, change production semantics, UI redesign, deploy/archive rewrite.
