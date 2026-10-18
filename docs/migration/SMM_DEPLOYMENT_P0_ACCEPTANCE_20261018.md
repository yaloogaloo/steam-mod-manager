# SMM 部署 P0 / P1 验收记录 — 2026-10-18

**最终状态：NOT CLOSED。** 已提交边界明确的兼容修复、部署证据展示、注册偏好和星标修改；未执行任何生产部署、撤出、排序或状态恢复。真实资源仍有缺失及所有权证据不足，Stellaris 游戏实际加载未证明，强制回归门禁仍有两个失败。不能把本报告当作生产部署恢复完成。

证据目录：`D:/smm-deployment-evidence-20261018`。本文源码行号对应修复提交 `94856cc`；现场基线为 `5d1d947`。开始时已有的工作区改动全部保留。

## 逐项结论

| 验收项 | 状态 | 证据与限制 |
|---|---|---|
| Anno 1800 状态恢复 | NOT CLOSED | 当前 DB / Bridge / 正式 Qt 均为28，未复现当前全部未部署；仅一个187文件的真实历史对象通过全部只读兼容证明，未写生产数据 |
| Palworld 状态恢复 | NOT CLOSED | 当前三层均为0，14份历史清单映射后106/129文件存在；缺失23个，不能批量恢复登记 |
| 历史 Manifest 兼容 | NOT CLOSED | schema=2 可验证历史身份已有窄范围准入；schema=0/1 缺乏覆盖所有权证明，仍拒绝写入；共享及变更目标仍拒绝 |
| Stellaris 部署文件正确 | NOT CLOSED | 17/18 用户描述符及其目标存在；缺失 ugc_3818467946.mod |
| Stellaris 加载配置正确 | NOT CLOSED | dlc_load列出18项但包含缺失描述符；Launcher无激活Playset |
| Stellaris 游戏实际生效 | NOT CLOSED | 无足以证明18个 Mod 加载的 L4 日志或实际游戏验证 |
| 其他游戏部署一致性 | NOT CLOSED | 已覆盖最终18个正式游戏；存在缺文件、历史清单待核验和无样本情况，详见矩阵 |
| 注册方式记忆 | PASS | Nexus离线网页关闭/重开及正式进程重启恢复；跨游戏回退有效，文件路径为空 |
| 收藏星标视觉 | CODE FIXED / USER REVIEW PENDING | 正式 Qt 已截图，SVG、Hover和键盘焦点验证；最终视觉由用户评审 |
| 生产数据安全 | NOT CLOSED | Schema、部署字段、283份Manifest和核验目标未变化；最终库多一个游戏行及一个Mod更新时间变化，来源尚未确认，未回滚 |

## 1. 状态失真及历史拒绝的根因

2026-09-06 身份重建报告确认执行过替换库并激活。`tools/identity_full_rebuild_apply.py:144` 的 `_insert_mod` 在177行将每个新行部署状态初始化为 `not_deployed`，部署时间和路径置空。`tools/identity_rebuild_runtime_migrate.py:286` 的恢复查询及328行更新恢复收藏等字段，却不包含 per-Mod 的 `deploy_status / deploy_time / deploy_path`；它恢复的 `deployment_records` 是模板集合，不能替代每个 Mod 的当前登记。执行报告、映射及迁移报告已复制至证据 `baseline/`。

因此 Palworld 当前0登记与历史清单/现存文件并存有历史迁移缺口依据；不能将所有对象的历史状态精确归因到同一事件，也不能归咎于当前前端过滤。Anno当前已有28登记，当前“全为未部署”没有复现，不能声称已经修复一个本次未复现的展示问题。

原始报错最早引入 `919aa51` 的 `services/production_resources.py:334`、`ProductionResources.validate_deploy`。原条件把 schema<2 **或** `internal_id != 当前Frozen UUID` 全部拒绝。历史清单仍有 Workshop数字ID、旧PK或可选的内部身份；原服务已有 `.info/backups` 恢复及目标重映射，新资源准入层没有完整接纳原合法格式。

