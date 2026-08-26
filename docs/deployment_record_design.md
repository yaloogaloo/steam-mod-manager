# Deployment Record 设计约束

> 架构冻结文档。改动本功能前必须先读本文。  
> **Record = Filter · Record ≠ Mode · Record ≠ Deploy · Record ≠ Second Library**

## 1. 产品定位

Deployment Record 与下列筛选项**同等级**：

全部 · 内容缺失 · 异常 · 已部署 · 收藏 · **部署记录**

用户心智：「部署记录就是一个可以保存的收藏筛选。」

| 是 | 不是 |
|----|------|
| `_status_filter = FILTER_DEPLOYMENT_RECORD` | 进入/退出记录模式 |
| 筛选参数 `deployment_record_id` | Active Record / Record 页面 |
| 复用 `filter_and_sort` + 同一套 ModCard | 第二套列表 / 侧栏 / 提示行 |

禁止出现：进入记录、退出记录、当前记录模式、Record 状态机。

## 2. 数据模型

### `deployment_records`

`id`（内部）、`app_id`、`name`（用户唯一标识）、`created_at`、`updated_at`

约束：`UNIQUE (app_id, name)`。跨游戏允许同名。

### `deployment_record_items`

`record_id`、`mod_id`

**禁止**持久化：`extra_deployed` / `record_missing` / `relative_status`。

## 3. Filter 互斥

```
点「收藏」     → status=FAVORITE, record_id=None, 清 overlay
点「记录·xxx」 → status=DEPLOYMENT_RECORD, record_id=<内部>, 取消其它状态芯片
点「全部」     → status=ALL, record_id=None, 清 overlay
```

禁止 Record ∧ 收藏 / 已部署 同时生效。

平台 / 分类 / 搜索仍可与当前 status filter AND。

## 4. DEPLOYMENT_RECORD 可见集与 relative

```
visible = recorded_mod_ids ∪ currently_deployed
```

内存计算：

- 记录内 + 已部署 → 无徽章
- 记录内 + 未部署 → **记录缺失**
- 不在记录 + 已部署 → **额外部署**

切到非 `FILTER_DEPLOYMENT_RECORD` 时立即清除 overlay。

## 5. UI

同一行、不增高：

```
[全部][内容缺失][异常][已部署][收藏]          [💾 部署记录 ▼]
```

整颗按钮打开 **唯一 Popup**（筛选 + 管理）：

- 全部 / 各记录名（互斥筛选）
- 保存当前部署（同名须确认覆盖）
- 更新记录 / 重命名记录 / 删除记录（左键可达）

允许按钮文案变为 `💾 宝可梦一周目`；禁止提示行 / 侧栏 / banner。

## 6. 禁止事项

1. 禁止 enter/leave / `_record_context` 模式状态机  
2. 禁止写 deploy_status / 调用 deploy.py  
3. 禁止 relative 落库  
4. 禁止第二套列表或纵向占高 UI  
5. 禁止静默同名覆盖（必须确认）

Service：`create_or_update_record` / `find_record_by_name` / `rename` / `delete` / `list` / `get_record_mod_ids` — 只读写 record 表，只读已部署集合。
