# Identity Runtime Boundary Audit

Date: 2026-09-15  
Scope: white-box scan of current `ui/`, `services/`, `core/` (models live under `core/models.py`). No code was changed.

Audit model (as specified for this report):

```
Workspace ID  →  user interaction (list/detail display, copy)
Internal ID   →  Frozen UUID business entity (Deploy / Backup / Cache / Relationship)
SQLite mod_id →  SQL PK/FK only; must not leak to UI, business APIs, persistence paths, or business logs
```

Platform source (`steam` / `nexus` / …) is treated as metadata classification, not an identity layer.

---

# 1. 总体结论

**Identity Runtime Compliance: 48 / 100**

**不符合** 目标流：

```
Workspace ID → Internal ID (UUID) → mod_id (SQL only)
```

当前运行时流是：

```
UI 展示 Workspace ID
UI 操作句柄 = SQLite mod_id (digit PK)
业务 API 参数名常叫 internal_id，值是 PK
resolve_mod_pk() 把 UUID 或 PK 收成 PK
Deploy / Backup / FilePlan / Relationship FK / 业务日志 全程使用 PK
Frozen UUID 主要用于磁盘证明 (.info/internal_id) 和反向查找
```

一句话：三列在数据库里是分开的；**泄漏到 UI 和业务运行时的实体句柄是 `mods.mod_id`，不是 Internal UUID。** Workspace ID 只作为展示/复制/注册元组/依赖输入，没有成为列表或 Deploy 入口。

---

# 2. 当前真实身份流

## 2.1 列表与详情（用户看见什么 vs 代码传什么）

```
SQLite mods
  mod_id          INTEGER PK          ← 运行时句柄
  internal_id     TEXT UUID           ← Frozen 证明
  workspace_id    TEXT                ← 展示号
        │
        ▼
core/db_manager.py list_mod_list_items()
  "internal_id": str(row["mod_id"])   # 字段名 Frozen，值是 PK
  "workspace_id": row["workspace_id"] # 仅投影
        │
        ▼
ModListItem.internal_id = PK
ModCardData.id = item.internal_id     # 仍是 PK
ModCardData.internal_id / .mod_id     # 同值别名
        │
        ├─ UI 展示 / 复制：详情页 view_id = workspace_id
        │                 “Workspace ID: …”
        │                 Debug 才追加 “Internal Database ID: {mod_id PK}”
        │
        └─ UI 操作：card._mod_id() = data.id = PK
                    detail.current_mod_id() = display_info.mod_id if isdigit()
```

## 2.2 Deploy（按钮到 Apply）

```
ModCardWidget._emit_deploy
  deploy_requested.emit(self._mod_id())          # PK
        或
ModDetailPanel._request_deploy
  current_mod_id() → digit PK
  deploy_requested.emit(mid)
        │
        ▼
LibraryView._on_deploy_action(mod_id)
  if not mid.isdigit(): 失败「缺少有效的 Mod ID」   # 拒绝 Frozen UUID
  DeployWorker(mid)
        │
        ▼
DeployWorker.run
  ModDeployer.deploy_mod(self.mod_id)            # 仍是 PK
        │
        ▼
ModDeployer.deploy_mod(internal_id=…)            # 形参名 Frozen
  mid = resolve_deploy_identity(…)               # = resolve_mod_pk
      ① find_mod_by_internal_id(UUID) → PK
      ② token.isdigit() and get_mod(token) → PK  # 生产路径走这里
      永不解析 workspace_id
        │
        ▼
_resolve_context → DeployContext(internal_id=mid)  # mid 是 PK，不是 UUID
        │
        ▼
strategy.plan(ctx)
file_plan_from_strategy_result
  DeployFilePlan.internal_id = ctx.internal_id     # 仍是 PK
        │
        ▼
BackupManager / prove_backup_storage_key
  data/mod_backup/<mod_id PK>/
        │
        ▼
apply_file_plan → verify → update_mod_deploy_status(PK)
业务日志: [DEPLOY] internal_id={PK}
```

## 2.3 Dependency

```
详情「添加依赖」
  提示输入 Workspace ID                 # 符合用户层
  owner = current_mod_id()              # PK
  add_dependency_by_workspace_id(owner_pk, workspace_id)
    resolve_internal_id_from_workspace_id(wid, platform, app_id)
      → resolve_mod_id_by_scoped_workspace → PK
    add_mod_relationship(owner_pk, target_pk, …)   # FK 存 PK
```