当前 `services/production_resources.py:290` 的准入保留身份、边界及备份检查。`services/deploy_manifest_compat.py:15` 的 `validate_legacy_v2` 仅接纳 schema=2、当前PK一致且归档目录证明当前实体的历史清单；写入前完整核验源哈希、ZIP对应成员、目标内容、覆盖前备份和同游戏目标共享。空清单、外来身份、变化文件、共享目标及缺备份证据均拒绝。无需改写Manifest或生产状态，未扩大游戏目录权限。

schema=0/1 继续拒绝写入，原因是这些实际记录未充分证明覆盖前备份及恢复所有权，而非单纯版本号不新。对于不能证明安全撤出的资源，本轮没有提供强制覆盖开关。

## 2. 修复前后真实状态

| 游戏 | SQLite / Bridge / Qt：前→后 | Manifest | 文件只读核验 |
|---|---|---|---|
| Anno 1800 / 916440 | 28/28/28 → 28/28/28 | 29份，28份schema2、1份schema0；哈希未变 | 全部历史旧绝对目标13696/13696存在；其中当前已登记对象12989/12989 |
| Palworld / 1623730 | 0/0/0 → 0/0/0 | 14份schema0；哈希未变 | 旧F盘目标0/129；按原规则映射当前E盘后106/129 |
| Stellaris / 281990 | 18/18/18 → 18/18/18 | 原Paradox启用Backend不依赖复制Manifest | 用户描述符及所指目录17/18，未修写配置 |

不一致对象按原始清单与逐文件记录保留在 `target-audit.json`、`remapped-target-audit.json` 及 `historical-admission-review.csv`。Anno和Palworld合计43个对象的审查分类：A=1（完整证明通过）、B=4（目标缺失/不一致）、C=0（未证实外来身份）、D=35（未完成所有权和全部内容证明）、E=3（共享目标风险）。B不能被表述为已证明用户修改，旧数字身份也不能直接判为C。

Anno代表对象：PK=1230，Frozen UUID=`f5b7f6aa-f1b7-4602-bf37-4da414e89dea`，归档 `E:/mod/Anno 1800/1905 - Trans-Ocean Liner`。187个目标、源ZIP成员及原备份完整只读验证通过，用时21.125秒（`anno-readonly-compatibility.json`），未部署或撤出。另有162个旧绝对目标被多个实体声明，保留证据，不能作为可清理重复文件。

列表DTO保留原始状态，失败/空/未知不再静默显示未部署。Inspector新增只读证据，区分登记、Manifest待兼容、目标不一致和状态无法确认；canonical已部署集合仍以原SQLite合同为准，不用外部加载配置删减集合。目标存在只证明部分L2，不证明内容与L4。

## 3. 三版本合同及 Stellaris 调用链

对比源码和diff保存于证据 `baseline/`，函数精确行号另见 `source-locations.json`。

| 环节 | A：旧正式tag `f1dadba` | B：准入层前 `df1bec3` | C：现场 `5d1d947` → 修复 `94856cc` |
|---|---|---|---|
| 正式入口 | `main._run_gui:222` 默认原MainWindow | `main._run_gui:213` 原MainWindow | `main._run_gui:219` 正式WebWindow |
| 部署入口 | `LibraryView._on_deploy_action:4058` | 同函数4058 | `LibraryBridge._start_deploy:298` |
| Worker | `DeployWorker._run_operation:98` | `DeployWorker.run:79` | `DeployWorker._run_operation:98` |
| Service | `ModDeployer.deploy_mod:1769` | 同函数1765 | 同函数1769，沿用原业务 |
| Backend解析 | `resolve_deploy_type:101` | 同函数101 | 同函数101，Stellaris仍为paradox_launcher |
| 原Paradox完成路径 | `_finish_paradox_activation_deploy:3474` | 同函数3465 | 同函数3474 |
| 描述符映射 | `map_to_launcher_id:736` | 同函数736 | 同函数736，新增本体存在检查 |
| Launcher同步 | `sync_paradox_launcher:1211` | 同函数1211 | 同函数1224 |
| Manifest读取 | `load_manifest:183` | 同函数183 | 同函数183，原解析未重写 |
| 新资源边界 | 当时路径无本轮新增准入 | 无本轮新增准入 | `ProductionResources.validate_deploy:290`，补窄范围历史证明 |

