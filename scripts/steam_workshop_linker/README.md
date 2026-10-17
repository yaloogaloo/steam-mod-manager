# Steam Workshop Linker

独立运维工具：把 Steam Workshop 目录变成指向 **SMM Mod 目录** 的 Windows Junction。

不接入 Steam Mod Manager 主程序，不改 `core/` / `services/` / `ui/` / DB schema / 部署 / 导入逻辑。本工具只用 SQLite **只读**查询 `data/mod_manager.db`，从不写入数据库。

## 目标

```text
Workshop workspace_id
        ↓
SQLite  platform='steam' AND app_id AND workspace_id
        ↓
已注册 → last_known_path → .info 校验 → fallback 扫描
未注册 → 完全忽略
        ↓
Workshop ── Junction ──> SMM Mod Directory（唯一真实存储）
```

未注册 Workshop 文件夹：不删除、不建 Junction、不导入、不改库。

## 注册判断（数据库，不是目录扫描）

Workshop root 的**直接子目录名**就是 `workspace_id`（必须是纯十进制数字）。

查询：

```sql
SELECT ... FROM mods
WHERE platform = 'steam'
  AND app_id = ?
  AND workspace_id = ?
```

- 0 行 → `UNREGISTERED + IGNORE`
- 1 行 → 已注册，解析 SMM 路径
- \>1 行 → `AMBIGUOUS_REGISTRATION`，跳过，不猜测

`metadata.source_type` / `platform` / `source` **不用于**是否注册。

SMM 路径：`mods.last_known_path` 必须存在、是普通目录、在 `smm_mod_root` 下、`.info/metadata.json` 的 `workspace_id` 一致。失败则扫描 `smm_mod_root` 直接子目录的 metadata。`internal_id` 若在磁盘与 DB 都存在，必须一致。

## 用法

默认 **dry-run**：

```powershell
python scripts/steam_workshop_linker/sync.py
python scripts/steam_workshop_linker/sync.py --app-id 262060 --dry-run
```

真正执行：

```powershell
python scripts/steam_workshop_linker/sync.py --app-id 262060 --execute
```

可选 `--db` 指向只读 `mod_manager.db`（默认 `<project>/data/mod_manager.db`）。

`mapped_games.json` 只是历史记录，不会跳过实时检查。

## Steam 删除 Junction

删除 Workshop Junction ≠ 删除 SMM target。Steam 若重新写出真实 Workshop 目录，再次 `--execute` 会删掉那份副本并重建 Junction。本工具不监控、不修 Junction 生命周期以外的事。

## 测试

```powershell
python -m pytest scripts/steam_workshop_linker/tests -q
```
