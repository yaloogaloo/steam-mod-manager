# Steam Mod Manager — 全局性能与代码清理审计

**日期：** 2026-08-12  
**性质：** 只读审计。本文档不修改业务代码、UI、数据库或行为。  
**约束：** 不建议大规模重写；优化路线以小步、可回滚、可测为准。  
**范围：** `main.py`、`core/`、`services/`、`ui/`；测试与 `data/` 探针文件仅作为死代码/遗留证据。

---

## 1. 项目架构扫描

### 1.1 核心模块

| 层 | 路径 | 职责 |
|---|---|---|
| 入口 | `main.py` | CLI（`scan` / `fetch` / `sync` / `backfill`）或 `launch_gui()`。GUI 路径在创建窗口前调用 `get_db()`，安装诊断探针，再 `MainWindow.show()`。 |
| 数据 | `core/db_manager.py` | 进程级 SQLite 单例（WAL、`check_same_thread=False`、`RLock`）。游戏配置、Mod 元数据、标签、两套关系表、部署状态、离线状态、备份快照列。 |
| 模型 | `core/models.py`、`core/mod_platform.py`、`core/mod_status.py`、`core/game_info.py` | `ModMetadata`、平台常量、多文件 bundle、生命周期状态、游戏信息。 |
| Steam 网络 | `core/steam_api.py`、`core/scraper.py`、`core/scanner.py` | Workshop API 批量拉取；HTML 刮取兜底；工坊目录扫描。 |
| 库文件 | `services/file_ops.py` | **磁盘** `ModFileManager`：列出游戏/Mod 目录、读写 `.info/metadata.json`、封面路径、缺失内容检测、复制。 |
| 多文件 JSON | `services/mod_files.py` | **同名** `ModFileManager`：只管理 SQLite `mods.mod_files` JSON，供详情/部署勾选。 |
| 导入 | `services/importers/` | Steam / Nexus / GitHub / Mod.io / Other / Archive / 目录批量。共同落盘入口是 `materialize_imported_mod`。 |
| 同步 | `services/sync.py` | CLI/同步中心：扫描工坊 → 复制 → 封面 → Steam 离线页。 |
| 部署 | `services/deploy.py` + `services/deploy_rules/` | `ModDeployer` + 策略（generic / palworld / anno / slay_the_spire / custom）。 |
| 元数据刷新 | `services/metadata_refresh.py`、`services/modio_metadata_refresh.py` | Steam 远程刷新 + 批量调度；Mod.io REST；Nexus/GitHub 走本地 `rescan_mod_folder`。 |
| 版本检查 | `services/mod_update.py` + `services/update_sources/` | 与「元数据刷新」平行的另一套平台适配（Steam 源为 stub，无 Mod.io）。 |
| 离线页 | `services/archive.py` + `services/offline/` | Steam 用 `OfflinePageArchiver`；其它平台经 `OfflineManager` 分发到 provider。 |
| Sidecar / 备份 | `services/info_sidecar.py`、`services/metadata_backup.py` | 同一份 `.info/metadata.json` 的第二套读写；`data/mod_backup/<id>/` 第三份磁盘副本。 |
| 封面 | `services/cover_loader.py` | `QThreadPool`（最多 4 worker）异步 `QImage`，GUI 线程转 `QPixmap`。 |
| UI 壳 | `ui/main_window.py` | 无边框主窗 + 三页：同步中心 / Mod 库 / 游戏部署。 |
| 库页 | `ui/library_view.py`、`ui/mod_card.py`、`ui/mod_detail_panel.py`、`ui/library_query.py`、`ui/flow_layout.py` | 游戏侧栏 + 卡片网格 + 右侧详情。筛选/排序纯函数在 `library_query`。 |
| 诊断（生产路径仍启用） | `ui/startup_lifecycle.py`、`ui/widget_show_trace.py`、`ui/popup_trace.py`、`ui/window_chrome.py` 探针 | 启动时间线、`QWidget.show` 猴子补丁、弹窗堆栈日志、任务栏图标诊断。 |

### 1.2 数据流

```
磁盘库  mod/<Game>/<Mod>/
          ├── 内容文件
          └── .info/metadata.json          ← 便携身份（file_ops + info_sidecar 两套解析）
              ├── cover.*
              ├── offline/index.html       ← 离线页
              └── deploy_manifest.json     ← 部署清单

SQLite  data/mod_manager.db
          games / mods / mod_tags
          mod_relations + mod_relationships   ← 两套关系表并存
          game_categories
          mods.backup_* 列                    ← 备份元数据也进库

备份    data/mod_backup/<mod_id>/
          metadata.json + cover + offline/    ← 第四份副本
```

**权威源（实际运行时）：**