Stellaris没有退化为通用复制，也没有调用测试路径投影；`resolve_deploy_type` 原有特殊分支优先于游戏配置的泛用folder_copy。原路径负责原生Workshop身份、已有用户 `.mod`、`dlc_load.json` 和Launcher数据库投影，不会凭空生成缺失的用户ugc描述符。本轮没有另写复制算法，也没有自动启用全部归档。

原Service会把某些异常写入 `projection_errors` 后返回成功；原Worker已将这些error转换为失败。但“描述符未解析”只进入 `unresolved`，没有进入errors，仍可能误报成功。本轮 `ModDeployer._annotate_paradox_launcher_result:3570` 将未解析、直接配置写入失败和Launcher同步警告明确报告为部分失败，`game_load_verified=False`；Worker保留真实的 `partial_state_saved`，不伪称DB写入成功。canonical登记与投影失败分别报告。

原resolver得到的实际用户目录为 `C:/Users/king/Documents/Paradox Interactive/Stellaris`，没有硬编码到业务实现。当前 `dlc_load.json` 含18项，其中 `mod/ugc_3818467946.mod` 缺失；对应原生Workshop目录和内部 `descriptor.mod` 存在。其余17个用户描述符指向存在的目录。Launcher无激活Playset，effective集合为空。日志有部分描述符 supported_version 报错，但不足以逐Mod证明实际加载。不能据此断言18个全部生效，也不能断言17个全部无效。

L1登记一致；L2缺少1个用户描述符；L3有无效引用且Launcher未激活Playset；L4未闭合。恢复描述符需要沿用Steam/Launcher注册工作流并确认用户实际启动方式和Playset，不能通过重写全部配置替代。

## 4. P1：偏好与收藏

注册偏好复用 `QSettings('SteamModManager','WorkshopLibrary')`，Windows位置 `HKEY_CURRENT_USER/Software/SteamModManager/WorkshopLibrary`，键 `ui/registration/{appid}`。仅白名单保存 `kind/platform/localPlatform/importKind`，不保存API Key、临时文件路径或上一个Mod身份，不写SQLite。按游戏隔离，恢复时不支持的选项回退有效默认值。

`registration-select.json` 与 `registration-restart.json` 证明：真实正式可写入口选择Nexus离线网页→关闭→重开保持→新进程重启保持；切换Palworld为有效Steam方式，路径均为空，未注册或下载任何资源。

收藏复用现有SVG体系，32×32点击区、19px图标，空心/实心金色、轻量Hover及明确键盘焦点，支持减少动态效果。Inspector星标数量为0。正式生产只验证Hover/Focus，未点击更改收藏；嵌套SVG点击不触发卡片选择/详情且收藏持久化，已在隔离Qt测试验证。

## 5. 正式 Qt 截图及验证边界

通过正式 `main.main` 入口运行Qt WebEngine，未使用浏览器Demo。1728×900逻辑尺寸，DPR=1.25，对应2160×1125物理截图。只读实际资产：Anno106、Palworld200、Stellaris390、WH3 535等；最终补查18个正式游戏成功，`delivery-qt.json` 记录结果。

所有截图位于 `D:/smm-deployment-evidence-20261018/`：

- 修改前：`before-916440.png`、`before-1623730.png`、`before-281990.png`。
- 修改后：`delivery-916440.png`、`delivery-1623730.png`、`delivery-281990.png`；此前同版核验为 `final-*.png`。
- 星标正式正常模式：`normal-star-hover.png`、`normal-star-focus.png`，检查值 `star-interaction.json`。
- 注册方式：`registration-select.png`、`registration-restart.png`。

静态源码检查、隔离临时根部署回放、真实Qt生产只读核验分开记录。没有执行生产收藏切换、部署、撤出、加载顺序修改或真实游戏L4启动验证；测试通过不能代替这些尚未执行的验收。

相关回归：`targeted-release.txt` 50通过，`activation-final.txt` 55通过，`worker-release.txt` 24通过；最终只读证明及Web部署组合 `readonly-proof-release.txt` 29通过。这些组合存在重叠，不能相加作为独立覆盖数量。

