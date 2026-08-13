# Metadata Resolver Side-Effect Audit (Phase 3-B Task 1)

Audit date: 2026-08-13  
Scope: identify any write I/O triggered by metadata **read** paths.  
Constraint for Phase 3-B: Resolver must become pure-read; backup sync only on write events.

## Summary table

| 调用位置 | 当前行为 | 是否产生 I/O | 是否应该保留 |
|----------|----------|-------------:|--------------|
| Resolver `resolve_mod_metadata(..., sync_backup=True)` | 目录存在时调用 `sync_after_metadata_change(..., "rescan")` → 复制 cover/offline、写 backup metadata、更新 SQLite backup 状态 | **是（写）** | **否** — 必须删除 |
| Resolver `resolve_cover_path` / `resolve_offline_page` | 内部 `resolve(..., sync_backup=False)`，只读路径 | 否（读） | 是 |
| Resolver `list_visible_mods` | 默认 `sync_backup=False` | 否 | 是（去掉参数后仍只读） |
| Detail Panel `show_mod` | `resolve_mod_metadata(..., sync_backup=True)` → 打开详情即同步 backup | **是（写）** | **否** |
| Detail Dialog | 同上 `sync_backup=True` | **是（写）** | **否** |
| Library / Card | 已传 `sync_backup=False` | 否 | 是（去掉参数） |
| Cover Loader | 走 Resolver cover API | 否 | 是 |
| Offline Viewer / Search | 走 Resolver offline / list | 否 | 是 |
| `persist_unified_metadata_dict` | 写 `.info` 后 `sync_after_metadata_change(..., "edit")` | 是（写，正确） | 是 |
| `ModFileManager.save_metadata` | 只写 `.info`，**不** sync | 是（写 .info） | 应改为写后一次 sync |
| Refresh / materialize / cover / offline / rescan | 多处显式 sync；部分路径与 persist/save 重复 | 是 | 收口为「一写一 sync」 |

## Resolver APIs — implicit writes?

| API | 隐式写？ | 说明 |
|-----|---------|------|
| `resolve_mod_metadata` | **是**（默认 `sync_backup=True`） | 唯一写副作用入口 |
| `resolve_cover_path` | 否 | `sync_backup=False` |
| `resolve_offline_page` | 否 | `sync_backup=False` |

Resolver 本身无 `write_text` / `copytree` / `mkdir`；写操作全部经 `sync_after_metadata_change` → `sync_metadata_backup` → `snapshot_from_mod_folder`。

## Write-path duplicate sync (pre-fix)

| 路径 | 重复来源 |
|------|----------|
| Import `materialize` | `save_metadata`（无 sync）→ `apply_cover`（sync）→ `write_sidecar`→`persist`（sync）→`apply_missing`→`persist`（sync）→ 末尾再 sync |
| Steam refresh | `save_metadata`（无 sync）→ 显式 `sync_after(..., "refresh")` |
| Mod.io refresh | cover sync + 末尾 refresh sync |
| Rescan | `write_sidecar`→persist sync + 末尾 rescan sync |
| Edit (Detail) | `persist_unified_metadata_dict` 已 sync（正确） |
| ModEditDialog | 仅 SQLite 用户字段，不 sync（正确） |

## Directionality

- Backup → `.info`：未发现
- 打开详情触发 backup 写：存在（本阶段必须消除）

## Phase 3-B actions

1. 删除 Resolver / Detail 的 `sync_backup` 写副作用
2. `save_metadata` → 写 `.info` + 一次 sync；上层去掉重复 sync
3. 增加 `rebuild_metadata_backup(..., reason="repair")`
4. Snapshot 幂等（sha256）+ `.info` 删除 asset 时镜像删除 backup asset