- 列表/卡片展示：磁盘目录扫描 + `.info/metadata.json` + SQLite 覆盖字段。
- 详情：再次读 metadata.json、sidecar 写回 DB、备份同步、再读 SQLite。
- 部署/启用/收藏/标签：SQLite 为主。
- 离线页：磁盘 `.info/offline/`（及 Steam 遗留 `.info/index.html`）。

同一 Mod 的标题/封面/离线路径至少存在 **磁盘 JSON、SQLite、备份目录** 三处；详情打开时还会主动把 sidecar 写回 DB 并复制备份。

### 1.3 UI 调用链

**启动**

```
main.launch_gui
  → get_db()                          # schema + 多次 ALTER 迁移
  → QApplication + 双份 stylesheet
  → install_widget_show_trace         # 猴子补丁 QWidget.show / setVisible
  → MainWindow.__init__
       → 同时构造 SyncCenterView + ModLibraryView + GameDeployView
       → ModLibraryView 内立即构造完整 ModDetailPanel（约 4500 行控件树）
       → _restore_settings
            若上次停在「Mod 库」→ library_view.refresh()   # 首屏前、UI 线程
            若停在「同步/部署」→ 对应 refresh
  → window.show() + processEvents
  → dump_startup_surface_audit
  → QTimer(0): 任务栏图标诊断
  → QTimer(0): run_deploy_audit       # 见 §2 / P0
```

**打开 / 刷新 Mod 库**

```
ModLibraryView.refresh                    # UI 线程，同步
  → _set_loading(True) + processEvents
  → _rebuild_game_list
       list_managed_mods() 全库一次
       + 每个游戏再 list_managed_mods(game_name)
  → _render_mod_cards
       list_managed_mods(当前游戏)
       每个 folder: load_metadata + load_info_sidecar + enrich_title_from_db
       reconcile_library_presence（写 DB）
       批量 get_mods_search_fields / get_mods_tag_flags
       创建或 rebind ModCardWidget
  → 每张卡 refresh_display → 每卡 6～8 次 SQLite + 磁盘探测
  → _apply_view_filter：拆掉 FlowLayout 再按可见集 addWidget + show
  → CoverLoaderManager.request（每张卡）
```

**点选 / 切换详情**

```
ModCardWidget.mousePressEvent
  → log_popup（warning + 堆栈）
  → selection_requested → LibraryView.on_mod_selected
       → log_popup
       → _select_card → detail_panel.show_mod(path)
            → load_metadata
            → apply_sidecar_to_db          # 每次点击写 SQLite
            → sync_metadata_backup         # 每次点击 copytree 离线页
            → get_mod_display_info
            → _fill_view
                 get_directory_size(rglob 整棵树含 .info/offline)
                 再读 metadata.json（作者字段）
                 关系 / 标签 / 生命周期 / 文件列表
                 封面异步加载
```

**批量刷新**

```
MetadataBatchRefreshWorker (QThread)
  → refresh_selected_mods_metadata
       Nexus: silent_correct_nexus_workspace_id
       Mod.io: refresh_modio_mod_metadata（网络，逐个）
       Nexus/GitHub/其它: rescan_mod_folder（本地，逐个、串行）
       Steam: refresh_steam_mods_metadata（ThreadPool，max_workers≤2）
```

单条详情刷新：Steam / Mod.io 走 QThread；**Nexus / GitHub 在 UI 线程**调用 `rescan_mod_folder`。

### 1.4 后台任务

| 类 | 文件 | 工作 | 是否碰 UI | I/O |
|---|---|---|---|---|
| `SyncWorker` | `ui/sync_thread.py` | 工坊同步 | 信号回 UI | 磁盘 + 网络 |
| `OfflinePagesSyncWorker` | 同上 | 批量离线页 | 信号回 UI | 网络 + 磁盘 |
| `ImportWorker` | `ui/import_thread.py` | 导入 | 信号回 UI | 磁盘 + 解压 |
| `DeployWorker` | `ui/deploy_thread.py` | 部署/卸载 | 信号回 UI | 磁盘 |
| `MetadataRefreshWorker` | `ui/metadata_refresh_thread.py` | 单条 Steam 刷新 | 信号回 UI | 网络 + 重命名 |
| `ModioMetadataRefreshWorker` | 同上 | 单条 Mod.io | 信号回 UI | 网络 |
| `MetadataBatchRefreshWorker` | 同上 | 混合平台批量 | 进度信号 | 网络 + 磁盘 |
| `OfflineArchiveWorker` | `ui/offline_archive_thread.py` | 单条离线页 | 信号回 UI | 网络 / Playwright |
| `OfflineHtmlImportWorker` | 同上 | 手工 HTML/MHTML | 信号回 UI | 磁盘 |
| `GameInfoWorker` | `ui/game_info_worker.py` | Steam 游戏信息 | 信号回 UI | 网络 |
| `CoverLoadTask` | `services/cover_loader.py` | 封面解码 | `image_ready` 排队到 GUI | 磁盘 |
| `OfflinePageWorker` | `ui/mod_detail_dialog.py` | 遗留详情对话框归档 | 仅遗留 UI | 网络 |