AGENTS要求的canonical门禁最新 `canonical-delivery.txt` 为 **136通过、2失败**：`test_sort_mode_library_order_and_viewport`、`test_filter_row_does_not_keep_phantom_height`，均为WH3旧布局断言。`baseline-wh3-layout.txt` 使用原Service结果处理同样复现2失败；测试销毁过程中另有旧QScrollArea已删除回调日志。没有通过修改无关布局或放松断言使其变绿。**门禁未通过，不能结项。** 本轮未触及cover/lifecycle gate列出的文件。

## 6. 文件、提交与生产数据安全

本轮实际代码文件：

- 部署与证据：`core/db_manager.py`、`services/production_resources.py`、`services/deploy_manifest_compat.py`、`services/deployment_inspection.py`、`services/deploy.py`、`services/paradox_activation.py`、`ui/deploy_thread.py`、`tools/deployment_consistency_audit.py`。
- 正式前端：`frontend/web_bridge.py`、`frontend/web/app.js`、`frontend/web/registration.js`、`frontend/web/style.css`、`frontend/registration_preferences.py`。
- 回归：`tests/test_deployment_compatibility_evidence.py`、`tests/test_registration_preferences.py`、`tests/test_stellaris_activation.py`、`tests/test_ck3_sort_mode_ui.py`、`tests/test_web_business.py`。
- 文档：本报告及 `docs/migration/SMM_DEPLOYMENT_COMPATIBILITY_MATRIX.md`。

领域分开提交：`40f1175`（部署兼容/部分失败）、`445e20b`（注册偏好）、`0e2479b`（SVG星标）、`7ba8f38`（未知状态/空清单）、`94856cc`（纯只读证明及共享目标拒绝）。未将开始前的DB、映射文件、其他测试和视觉变量改动混入这些提交。

阶段A已用SQLite backup API建立一致性恢复点 `baseline/mod_manager.db`，integrity_check=ok，SHA256=`56c20f0cc2eaba492bbfbd45cd282e09fca059a0ad2b1813d48e81ae3fe2f78d`，未仅复制活跃主库。原Git状态、源码、身份映射、283份Manifest清单及哈希、目标元数据和必要的小配置/Launcher数据库恢复点均已保存，没有复制612GB归档。

最终 `production-safety.json`：Schema相同、部署字段相同、283份Manifest哈希无变化、所有原记录目标的存在性/大小/mtime无变化、代表性当前目标哈希无变化，生产部署命令数0。此结论不是对612GB所有字节做完整哈希证明。

**最终数据库全表指纹不相同。** `database-final-delta.json` 逐字段显示：新增 `games.app_id=2054972`（龙之信条Ⅱ，updated_at=2026-10-18T09:54:39+00:00），Mod PK=3770023545仅 `updated_at` 从2026-10-17T10:52:00+00:00变为2026-10-18T07:13:42+00:00。所有其他行字段相同。尚未建立这两处差异的写入来源，不能声称生产库完全未变；未删除或回滚这些现场记录。新增游戏已在正式Qt只读复核，0个Mod，矩阵标无样本。

## 7. 未闭合工作与恢复边界

跨游戏完整结论见 [SMM_DEPLOYMENT_COMPATIBILITY_MATRIX.md](SMM_DEPLOYMENT_COMPATIBILITY_MATRIX.md)。最终18游戏均独立列出，无样本不判PASS，352个对象逐项CSV/JSON供复查。

历史v0所有权重建、Palworld缺失目标、Stellaris用户描述符/Playset恢复和L4验证仍待完成。本轮没有执行生产迁移，也没有将未知状态改成已部署。首次真实部署或配置修复必须先列出具体目标文件、覆盖前备份/恢复点、预期操作和数据影响，再由用户确认；本报告不构成自动写入授权。

代码回退应按上述领域提交逐一检查后反向撤销，保留现场原有工作区改动；无本轮生产部署文件需要撤回。不要用阶段A整库覆盖当前生产库，因为已发现后续现场差异。两个强制门禁失败及数据库差异归因也必须解决后才能结项。
