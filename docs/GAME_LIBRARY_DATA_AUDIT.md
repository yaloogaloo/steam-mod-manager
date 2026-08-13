# Game Library Data Audit (Phase 6-A Task 1.1)

Audit date: 2026-08-13  
Scope: how the left **Game Library** list is populated, and how residual / polluted
entries appear.  
Constraint: **read-only** — no deletes in this task.

## Verdict

The sidebar merges **three sources** without a documented priority. That is why
test folders (`GameA`, `G`, `test_*`) and real missing games (`Anno 1800`) can
look the same in the UI.

Live sample (project `mod/` + `data/mod_manager.db`):

| Source | Observation |
|--------|-------------|
| Disk `mod/` | 6 real games: Anno 1800, Palworld, 文明Ⅵ, 星露谷物语, 暗黑地牢, 杀戮尖塔 |
| `games` table | 8 rows — includes pollution **`Game` (app_id=1)** and **战锤Ⅲ** (no disk folder) |
| `mods.last_known_path` parents | Includes **TestGame, G, Game, GameA, test_save_info_sidecar_preserv0** |
| `data/mod_backup/` | ~1114 backup dirs (mostly real; some tied to test mods) |

## 1. Game list sources (today)

Code path: `build_library_snapshot` → `_build_game_entries` → UI / `resolve_library_games`.

| Source | Mechanism | Role today |
|--------|-----------|------------|
| **Filesystem** | `ModFileManager.list_games()` under `mod/<Game>/` | Live library folders |
| **`games` table** | `list_games()` where `app_id > 0` | Configured Steam games + categories |
| **Backup / cards** | `list_visible_mods` → card `game_folder` from path or resolver; often from `last_known_path` | Keeps deleted game folders visible |

### Current merge order (Phase 5)

1. Insert all **`games` table** rows (folder = sanitized name)
2. Add **disk** folders not already present
3. Add leftover **card count keys** (backup-derived game folders)

This is **not** the desired Phase 6 priority (disk > backup history > games table).

### Desired priority (Phase 6 Part 3)

```text
真实游戏目录 (filesystem)
    >
历史路径 / backup (last_known_path → parent game folder)
    >
games 表
```

Missing on-disk folders that still have backup/mod rows must remain visible with
`game_status=missing_folder`.

## 2. Residual types

### A. User-real missing games — **must keep**

Example pattern:

- Game folder was `mod/Anno1800` (or `Anno 1800`)
- Directory removed
- `mods.last_known_path` / `mod_backup/<id>/` still present
- Sidebar should show ⚠ + metadata still browsable

Do **not** delete these in maintenance cleanup.

### B. Orphan garbage — **safe to flag** (not auto-delete)

Examples:

- `games` row with no mods, no backup, no disk folder
- Backup directory with no SQLite `mods` row and no recoverable metadata link
- Empty disk folder with zero mods and zero backup

Report only in `scan_library_issues()`; cleanup script must be explicit + dry-run.

### C. Test pollution — **report, never auto-delete in product**

Observed sticky parents / names:

| Name | Likely origin |
|------|----------------|
| `G` | Short fixture folder |
| `Game` | `games.app_id=1` + last_known_path |
| `GameA` | pytest library fixtures |
| `TestGame` | ad-hoc test |
| `test_save_info_sidecar_preserv0` | truncated test folder name |

Also: any folder / game_name matching `test_*` or exact `GameA` / `GameB`.

Dev-only cleanup: `scripts/cleanup_test_library.py` (dry-run default).

## 3. UI IA gap (feeds Part 2)

Today `_GameFilterRow` uses one object name (`gameFilterRow`) and one name style
(`gameListName`) for **both** game and category rows. Indent alone is not enough
to tell “杀戮尖塔” from “事件”.

## 4. Out of scope (do not change)

- Resolver priority / backup write direction
- Import / deploy pipelines
- Mod `source_type` sticky / `content_status` computation
- Auto-deletion of Type A missing games