**仍在 UI 线程的重活（不是 QThread）：**

- `ModLibraryView.refresh` 全路径
- `run_deploy_audit` / `scan_deployed_mods`
- `ModDetailPanel.show_mod`（sidecar 写库、备份 copytree、目录 size）
- Nexus/GitHub `rescan_mod_folder`
- `ModCardWidget.refresh_display` 的全部 SQLite / `rglob`

### 1.5 数据库职责

**表**

- `games`：AppID、名称、安装/Mod/工坊路径、部署类型。
- `mods`：标题、描述、用户覆盖、平台/URL/external_id/workspace_id、`mod_files` JSON、失效/冲突、版本、启用、离线状态、封面、部署状态、**备份快照列**。
- `mod_tags`：分类/用户标签。
- `mod_relations`：旧关系（note 字段）。
- `mod_relationships`：新关系（dependency / conflict，UNIQUE）。
- `game_categories`：游戏侧栏分类。

**索引：** `app_id`、`platform`、`workspace_id`、标签、两套关系。  
**缺失：** `deploy_status`、`folder_present` 无索引（`list_deployed_mod_ids` 全表扫描；库规模小时可接受）。

**已有批量 API（热路径未用够）：**

- `get_mods_search_fields(ids)` — 库网格筛选字段
- `get_mods_tag_flags(ids)`
- `get_relationship_counts(ids)` — 卡片却按 **单 ID** 调用

**N+1 / 全表模式：** 见 §2。SQLite 本身不是第一瓶颈；瓶颈是 **每张卡多次同步查询 + 每次查询带上 `description` 大字段**（`get_mod_display_info` 使用 `_MOD_SELECT_COLS`，含 `description`）。

---

## 2. 性能瓶颈分析

### 2.1 启动速度

**首屏前（`MainWindow.__init__`，UI 线程）**

1. `get_db()`：建表 + `_GAMES_MIGRATIONS` / `_MODS_MIGRATIONS` 的 `PRAGMA table_info` + 条件 `ALTER`。冷启动可接受；热启动也每次跑迁移探测。
2. `APP_STYLE` 应用两次：`launch_gui` 的 `app.setStyleSheet`，以及 `MainWindow.setStyleSheet`（同一套 + 标题栏）。
3. 三页一次性构造。`ModDetailPanel` 控件树在用户未打开库时已经建完。
4. 若 `QSettings` 上次页是 Mod 库：`library_view.refresh()` 在 **`show()` 之前**跑完整库扫描（见 2.2）。这是「启动黑屏/卡死」的最大来源。
5. 生产路径诊断：
   - `install_widget_show_trace`：给 `QWidget`/`QLabel`/`QFrame`/… 换成 Python `show`/`setVisible`，每次显示走堆栈探测。
   - `startup_lifecycle` / `window_chrome`：`print(..., flush=True)`。
   - `install_syscommand_probe`。

**首屏后（`QTimer.singleShot(0)`）**

6. `run_deploy_audit` → `scan_deployed_mods`：对每个已部署 ID 调用 `audit_deploy_state`，其中 `ModFileManager.find_by_published_id` 会 **`list_managed_mods()` + 逐个 `load_metadata`**。复杂度 **O(已部署数 × 全库 Mod 数)**，且在 UI 线程。

**已做对的事：** 部署/导入/Steam 刷新/封面解码已下放到线程；库有卡片缓存（`_card_cache`）。

### 2.2 打开 Mod 库速度

热路径全部在 UI 线程，且 **没有虚拟化**：可见卡片全部进入 `FlowLayout`。

**文件系统重复扫描**

| 调用点 | 行为 |
|---|---|
| `_rebuild_game_list` | `list_managed_mods()` 无过滤（全库）+ 每个游戏再扫一遍 |
| `_render_mod_cards` | 再 `list_managed_mods(game)` |
| 每个 folder | `load_metadata`（读 metadata.json）+ `load_info_sidecar`（再读同一文件）+ `enrich_title_from_db`（可能 `get_mod`） |
| `_build_filter_index` | `offline_page_exists` → `resolve_offline_page_path`（多候选 `is_file`/`stat`）+ `folder.stat()` |
| `find_by_published_id` | 全库线性扫描（审计、部分写 sidecar 路径） |

**卡片 N+1（`ModCardWidget.refresh_display`）**

每张卡、每次 refresh 大约：

