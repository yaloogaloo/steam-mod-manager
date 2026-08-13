# Metadata Backup Lifecycle Audit (Phase 3-A Task 1)

Audit date: 2026-08-12  
Scope: write paths that create or update Mod identity metadata, and whether they sync `data/mod_backup/<id>`.  
Constraint: unidirectional `.info` → backup → UI. Backup must never write `.info`.

## Summary table

| 入口 | 当前行为 | 是否同步 backup | 问题 |
|------|----------|-----------------|------|
| Import | `materialize_imported_mod` writes `.info` (sidecar + cover), then calls `sync_metadata_backup(dest)` | 是 | 调用分散；未统一 reason / 完整性校验 |
| Steam refresh | `metadata_refresh` → `ModFileManager.save_metadata`（写 `.info`，**不**自动 sync）→ 显式 `sync_metadata_backup` | 是 | `save_metadata` 与 `persist_unified_metadata_dict` 行为不一致；双路径易漏 sync |
| Mod.io refresh | `modio_metadata_refresh` 写 `.info` + cover 后显式 `sync_metadata_backup` | 是 | 同上，分散调用 |
| 编辑 metadata（Detail） | Detail Panel 改字段 → `persist_unified_metadata_dict` / `write_sidecar_for_mod` | 是（经 persist） | 无统一 reason；无 validate |
| 编辑 metadata（ModEditDialog） | 仅写 SQLite 用户字段（display_name / notes / favorite），**不写** `.info` | 否 | 符合「用户态在 SQLite」；不构成 `.info`→backup 路径；勿从 dialog 反写 `.info` |
| 修改封面 | `apply_cover_to_mod` 安装 `.info/cover.*`，可选 `save_metadata`，再 `sync_metadata_backup` | 是 | 分散调用 |
| 保存离线页 | `attach_nexus_offline_page` / providers 写入 `.info/offline/` 后 sync | 是（Nexus attach 路径） | Steam/Mod.io/GitHub provider 路径需确认均走到统一入口 |
| Rescan | `rescan_mod_folder` → `write_sidecar_for_mod` → `sync_metadata_backup` | 是 | `write_sidecar` 已经 persist sync 一次，rescan 再 sync 一次（冗余但无害） |
| Resolver（Detail 打开） | `resolve_mod_metadata(..., sync_backup=True)` 副作用 sync | 是（机会性） | 读路径副作用；应逐步收敛到写事件触发 |

## Per-entry detail

### Import

- **Files:** `services/importers/materialize.py`, `services/importers/image_picker.py`, `ui/import_thread.py` (offline attach)
- **Flow:** materialize → write sidecar / cover → `sync_metadata_backup(dest)`; optional Nexus offline → `attach_nexus_offline_page` → sync again
- **Backup sync:** yes
- **Issues:** multiple direct `sync_metadata_backup` calls; no status field update

### Steam refresh

- **Files:** `services/metadata_refresh.py`
- **Flow:** fetch → rename folder if needed → `mgr.save_metadata` (writes `.info` via `_write_unified_metadata` **without** sync) → `sync_metadata_backup(new_path)`
- **Backup sync:** yes (explicit)
- **Issues:** `save_metadata` bypasses `persist_unified_metadata_dict`; any future caller of `save_metadata` alone will skip backup

### Mod.io refresh

- **Files:** `services/modio_metadata_refresh.py`
- **Flow:** update `.info` + cover via `apply_cover_to_mod` (already syncs) + final `sync_metadata_backup`
- **Backup sync:** yes (possibly double)
- **Issues:** same fragmentation

### Edit metadata

| Path | Writes `.info`? | Syncs backup? |
|------|-----------------|---------------|
| Detail Panel field edits | yes (`persist_unified_metadata_dict`) | yes |
| `write_sidecar_for_mod` / `save_info_sidecar` | yes | yes (via persist) |
| `ModEditDialog` | no (SQLite only) | no |

**Issue:** Dialog is intentionally user-state only. Do not teach backup from SQLite user fields. Identity edits that matter for backup must go through `.info` writers.

### Cover change

- **Files:** `services/importers/image_picker.py` (`apply_cover_to_mod`)
- **Flow:** install cover under `.info/` → optional `save_metadata` → `sync_metadata_backup`
- **Backup sync:** yes
- **Note:** `save_metadata` does not sync; cover function adds sync afterwards

### Offline page save

- **Files:** `services/offline/manager.py` (`attach_nexus_offline_page`), provider modules
- **Flow:** write `.info/offline/` + metadata pointers → Nexus attach syncs backup
- **Backup sync:** yes for Nexus attach wrapper; other providers should be audited to call the same unified entry after `.info` update

### Rescan

- **Files:** `services/info_sidecar.py` (`rescan_mod_folder`)
- **Flow:** merge file roles → `write_sidecar_for_mod` (persist→sync) → explicit `sync_metadata_backup` again
- **Backup sync:** yes (redundant)

### Other writers

| Writer | Sync? |
|--------|-------|
| `persist_unified_metadata_dict` | yes |
| `ModFileManager.save_metadata` | **no** (callers must sync) |
| Resolver `sync_backup=True` | yes (read-side side effect) |
| `restore_check` | yes via `sync_metadata_backup` |

## Directionality check

| Operation | Direction | Allowed? |
|-----------|-----------|----------|
| `snapshot_from_mod_folder` | `.info` → `data/mod_backup` | yes |
| `sync_metadata_backup` when folder missing | mark `folder_present=0` only | yes |
| Backup → write `.info` | — | **not present** (good) |
| Resolver when folder exists | prefer `.info`, then backup | unchanged; Phase 3-A must not invert |

## Gaps for Phase 3-A

1. No single `sync_after_metadata_change(mod_id, managed_path, reason)` entry
2. No `validate_backup` / integrity status
3. No startup backfill for mods with `.info` but missing `data/mod_backup/<id>`
4. SQLite lacks `backup_status` / `backup_last_validate_at`
5. `save_metadata` does not participate in the persist→sync contract (call-site dependent)

## Out of scope (explicit)

- Resolver priority changes
- UI display logic
- Deploy / import pipeline redesign
- Teaching `.info` from backup