用户输入是 Workspace ID（符合）。Owner 与落库目标都是 PK（不符合 “业务流程围绕 Internal UUID”）。

## 2.4 Collection

```
成员 API: add_mod_to_collection(internal_id=…)
  _member_pks → resolve_mod_pk → INTEGER collection_mods.mod_id
```

Service 声明接受 Frozen UUID **或** PK handle。UI 卡片句柄是 PK，因此生产调用是 PK。SQL FK 用 `mod_id` 合理；泄漏发生在 UI→Service 边界，不在 JOIN。

## 2.5 Backup 能否在数据库重建后找回

当前存储键：

```
data/mod_backup/<mods.mod_id>/
```

`prove_backup_storage_key()` 解析顺序：

1. `resolve_mod_pk(hint)`（UUID 或现有 PK）
2. `.info/internal_id` → 当前 `mods.mod_id`

**不能**在「库重建后 PK 改变、只剩 Backup 目录」时，单靠目录名找回。目录名是旧 PK。若重建后 Frozen UUID 仍写在 sidecar/`mods.internal_id`，理论上可用证明找回**新** PK，但 Backup **文件夹本身**仍按旧 PK 命名，不会自动变成 UUID 目录。

结论：Backup 路径绑定 PK，不满足 “数据库重建后仍以 Internal UUID 找到原 Mod” 的运行时边界。

---

# 3. 偏差列表

## P0 — 业务身份错误

### P0-1 UI 操作入口泄漏 PK

| | |
|---|---|
| **文件** | `core/db_manager.py` |
| **行号** | 3263–3268 |
| **当前行为** | Layer-1 注释写明 session key = SQLite PK；字典键 `"internal_id"` 填 `str(row["mod_id"])`。 |
| **设计要求** | UI 传 Workspace ID 或 Internal UUID；PK 不得泄漏到 UI。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `services/mod_library_cache.py` |
| **行号** | 93–99, 231 |
| **当前行为** | `ModCardData.id = item.internal_id`（PK）。属性 `internal_id` / `mod_id` 都返回 `self.id`。 |
| **设计要求** | DTO 不得把 PK 标成 Internal ID，也不得作为卡片实体键传给业务层。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `ui/mod_card.py` |
| **行号** | 155, 726–731, 644–647 |
| **当前行为** | `_mod_id()` 返回 `data.id`（PK）。`deploy_requested.emit(mid)` 传 PK。选择/详情/打开文件夹同一句柄。 |
| **设计要求** | Deploy 按钮不得直接传 `mod_id`。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `ui/mod_detail_panel.py` |
| **行号** | 5405–5428, 965 |
| **当前行为** | `current_mod_id()` 文档写明返回 `mods.mod_id`；只要 `isdigit()`。注释：`PK (= internal_id)`。Deploy emit 该 PK。 |
| **设计要求** | 允许 Workspace 或 Internal UUID；禁止直接传 PK。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `ui/library_view.py` |
| **行号** | 3962–3976 |
| **当前行为** | `_on_deploy_action`：`if not mid.isdigit()` 失败。Frozen UUID 无法从 UI 进入 Deploy。 |
| **设计要求** | Internal UUID 应能作为业务入口；digit 门禁把入口钉死在 PK。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `ui/deploy_thread.py` |
| **行号** | 37–82 |
| **当前行为** | `DeployWorker.mod_id` → `deployer.deploy_mod(self.mod_id)`。失败结果 `internal_id=self.mod_id`（值仍是 PK）。 |
| **设计要求** | Worker 不得以 PK 为部署入口身份。 |
| **风险等级** | P0 |

### P0-2 Deploy 运行时身份是 PK

| | |
|---|---|
| **文件** | `services/deploy.py` |
| **行号** | 1572–1584, 1342–1352 |
| **当前行为** | `deploy_mod(internal_id)` 立即 `resolve_deploy_identity` → PK。`DeployContext(internal_id=mid)` 的 `mid` 是 PK。Docstring 仍写 “by Workshop / published file id”（与实现也不符）。 |
| **设计要求** | Deploy 围绕 Internal UUID；PK 只用于随后的 SQL。 |
| **风险等级** | P0 |

**问题 1 回答：** `deploy_mod()` 形参名是 `internal_id`。生产 UI 传入的是 **mod_id PK**。函数会接受 Frozen UUID（经 `find_mod_by_internal_id`），但 UI 进不去那条路。**不接受 workspace_id。**

