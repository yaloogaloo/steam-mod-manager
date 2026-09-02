# P0 PRODUCTION REPAIR REPORT

**This document is a production report. It is not a test report.**

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

---

## Who applied (important)

The forensic session that produced `docs/P0_SYSTEM_FORENSICS.md` was instructed **not** to apply.

Production **was** mutated at **2026-09-02T10:10:49Z** by `apply_identity_repair` (parallel identity-repair conversation / CLI), evidenced by:

- `identity_repair_audit` rows with `operation=remove_invalid_duplicate` and `operation=scrub_url`
- Quarantine tree `data/identity_repair_quarantine/2026-09-02T101049+0000/`
- Preflight snapshot at 09:45Z still had `apply_executed: false`

Do not credit this forensic session with the apply. Do not hide the apply.

---

## BEFORE (09:45Z preflight, production not yet mutated)

Source: `data/identity_repair_preflight.json`

| Metric | Count |
|---|---|
| CRITICAL | 32 |
| HIGH | 9 |
| steam_internal ghosts | 13 |
| source_url_internal | 19 |
| duplicate_source_url groups | 9 |
| mods rows | 1788 |
| READY_FOR_APPLY | true |
| apply_executed | false |

Planned actions:

- 13 Steam ghosts → `REMOVE_INVALID_DUPLICATE` (not MERGE)
- 9 invalid duplicates → `REMOVE_INVALID_DUPLICATE` (not MARK_CONFLICT)
- 19 polluted `source_url` → `SCRUB_POLLUTING_SOURCE_URL`

Backup taken 09:46:31Z: `data/identity_repair_production_backup/20260902T094631Z/` (DB copy + robocopy of `mod/`).

---

## ACTION (10:10:49Z)

| Action | Count | Meaning |
|---|---|---|
| `remove_invalid_duplicate` / `REMOVE_INVALID_DUPLICATE` | 22 | 13 ghosts + 9 duplicates |
| `scrub_url` / `SCRUB_POLLUTING_SOURCE_URL` | 19 | clear polluting Steam URLs |
| MERGE ghost into canonical | 0 | not used |
| MARK_CONFLICT | 0 | not used |

Filesystem: unique ghost folders moved to quarantine (example `invalid_9000000000003438/Unknown Mod 3591453758`). Shared-folder duplicates were DB-only deletes.

References: migrated toward canonical per repair code; remaining ghost id hits are **audit history only**.

---

## AFTER (read-only verification 10:13–10:20Z)

### 13 Steam ghosts

| Check | Result |
|---|---|
| `mods` rows `9000000000003438`–`3450` | **absent** (integer range count = 0) |
| Active library `mod/逃离鸭科夫/Unknown Mod <canonical>` | **absent** |
| Quarantine folders for all 13 | **present** |
| Canonical Steam 13 rows | **present**, workshop ids unchanged |
| Live FK-like refs in `mods`/`mod_tags`/`mod_relations` | **none found** |
| Refs in `identity_audit_log` / `identity_repair_audit` | **present** (history) |

**Wording:** production DB rows for the 13 ghosts are gone. Filesystem copies exist in **quarantine**. This is not “deleted from the universe”. This is not PRODUCTION_DELETED_AND_VERIFIED.

### 9 invalid duplicates

All nine ids from preflight (`0349, 0351, 3226, 3227, 3228, 3229, 3230, 3232, 3251`) have `mods` count 0. Quarantine entries exist (shared-folder cases may be DB-only).

### 19 URL scrubs

Those 19 were applied. **Seven additional** internal Steam URLs were **not** in that set and **remain**:

| mod_id | title | leftover source_url |
|---|---|---|
| 9000000000000354 | 天极鲸替换奥沧鲸 | Steam URL(internal) |
| 9000000000000360 | 全产业模板 | Steam URL(internal) |
| 9000000000000361 | 全【区域】【居民区】蓝图 | Steam URL(internal) |
| 9000000000000362 | 游戏初期的【布局模板】 | Steam URL(internal) |
| 9000000000003031 | 壮丽艺廊添加卡片(免费版) | Steam URL(internal) |
| 9000000000003054 | 更好的角色装备展示界面 | Steam URL(internal) |
| 9000000000003225 | [ZIMIK]2b | Steam URL(internal) |

`900089445` looks similar in a LIKE query but is a **real Steam workshop id**, not an internal PK. Do not scrub it as pollution.

### Post-apply readonly identity plan

Command: `python services/identity_repair.py --audit` (sqlite `mode=ro`).

```
CRITICAL: 7
HIGH: 0
steam+internal=0
duplicate URL groups=0
Invalid duplicates to remove: 0
Polluting URL scrubs: 7
Identity conflicts: 0
```

Required bar was CRITICAL=0 HIGH=0 REQUIRES_REVIEW=0. **Not met.**

### Create-after-apply (regression evidence)

At 10:15:05Z the DB allocated:

- `9000000000003451` Empty Mod ee0974ce (Nexus / 巫师三)
- `9000000000003452` Empty Mod b6f2547e

Repair did not allocate (`repair_no_allocate_scope`). Some other lifecycle path did. System constraint “no mint outside IdentityService import” is **not** production-verified.

---

## VERIFIED checklist

| Requirement | Result |
|---|---|
| 13 ghost DB rows absent | true |
| 9 duplicate DB rows absent | true |
| 22 invalid folders absent from **active** library | true (moved/quarantined) |
| Canonical entities still present | true |
| Canonical Steam identity not rewritten to ghost ids | true (spot-check 13 workshop rows) |
| Reference graph no live dangling ghost PK | true for mods/tags/relations |
| Audit history still names ghost ids | true (allowed) |
| CRITICAL = 0 | **false (7)** |
| HIGH = 0 | true |
| Sidecar pollution gone | **false** (0362 sidecar `url`/`published_file_id`) |
| No new internal ids after apply | **false (3451, 3452)** |

**Final production label: APPLY_UNVERIFIED.**  
Do not write FIXED. Do not write PRODUCTION_DELETED_AND_VERIFIED.

---

## BEFORE / ACTION / AFTER / VERIFIED (compact)

```
BEFORE:
  invalid_ghost_rows=13
  invalid_duplicate_rows=9
  polluting_source_url=19
  CRITICAL=32 HIGH=9

ACTION:
  deleted_mods_rows=22
  scrubbed_source_url=19
  quarantined_unique_folders=yes
  merge=0 mark_conflict=0

AFTER:
  ghost_rows=0
  duplicate_rows=0
  CRITICAL=7 HIGH=0
  leftover_internal_source_url=7
  new_internal_ids=2

VERIFIED:
  db_rows_absent_22=true
  active_library_ghost_folders_absent=true
  filesystem_quarantine_present=true
  references_live_valid=true
  canonical_untouched=true
  post_audit_critical_zero=false
  → APPLY_UNVERIFIED
```

---

## What remains for a future gated apply

1. Scrub the 7 leftover `source_url` values (clear only; no Steam URL rewrite).
2. Scrub matching sidecar `url` / illegal `published_file_id`.
3. Persist real Anno `workspace_id` from sidecar to DB for `0362`.
4. Policy for Empty Mod 3451/3452.
5. Re-run `--audit` until CRITICAL=0 HIGH=0, then fill verification_status.

Do not apply those steps from a forensics-only mandate without an explicit apply request and a fresh backup.