| 次数 | API | 问题 |
|---|---|---|
| ×3 | `get_mod_display_info` | `_apply_titles` / `_render_offline_badge` / `_render_platform_badge`；含 `description` |
| ×1 | `get_mod_status` | 与 display_info 字段重叠 |
| ×1 | `is_mod_enabled` | 又可合并 |
| ×1 | `get_category_tags` | 筛选阶段已有 `category_tags` |
| ×1 | `get_mod_deploy_info` | 筛选阶段已有 `deploy_status` |
| ×1 | `get_relationship_counts([mid])` | 批量 API 被当成单条用 |
| ×1 | `offline_page_file_exists` | 多路径 stat |
| ×1 | `read_is_missing_content` | 再读 metadata.json；未置位时 `rglob` 整棵树（含 `.info` 枚举后再跳过） |

100 张卡 ≈ **800+ 次 SQLite round-trip** + 同等量级磁盘探测，全部阻塞 UI。  
`_render_mod_cards` 已经批量拉了 `get_mods_search_fields`，但 **没有传给卡片**。

**封面**

- 无解码缓存：同一 `cover.jpg` 每次 `QImage` + `SmoothTransformation`。
- 每张卡在 `__init__` 连接单例 `image_ready`。N 张卡 × M 次完成 = **O(N×M) GUI 槽调用**（缓存跨游戏累积后更差）。
- Worker 上限 4；`resolve_cover_path` 对每个 basename×扩展名做 `is_file`（约 12 次）。
- 缺失文件夹的封面在 **GUI 线程** 同步 `QImage(str(direct))`。

**其它**

- `_set_loading(True)` 调用 `QApplication.processEvents()`：加载中可重入点击/刷新。
- `_apply_view_filter` 拆光 layout 再 `addWidget`+`show`：每张 `show` 经过 widget_show_trace。
- `log_popup` 在 tooltip / 点击上打 `logger.warning` + `traceback.extract_stack`。

### 2.3 切换 Mod 详情速度

`show_mod` 文档写「no Steam I/O」，但 UI 线程仍做：

1. **`apply_sidecar_to_db`**：读 sidecar、`update_mod_user_metadata`（写库）。点选不应是写路径。
2. **`sync_metadata_backup` → `snapshot_from_mod_folder`**：写 `data/mod_backup/<id>/metadata.json`，复制封面，并对 `.info/offline/` 做 **`shutil.rmtree` + `copytree`**。有 Playwright/Mod.io 资源树的 Mod，单次点击可复制数百文件。
3. **`get_directory_size`**：`root.rglob("*")` + 每个文件 `stat().st_size`，**包含** `.info/offline/assets`。体积徽章会扫完整离线快照。
4. `_fill_view` 再读一次 `read_info_metadata_dict`（作者）。
5. `_fill_relationships` 与 `_refresh_dependency_pill` **各** `get_mod_relationships` 一次。
6. `_fill_lifecycle_status` / `_fill_user_tags` / 分类标签：更多同步查询。
7. 封面再走 CoverLoader（与卡片重复解码）。

Nexus/GitHub 点「刷新」：`rescan_mod_folder` 在 UI 线程（归档扫描 + 写 sidecar + 再 `sync_metadata_backup`）。

### 2.4 批量刷新速度

**已隔离：** Steam / Mod.io 在 `QThread`；Steam 侧 `max_workers≤2` + `needs_metadata_refresh` 跳过健康项。

**仍慢的原因：**

- 非 Steam 在 `refresh_selected_mods_metadata` 里 **for 循环串行**。
- 每条 Mod.io：`get_mod_display_info` + REST + 写 JSON/DB + 可能重命名 + 下封面。
- 每条 Nexus/GitHub：`rescan_mod_folder`（磁盘，无远程元数据）。
- 刷新后 UI 常 `show_mod` / `refresh_display`，把 §2.2–2.3 的同步成本再付一遍。
- 封面下载与 Steam 刷新逻辑在 `modio_metadata_refresh` 中复制了一份。
- `update_sources` 与 metadata refresh 是另一套平台分发，Steam 源直接 `supported=False`，无 Mod.io。

### 2.5 横切问题汇总

| 类型 | 证据 |
|---|---|
| UI 线程阻塞 | `refresh`、`show_mod`、`run_deploy_audit`、`rescan_mod_folder`、卡片 `refresh_display`、`get_directory_size`、备份 `copytree` |
| 文件系统重复扫描 | 游戏列表 × 卡片列表；`find_by_published_id` 全库；详情 size 再扫一遍 |
| 重复读取 | 同一 `metadata.json`：`load_metadata` + `load_info_sidecar` + 卡片 `read_is_missing_content` + 详情作者字段 |
| 图片瓶颈 | 无 QImage 缓存；信号扇出；4 worker；路径探测 12×`is_file` |
| 数据库瓶颈 | 每卡 6～8 次查询；`get_mod_display_info` 拉 description；全局 `RLock` 串行化；详情点击写库 |