**问题 2 回答：** `DeployContext.internal_id` **不是** Internal UUID。变量名叫 `internal_id`，赋值为解析后的 **SQLite PK**。

**问题 3 回答：** **存在** `mod_id` 直接作为部署入口：UI `isdigit()` + `resolve_mod_pk()` 第二步 digit PK 兼容。

| | |
|---|---|
| **文件** | `services/identity_service.py` |
| **行号** | 464–494 |
| **当前行为** | `resolve_mod_pk`：① UUID→PK；② `token.isdigit()` 且 `get_mod(token)` 存在则原样返回 PK。注释写明 Layer-1/Worker 传 PK handle。 |
| **设计要求** | 业务入口不应把 PK 当作合法身份 token。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `services/deploy_paths.py` |
| **行号** | 46–58 |
| **当前行为** | `resolve_deploy_identity` 直接调用 `resolve_mod_pk`（含 PK 兼容）。 |
| **设计要求** | Deploy 解析边界应产出 Internal UUID 供业务使用，PK 仅在 DAL。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `services/deploy_path_lifecycle.py` |
| **行号** | 290–308 |
| **当前行为** | `resolve_entity_internal_id` 名称含 Internal，返回 digit **PK**；非 PK 的 digit（例如 Workspace 数字）拒绝。 |
| **设计要求** | 名称与返回值应为 Frozen UUID，或不得叫 internal_id。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `services/deploy_file_plan.py` |
| **行号** | 90, 237–238 |
| **当前行为** | `DeployFilePlan.internal_id = str(ctx.internal_id)` → PK 进入 FilePlan / 后续 manifest 字段。 |
| **设计要求** | FilePlan 业务身份应为 Internal UUID。 |
| **风险等级** | P0 |

### P0-3 Backup 持久化路径使用 PK

| | |
|---|---|
| **文件** | `services/metadata_backup.py` |
| **行号** | 42–76 |
| **当前行为** | `backup_root(mod_id)` → `data/mod_backup/<mod_id>/`。`prove_backup_storage_key` 证明并返回 **当前 PK**。 |
| **设计要求** | Backup 存储键围绕 Internal UUID；PK 不得作为持久化路径。 |
| **风险等级** | P0 |

重建数据库后 PK 变化时，旧 Backup 目录名无法单独作为实体身份。

### P0-4 DTO 把 PK 叫成 Internal ID

| | |
|---|---|
| **文件** | `core/models.py` |
| **行号** | 147–159 |
| **当前行为** | `ModMetadata.internal_id` 注释为 Entity PK handle。`entity_internal_id()` 文档写明返回 `mods.mod_id`，不是 Frozen TEXT。 |
| **设计要求** | `internal_id` 必须是 UUID。 |
| **风险等级** | P0 |

| | |
|---|---|
| **文件** | `services/mod_list_item.py` |
| **行号** | 77–78 |
| **当前行为** | `ModListItem.internal_id` 运行时是 PK（由 `list_mod_list_items` 填入）。同结构另有真实 `workspace_id`。 |
| **设计要求** | 字段名 Internal 不得存 PK。 |
| **风险等级** | P0 |

### P0-5 分配函数名与产物不符

| | |
|---|---|
| **文件** | `services/identity_service.py` |
| **行号** | 523–537 |
| **当前行为** | `allocate_internal_id()` 调用 `db.allocate_mod_id()`，返回 **INTEGER PK**。 |
| **设计要求** | Internal ID 是 UUID；PK 分配不应叫 internal_id。 |
| **风险等级** | P0 |

---

## P1 — 命名污染

| | |
|---|---|
| **文件** | `services/identity_service.py` |
| **行号** | 430–461 |
| **当前行为** | `resolve_internal_id_from_workspace_id` 文档写 “→ Internal ID”；实现 `resolve_mod_id_by_scoped_workspace` → **PK 字符串**。 |
| **设计要求** | 名称与返回值一致：要么返回 UUID，要么函数名含 pk。 |
| **风险等级** | P1 |

| | |
|---|---|
| **文件** | `services/mod_relationships.py` |
| **行号** | 15–32, 57–64 |
| **当前行为** | 文档：Workspace → Internal，落库 Internal。实现：`owner.isdigit()` 要求 PK；关系表 FK 为 PK。 |
| **设计要求** | 业务流程围绕 UUID；SQL FK 用 PK 可以，但 owner 参数不应叫/当 Internal UUID。 |
| **风险等级** | P1 |

