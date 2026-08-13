# Library Status Model Audit (Phase 5 Task 1)

Audit date: 2026-08-13  
Scope: how Library encodes **origin**, **lifecycle**, and **errors** today.  
Constraint: read-only; no code changes in this task.

## Verdict

**Yes — fields are mixed.** One UI-facing field (`library_status`) currently blends provenance and health, and `platform` / `source_type` are overloaded with both identity and display.

| Concern | Current storage | Mixed? |
|---------|-----------------|--------|
| 1. 来源属性 (where Mod came from / platform) | `mods.platform`, `.info` `source_type`, card `platform` badge | Partially — platform is origin *and* store identity |
| 2. 生命周期状态 (folder present / healthy) | `folder_present`, `library_status=missing\|normal\|imported` | **Yes** — `imported` is provenance, `missing` is health |
| 3. 错误状态 | `library_status=conflict\|backup_invalid`, `is_invalid`, `conflict_status` | Partially — conflicts live in two places |

## Field map

| Field | Layer | Role today | Problem |
|-------|-------|------------|---------|
| `mods.platform` | SQLite | Steam / Nexus / GitHub / mod.io | Used as “source” badge; no `external` / `local` |
| `.info` `source_type` | Portable | Same as platform | Resolver reads it; not sticky “how it entered the library” |
| `mods.library_status` | SQLite | `normal` / `missing` / `imported` / `conflict` / `backup_invalid` | **Mixes** provenance (`imported`) with health/errors |
| `mods.folder_present` | SQLite | Cache of disk presence | Correct health signal; duplicated by `library_status=missing` |
| `backup_status` | SQLite | Backup integrity | Correct error signal; also mirrored into `library_status` |
| Card `platform_badge` | UI | Top-right platform label | Source only |
| Card `missing_badge` | UI | Center overlay | Shows 内容缺失 **and** 外部导入 / 冲突 / 备份损坏 |

## Critical bug (provenance)

In `reconcile_library()`:

```text
had_row == False  → library_status = imported
had_row == True   → library_status = normal
```

So after the **first refresh**, `imported` is wiped. That is why “外部导入” does not survive repeated library refresh.

## Badge layout today

```text
Cover
├── category / state (left)
├── platform (top-right)     ← source
├── missing_badge (center)   ← status AND sometimes “外部导入”
└── relation / deploy
```

Source and status compete for the same mental model.

## Recommended split (Phase 5 Tasks 2–4)

| Concept | Proposed field | Values |
|---------|----------------|--------|
| 来源 | `source_type` (sticky) | `steam` `nexus` `modio` `github` `external` `local` `unknown` |
| 内容状态 | `content_status` | `healthy` `folder_missing` `content_missing` `metadata_missing` `backup_invalid` `identity_conflict` |
| 游戏 | `game_status` | `healthy` `missing_folder` |

Rules:

- `source_type` set once (or only by explicit import/refresh pipelines); **never** cleared by reconcile refresh.
- `content_status` recomputed every reconcile from disk + backup validity.
- UI: row1 = source badge, row2 = status badge.

## Out of scope

- Resolver priority
- Backup write direction
- Deploy / Import Pipeline architecture