---

## 3. 卡顿来源排名

### P0 — 必须优化（用户可感知卡顿）

| # | 问题 | 热路径 | 建议（小步） |
|---|---|---|---|
| P0-1 | 库刷新在 UI 线程同步完成；上次停在库页时发生在 `show()` 前 | 启动 / 打开库 | 首屏只画壳；`QTimer.singleShot(0)` 再 refresh；或先画缓存卡片 |
| P0-2 | 每张卡 `refresh_display` N+1 SQLite（含 description） | 打开库 / 切游戏 | 把已有的 `get_mods_search_fields` + `get_relationship_counts(全部 id)` 一次传入卡片，禁止卡内再查 |
| P0-3 | `show_mod` 每次点击 `sync_metadata_backup`（离线树 copytree） | 切详情 | 备份改为导入/刷新/关闭时；点选只读 |
| P0-4 | `get_directory_size` 在 UI 线程 rglob 含 `.info/offline` | 切详情 | 徽章先空着；后台算；或只 stat 内容目录并缓存 |
| P0-5 | `find_by_published_id` 导致部署审计 O(n²) | 启动后 0ms | 一次 `list_managed_mods` 建 `id→path`；审计用 map |
| P0-6 | 生产环境诊断探针（show 补丁、`log_popup` warning+堆栈、`print flush`） | 启动 / 滚动 / 点击 | 默认关闭；`SMM_DEBUG=1` 才启用 |
| P0-7 | 封面 `image_ready` 扇出 + 无解码缓存 | 打开库 | 管理器按 token 分发；按 `(path, w, h)` 缓存 QImage |

### P1 — 建议优化

| # | 问题 | 建议 |
|---|---|---|
| P1-1 | `_rebuild_game_list` 重复 `list_managed_mods` | 一次扫描聚合成 `game → [folders]` |
| P1-2 | 渲染时 `load_metadata` + `load_info_sidecar` 读同一 JSON | 一次解析，两处投影 |
| P1-3 | `apply_sidecar_to_db` 在每次 `show_mod` 写库 | 仅文件夹刚出现 / 用户保存时恢复 |
| P1-4 | 无卡片虚拟化（FlowLayout 挂全部可见卡） | 先做 P0-2；库 >300 再考虑窗口化 |
| P1-5 | Nexus/GitHub 刷新在 UI 线程 | 复用 `MetadataRefreshWorker` 包一层 rescan |
| P1-6 | 批量非 Steam 串行；`max_workers` 封顶 2 | 保持 2（防限流）；只消掉每条的重复 DB/JSON 读写 |
| P1-7 | `read_is_missing_content` 的 `rglob` 仍枚举 `.info` | `os.walk` 剪枝 `.info`；或只信 metadata 标志 |
| P1-8 | `offline_page_file_exists` 多候选 | 卡片只用 canonical `.info/offline/index.html` 一次 `is_file` |
| P1-9 | `_set_loading` 里 `processEvents` | 去掉；用 overlay + 禁用按钮即可 |
| P1-10 | 双份 stylesheet + 启动构造三页+完整详情树 | 延迟构造未显示页；stylesheet 只设一次 |
| P1-11 | `enrich_title_from_db` 在渲染循环逐条 `get_mod` | 并入 search_fields 批量结果 |

### P2 — 可延后

| # | 问题 |
|---|---|
| P2-1 | `deploy_status` / `folder_present` 缺索引 |
| P2-2 | 全局 SQLite 连接 + `RLock`（刷新线程与 UI 抢锁） |
| P2-3 | Steam `WorkshopPageScraper` 同步兜底（仅网络失败路径） |
| P2-4 | 同步中心 `QPixmap(cover_path)` 在 GUI 槽里同步解码（游戏封面，次数少） |
| P2-5 | `FlowLayout` 多次 `heightForWidth` / `invalidate` |
| P2-6 | 卡片 `setStyleSheet` 字符串每卡每刷新重建 |
| P2-7 | `get_mod_display_info` 列集过大（长期：轻量 DTO） |
| P2-8 | 虚拟化 / 按滚动加载封面（P0-7 之后） |

---

## 4. 重复代码分析

不建议一次合并成「超级平台类」。下面是 **安全的收敛点**。

### 4.1 Steam / Nexus / Mod.io / GitHub

**导入（健康度较好）**  
`SteamImporter` / `NexusImporter` / `GithubImporter` / `ModioImporter` / `OtherImporter` 都走 `materialize_imported_mod`。重复的是 URL 解析（`parse_nexus_id`、`parse_modio_id`、`GithubImporter.parse_repo`）和 `build_*_mod_files`。可抽小组件，不要动 materialize。