| | |
|---|---|
| **文件** | `services/deploy.py` |
| **行号** | 1584 及多处 `internal_id=%s` |
| **当前行为** | 业务日志 `[DEPLOY] internal_id={PK}`。 |
| **设计要求** | 业务日志不得泄漏 PK（或不得把 PK 标成 internal_id）。 |
| **风险等级** | P1 |

| | |
|---|---|
| **文件** | `ui/mod_detail_panel.py` |
| **行号** | 2578–2589, 965, 975–984 |
| **当前行为** | Debug 展示 “Internal Database ID” 实际是 `info.mod_id` PK。编辑失败文案 “缺少有效的 Mod internal_id” 要求的是 digit PK。 |
| **设计要求** | Internal ID = UUID；Debug 可显示 PK，但不得叫 Internal ID。 |
| **风险等级** | P1 |

| | |
|---|---|
| **文件** | `services/collection.py` |
| **行号** | 13–18, 40–53, 220–239 |
| **当前行为** | 公开 API 参数名 `internal_id`，同时文档允许 “Frozen 或 PK handle”；`invalid mod_id` 报错。 |
| **设计要求** | 业务 API 要么只收 UUID，要么参数名不叫 internal_id。 |
| **风险等级** | P1 |

| | |
|---|---|
| **文件** | `services/deploy_rules/base.py` |
| **行号** | 60, 72–73 |
| **当前行为** | `DeployContext.internal_id` 运行时为 PK；`workspace_id` 仍作为可选上下文字段传给 WH3/Duckov 规则（不是列表主键）。 |
| **设计要求** | `internal_id` 应为 UUID。 |
| **风险等级** | P1 |

---

## P2 — 兼容历史代码

| | |
|---|---|
| **文件** | `services/identity_service.py` |
| **行号** | 469–472, 488–491 |
| **当前行为** | 明确保留 “PK-digit compatibility / in-process handle”，供 Layer-1 与 Worker。 |
| **设计要求** | 目标模型不允许 PK 作为业务 token；这是有意兼容层。 |
| **风险等级** | P2（相对目标是债务；相对现行仓库合同是设计内） |

| | |
|---|---|
| **文件** | `core/db_manager.py` |
| **行号** | 2114–2129 |
| **当前行为** | `find_mod_by_workspace_id` 恒返回 `None`（已废实体键 API）。 |
| **设计要求** | Workspace 不作 DB 实体主键 — **符合**。列为 P2 仅因旧 API 名仍在。 |
| **风险等级** | P2（保留壳） |

| | |
|---|---|
| **文件** | `services/deploy.py` |
| **行号** | 1579 |
| **当前行为** | Docstring：“Deploy one Mod by Workshop / published file id.” 与实现（PK/UUID resolve）不一致。 |
| **设计要求** | 文档与入口身份一致。 |
| **风险等级** | P2 |

| | |
|---|---|
| **文件** | `core/models.py` |
| **行号** | `published_file_id` 字段 / `workshop_url` |
| **当前行为** | Steam 元数据字段仍在 DTO；**不是** Deploy 实体入口。本审计不把平台 ID 当作第四层身份。 |
| **设计要求** | 平台分类元数据可保留。 |
| **风险等级** | P2（元数据残留，非运行时实体键） |

---

# 4. 正确保留项

下列使用 `mod_id` **合理**（SQL / FK / 索引），不应当成 P0 误报。

| 位置 | 为什么合理 |
|---|---|
| `mods.mod_id` 列、主键、自增分配 `allocate_mod_id()` | 数据库内部 PK |
| `collection_mods.mod_id`、`mod_tags.mod_id`、`mod_relationships` 两端、`deployment_record_items.mod_id` | SQL FK |
| `DatabaseManager.get_mod(mod_id)` / `WHERE mod_id = ?` / JOIN `m.mod_id` | DAL 查询 |
| `find_mod_by_internal_id` **返回** `str(row["mod_id"])` | Frozen UUID → PK 的 DAL 边界（输出给 SQL 是对的；问题在调用方把该 PK 再送回 UI/Deploy） |
| `find_mod_for_registration(platform, app_id, workspace_id)` | 注册重匹配；Workspace 不作裸主键；返回 PK 给 SQL |
| `find_mod_by_workspace_id` → `None` | 禁止 Workspace 当实体主键 |
| 详情页展示/复制 `workspace_id`（`view_id`、`btn_copy_id`、`Workspace ID:` 标签） | 符合用户交互 ID |
| 添加依赖对话框提示输入 Workspace ID | 符合用户交互 ID |
| `update_mod_deploy_status(mid, …)` 在已持有 PK 之后写行 | SQL 更新按 PK |
| Layer-1 投影带出 `workspace_id` 供展示 | 展示字段，不是操作键 |