**刷新（明显分叉）**

| 平台 | 实现 | 差异 |
|---|---|---|
| Steam | `refresh_steam_mod_metadata` | API + 封面 + 重命名 |
| Mod.io | `refresh_modio_mod_metadata` | REST + 复制了一套封面/重命名/写 JSON |
| Nexus / GitHub / Other | `rescan_mod_folder` | 无远程元数据 |

`refresh_selected_mods_metadata` 已是唯一批量入口，适合作为长期门面；Mod.io 的 persist/rename 应调用 Steam 侧已有 `safe_directory_rename`（已部分复用）而不是再抄封面安装。

**更新检查**  
`services/update_sources/` 与 refresh 平行：Steam stub、无 Mod.io。不要在优化 Phase 1 合并；只在文档上标明「两套平台路由」。

**离线**  
`OfflineManager` 已是 UI 门面。底层仍有多套快照栈：

- Steam：`services/archive.py`（~1900 行，curl_cffi）
- Nexus 手工：`nexus_manual.py` + `nexus_cleaner/`
- GitHub / 布局：`layout_snapshot.py`（~1170 行）+ `github_browser_snapshot.py`
- Playwright：`offline/browser.py` **和** `offline/browser_snapshot/`
- 可读摘要：`readable_snapshot.py`（~1100 行，**生产无引用**）
- 遗留生成器：`offline/generator.py`（**无引用**）
- URL 改写：`html_rewriter.py` / `url_rewriter.py` / `browser_snapshot/resource_rewriter.py`

### 4.2 Metadata 处理重复

同一份 `.info/metadata.json`：

1. `file_ops.read_info_metadata_dict` / `persist_unified_metadata_dict`
2. `info_sidecar.load_info_sidecar` / `write_sidecar_for_mod`（另一 dataclass）
3. SQLite `mods.*`
4. `metadata_backup.snapshot_from_mod_folder` + `mods.backup_metadata_json`

库渲染走 1+2；详情打开走 2→3 和 4。  
**收敛点：** 读路径只保留 `read_info_metadata_dict`；sidecar 作为 typed view。写路径：用户保存 → JSON + DB；备份异步、去重。

### 4.3 Offline 处理重复

打开路径已有契约：`services/offline/paths.py`（canonical `.info/offline/index.html`）。  
卡片存在检测却走更宽的 `resolve_offline_page_path`（sidecar 候选 + `iterdir`）。  
Steam 遗留 `.info/index.html` 与新布局并存是兼容需要，不是重复实现；重复的是 **三套 HTML 改写** 和 **两套 Playwright 封装**。

### 4.4 Refresh 处理重复

- 线程：`MetadataRefreshWorker` vs `ModioMetadataRefreshWorker`（结构几乎相同）。
- 单条 UI：`mod_detail_panel` 按平台 if/else，Nexus/GitHub 不进线程。
- 批量：已统一到 `refresh_selected_mods_metadata`。
- 封面安装 / 文件夹重命名：Steam 与 Mod.io 各写一套。

**最小整理：** 一个 Worker + 平台 strategy callable；不要新建框架。

### 4.5 其它重复

| 对 | 说明 |
|---|---|
| `services/file_ops.ModFileManager` vs `services/mod_files.ModFileManager` | 同名不同职责；详情里 `as JsonMgr` 别名 |
| `services/conflict.py` vs `services/deploy_conflict.py` | 生产部署用前者；后者几乎只被测试引用 |
| `ui/edit_mod_dialog.py` vs `ui/mod_edit_dialog.py` | 面板用前者；后者只服务遗留 `mod_detail_dialog` |
| `mod_relations` vs `mod_relationships` | 两表；详情两边都读 |
| `_on_remove_mod` 定义两次 | 后者覆盖前者（见 §5） |

---

## 5. 死代码分析

置信度：高 = 全库无生产引用；中 = 仅测试/被覆盖；低 = 像临时代码但仍可能被手工跑。

### 5.1 未使用 / 被覆盖的函数与接线

| 符号 | 证据 | 置信度 |
|---|---|---|
| `ModLibraryView._on_remove_mod`（约 L865） | 同类中后一个定义（约 L1597）覆盖它 | 高 |
| `ModLibraryView._on_remove_mod`（L1597） | `detail_panel.remove_requested` **从未 `.connect`** | 高 |
| `ModDetailPanel.remove_requested` | 有 emit，无 slot | 高 |
| `test_raw_directory_rename` 内大量 `print` | `services/metadata_refresh.py` 诊断函数，生产刷新会间接触发打印 | 中 |

### 5.2 未引用或仅测试引用的模块

| 文件 | 证据 | 置信度 |
|---|---|---|
| `services/offline/generator.py` | 无 `import` | 高 |
| `services/offline/readable_snapshot.py` | 仅 `tests/test_readable_snapshot.py` | 高 |
| `ui/mod_detail_dialog.py` | 生产库用 `ModDetailPanel`；仅 `tests/test_offline_open_path_canonical.py` | 高 |
| `ui/mod_edit_dialog.py` | 仅被遗留 dialog 引用 | 高 |
| `services/deploy_conflict.py` | 生产 `deploy.py` 用 `services.conflict`；本模块几乎只在测试出现 | 中 |
| `services/offline/nexus.py` | 纯 re-export `nexus_manual`（有意别名，可留） | — |
| `_audit_mod_3761838546.py` | 根目录一次性取证脚本 | 高 |
| `scripts/trace_popup.py` | 手工弹窗追踪，不在 GUI 启动链 | 高 |

`core/scraper.py` **不是死代码**（`steam_api` 使用）。

### 5.3 Debug 遗留（仍挂在生产启动上）

| 文件 | 行为 |
|---|---|
| `ui/widget_show_trace.py` | `launch_gui` **无条件**安装；猴子补丁 `show`/`setVisible` |
| `ui/startup_lifecycle.py` | 启动 `print flush`；`MainWindow` mixin 记首次 paint |
| `ui/popup_trace.py` | `log_popup` 在卡片点击、tooltip、`show_mod`、loading overlay 上以 **warning** 打堆栈 |
| `ui/window_chrome.py` | `install_syscommand_probe`、`diagnose_and_bind_win32_taskbar_icon` 的 `print` |
| `services/metadata_refresh.py` | `test_raw_directory_rename` 诊断打印 |

这些不是「可删的死文件」，而是 **热路径上的临时代码**。对启动和点选的成本高于功能收益。

### 5.4 临时 / 探针数据（非业务代码）

仓库中可见、不应进入发布物：

- `data/_clean_*.txt`、`_garbage_*.txt`、`_zoom_*.html`、`_dms_text.txt`、`_ext_scan.txt`
- `data/startup_probe_chunk_*.txt`、`startup_probe_output.txt`
- `tests/_render_mod_card_layout.py`、`tests/_render_game_deploy_view.py`、`tests/_real_multi_archive.py`（手工渲染/实档脚本）

### 5.5 重复 UI / 表，清理需迁数据

- `edit_mod_dialog`（活）vs `mod_edit_dialog` + `mod_detail_dialog`（遗留）。
- `mod_relations` vs `mod_relationships`：不能当未引用删除，详情仍读旧表做冲突列表。

---

## 6. 优化路线图

原则：每次只改一条热路径；用现有测试（`test_library_card_cache`、`test_cover_loader`、`test_library_refresh_pure`、`test_metadata_refresh`、`test_detail_size_badge`）做回归；不改 DB schema（除非单独的 P2 索引 PR）。

### Phase 1 — 性能优化（优先，行为不变）

目标：启动可交互、切库/切卡不卡、点详情不再复制离线树。

| 步骤 | 修改文件 | 做什么 | 风险 | 收益 |
|---|---|---|---|---|
| 1.1 关闭生产诊断 | `main.py`；`widget_show_trace` / `popup_trace` / `startup_lifecycle` / `window_chrome` 加开关 | 默认 no-op；环境变量才启用 | 极低 | 启动、show、点击立刻变轻 |
| 1.2 推迟库 refresh | `ui/main_window.py` | `__init__` 不 `refresh()`；`showEvent`/`QTimer(0)` 再刷 | 低（需保留几何恢复） | 消除 show 前全库扫描 |
| 1.3 卡片批量字段 | `ui/mod_card.py`、`ui/library_view.py` | `rebind(..., search_fields, tag_flags, rel_counts)`；`refresh_display` 不再 `get_db()` | 中（徽章回归） | 打开库从 O(n) 查询降到 2～3 次批量查询 |
| 1.4 审计路径 map | `services/file_ops.py`、`services/deploy_audit.py` | `index_by_published_id()`；禁止循环内 `find_by_published_id` | 低 | 启动审计从 O(n²) 变 O(n) |
| 1.5 详情只读 | `ui/mod_detail_panel.py` | `show_mod` 去掉 `apply_sidecar_to_db` 与 `sync_metadata_backup` | 中（缺文件夹恢复要另测） | 切卡不再写盘/写库 |
| 1.6 体积徽章异步 | `ui/mod_detail_panel.py` | size 用 QRunnable；排除 `.info` 或缓存 | 低 | 切卡不再 rglob 离线资源 |
| 1.7 封面分发 + 缓存 | `services/cover_loader.py`、`ui/mod_card.py` | 单连接分发 token；LRU QImage | 中（重命名锁已有测试） | 滚动/切游戏少解码、少槽调用 |
| 1.8 游戏列表一次扫描 | `ui/library_view.py` | `_rebuild_game_list` 复用同一次 listing | 低 | 打开库少一半目录遍历 |