**Workspace ID 符合的部分：** 详情展示、复制、`(platform, app_id, workspace_id)` 注册、依赖用户输入。  
**Internal UUID 符合的部分：** 列存在、`.info/internal_id` 证明、`find_mod_by_internal_id`、`resolve_mod_pk` 第一步。  
**不符合的部分：** UUID 没有成为 UI→Deploy→Backup 的运行时句柄。

---

# 5. Resolver 对照表

| 函数 | 输入（代码） | 输出（代码） | 名称与语义 |
|---|---|---|---|
| `find_mod_by_internal_id` | Frozen TEXT `mods.internal_id` | `mods.mod_id` 字符串或 None | **一致**（DAL：UUID→PK） |
| `find_mod_by_workspace_id` | workspace_id（忽略） | 恒 `None` | 旧名；行为符合“不作实体键” |
| `resolve_mod_pk` | UUID **或** digit PK | PK 字符串或 `""` | 名称像只收 Frozen；第二步收 PK → **污染** |
| `resolve_deploy_identity` | 同上 | 同上（调用 `resolve_mod_pk`） | 文档写 Frozen→PK；生产传入 PK → **污染** |
| `resolve_entity_internal_id` | 同上 | `(pk, None)` 或 `("", error)` | 名称 Internal，值 PK → **污染** |
| `resolve_internal_id_from_workspace_id` | workspace_id + platform + app_id | **PK** 字符串 | 名称 Internal，值 PK → **污染** |
| `allocate_internal_id` | 无 | **INTEGER PK** | 名称 Internal，产物 PK → **污染** |

存在任务点名的模式：

- 参数/返回叫 `internal_id`，实际是 PK  
- 函数叫 `*_from_workspace_id` → Internal，实际返回 PK  

---

# 6. 修复建议（只建议，不改代码）

不提出新身份模型。建议只用于把运行时拉回既定三层：

1. **钉死 UI→业务边界的 token 类型**  
   卡片/详情/Deploy/Collection 成员操作只允许传 Workspace ID（再解析）或 Frozen UUID。禁止 `isdigit()` 把 PK 当通行证。

2. **把 PK 限制在 `resolve_mod_pk` 之后的 DAL**  
   `DeployContext` / `DeployFilePlan` / 业务日志若必须引用实体，使用 UUID；SQL 更新再映射 PK。

3. **Backup 目录键改为 Internal UUID**（或 UUID 为主、PK 仅兼容读）  
   否则无法满足库重建后按业务身份找回。

4. **去掉命名谎言**  
   `ModListItem.internal_id`、`entity_internal_id()`、`allocate_internal_id()`、`resolve_internal_id_from_workspace_id` 的名称或返回值必须与 UUID/PK 之一对齐。

5. **收敛 `resolve_mod_pk` 第二步**  
   digit PK 兼容是当前生产 Deploy 能工作的原因，也是边界被打破的原因。在 UI 停止传 PK 之前，去掉它会直接破坏现网路径。

---

# 7. 最终问题

**当前 SMM 代码距离既定身份架构还有多少偏差，哪些已符合，哪些必须修？**

| | |
|---|---|
| 偏差量 | 运行时主路径（列表键、Deploy、Backup 路径、FilePlan、关系 owner、业务日志）都在用 PK。符合度 **48/100**。 |
| 已符合 | 三列物理分离；Workspace 不作 DB 实体主键；Deploy 不按 Workspace 裸查；详情展示/复制 Workspace ID；依赖**输入**是 Workspace ID；Frozen UUID 与 `.info` 证明存在；SQL FK 用 `mod_id`。 |
| 必须修复（相对本模型） | UI 禁止传 PK；`DeployContext.internal_id` 必须是 UUID 而非 PK；Backup 路径不得用 PK；DTO/API/日志停止把 PK 叫作 `internal_id`。 |

当前实现能正确部署，是因为 **PK 被当成了事实上的业务 ID**，再靠 `resolve_mod_pk` 的 digit 兼容把 UI 和 SQL 接在一起。这与 “PK 不得泄漏到 UI、业务 API、持久化路径、业务日志” 相反。