**刻意不做：** 虚拟化、合并平台类、改 schema、动离线抓取。

### Phase 2 — 架构整理（小步收敛，不重写）

| 步骤 | 修改文件 | 做什么 | 风险 | 收益 |
|---|---|---|---|---|
| 2.1 JSON 单读 | `info_sidecar.py`、`file_ops.py`、`library_view.py` | sidecar 基于 `read_info_metadata_dict` | 中 | 去掉双解析 |
| 2.2 刷新进线程 | `mod_detail_panel.py`、`metadata_refresh_thread.py` | Nexus/GitHub rescan 走现有 Worker | 低 | 详情刷新不卡 UI |
| 2.3 Worker 合并 | `metadata_refresh_thread.py` | 一个 Worker + callable | 低 | 少一份样板 |
| 2.4 Mod.io 封面/重命名 | `modio_metadata_refresh.py` | 调用 Steam 已有 helper，删复制块 | 中 | 行为一致、少分叉 |
| 2.5 备份时机 | `metadata_backup.py`、导入/刷新成功回调 | 成功导入/刷新/用户保存时 sync | 中 | 备份仍在，不在点选路径 |
| 2.6 接上或删除移除信号 | `library_view.py` | `remove_requested.connect` 或删死代码 | 低 | 消除双定义/断线 |
| 2.7 离线存在检测 | `library_query.py`、`mod_card.py` | 卡片只用 canonical `is_file` | 低 | 少 `iterdir` |

**刻意不做：** 合并两个 `ModFileManager` 类名（先别名文档化）；合并 `conflict`/`deploy_conflict`；合并离线 Playwright 两套。

### Phase 3 — 清理旧代码（确认测试绿后再删）

| 步骤 | 修改文件 | 风险 | 收益 |
|---|---|---|---|
| 3.1 诊断模块默认不加载 | 同 1.1；确认无回归后可移到 `scripts/` | 低 | 启动路径干净 |
| 3.2 删除/归档 `_audit_mod_3761838546.py`、`data/_*.txt`、probe chunks | 无运行时依赖 | 极低 | 仓库噪音 |
| 3.3 `offline/generator.py`、`readable_snapshot.py` | 先确认无插件式 import | 中 | 少 ~1400 行维护面 |
| 3.4 遗留 `mod_detail_dialog` + `mod_edit_dialog` | 迁移/删对应测试 | 中 | 详情只剩一条 UI |
| 3.5 `deploy_conflict.py` | 测试改为 `conflict.py` 或薄包装 | 中 | 一套冲突检测 |
| 3.6 `mod_relations` 表 | **需要数据迁移**，单独 PR | 高 | 延后到明确只读旧表之后 |
| 3.7 `update_sources` Steam stub / 缺 Mod.io | 产品决策后再动 | 中 | 避免两套平台路由长期分叉 |

---

## 附录 A — 热路径文件体量（便于估风险）

| 文件 | 约行数 | 角色 |
|---|---|---|
| `ui/mod_detail_panel.py` | ~4590 | 详情；点选热路径 |
| `core/db_manager.py` | ~3400 | 全部 SQLite |
| `ui/library_view.py` | ~2370 | 库刷新热路径 |
| `services/archive.py` | ~1900 | Steam 离线 |
| `services/offline/layout_snapshot.py` | ~1170 | Nexus/GitHub 布局快照 |
| `services/metadata_refresh.py` | ~1190 | Steam/批量刷新 |
| `services/offline/readable_snapshot.py` | ~1130 | 疑似死代码 |
| `ui/mod_card.py` | ~1040 | 每卡 N+1 |
| `core/mod_platform.py` | ~730 | 平台/文件角色 |

改这些文件时保持 **行为测试先行、一次一个主题**。

## 附录 B — 已有、应复用的能力

不要重写这些，把热路径接到它们上：

- `get_mods_search_fields` / `get_mods_tag_flags` / `get_relationship_counts`
- `_card_cache` + `ModCardWidget.rebind`
- `CoverLoaderManager`（差的是缓存与扇出，不是线程模型）
- `refresh_selected_mods_metadata`（批量门面已在）
- `OfflineManager`（UI 不要直接 new provider）
- `library_query.filter_and_sort`（纯 CPU，不是瓶颈）

---

**结论：** 功能面（四平台、离线、刷新、部署、封面）已经闭环。当前卡顿主要来自 **把正确的后台能力绕开，在 UI 线程上重复扫盘、重复读 JSON、按卡查库、点选时写备份**。Phase 1 的八个小步不改变产品行为，预期对启动、开库、切详情的体感最大。
