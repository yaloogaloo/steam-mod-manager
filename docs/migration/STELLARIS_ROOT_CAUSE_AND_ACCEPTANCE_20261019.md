# Stellaris 根因与生产验收 — 2026-10-19

根因归类 PASS；最小修复已实现。生产数据安全在用户接受的范围内 PASS。总体仍为 NOT CLOSED：强制回归门禁还有两项旧 WH3 布局失败，Mod 内容兼容错误未处理。

## 调用链和历史证据

旧 Qt 和可写 Web 都使用 `DeployWorker → ModDeployer → paradox_launcher / ParadoxOrderBackend → paradox_activation`。首个 Web 提交 df1bec3 是只读入口；919aa51 接入可写命令后仍复用原 Worker、Service 和 Backend。没有另建 Stellaris 算法。虽 SQLite `deploy_type=folder_copy`，原解析器会为原生 Stellaris 激活方式选择 paradox_launcher，不是 Web 误路由。

旧版 df1bec3 父提交的 `_annotate_paradox_launcher_result` 明确不改变 success；无激活 Playset、投影文件写入失败仍可成功。这是旧业务隐藏的错误。后来部分成功改为如实失败，但通用重部署仍先撤出、再检查 success；纯激活撤出已删除 canonical member 后，投影失败使重部署中断并把记录标成 failed。这是新增失败处理与重部署衔接的回归，两个前端调用同一 Service 都会遇到。

证据 `D:/smm-card-size-stellaris-20261019/history` 和 `old-new-result-replay.json`：用相同隔离依赖重放历史/当前函数，无 Playset 与写入失败两种情况下，旧版 success 为 true、当前为 false。`stellaris-before-tests.txt` 在修复前复现两项成员丢失失败。

`services/deploy.py::redeploy_mod` 仅对无自定义部署路径的 Paradox 原生激活重部署直接调用原 `deploy_mod` 重做投影，避免无复制文件可撤出的情况下先丢成员；自定义文件复制分支保持原样。未放宽成功判据、修改 Launcher 算法或创建新的业务接口。新增测试覆盖缺描述符、无激活 Playset、写入失败、重复失败及修复后重试/撤出。

## 隔离与正式 Qt

- 定向部署测试 10 passed；完整相关组 95 passed / 1 skipped，之后大小回滚补充 1 passed。
- 正式 `main.py --validation-root`、真实 Qt WebEngine、原 QWebChannel/Worker/Service 完成部署、重部署、撤出、再部署、无 Playset 时失败但保留已部署成员，以及依赖恢复后的成功重试。`business-isolation/acceptance.json`、`stellaris-*.png`。
- 强制八文件 canonical 门禁：136 passed / 2 failed，见 `canonical-gate.txt`。失败为旧 `test_sort_mode_library_order_and_viewport` 与 `test_filter_row_does_not_keep_phantom_height`，有旧 QScrollArea 删除回调异常。本轮未改相关 WH3 UI，也未降低断言；按 AGENTS.md 不把任务整体视作完成。

## 已批准的生产操作

批准原文及冻结计划 SHA：`approved-production/approval.json`。正式入口启用原生产上下文，未绕过资源隔离。

原 Launcher 启动后自行恢复 `mod/ugc_3818467946.mod`，注册为 ready_to_play，path 指向原 Workshop `281990/3818467946`；原生 descriptor.mod SHA 保持 `de0cf1da9e045c07c020c012b1d73c9e31e311d75208b0e174819715938f9103`。没有手工生成、覆盖描述符或修改 Workshop 内容。

原 Launcher UI 新建并激活 `SMM verification 18 - 20261019`，ID `9eaffd2e-d355-4bfb-b646-130c3b322fff`。成员恰好等于批准的 18 项且全部启用。原 Initial playset（380 项、19 启用）及 111（0 项）的成员、顺序、启用值完全一致。

第一次验收脚本对 failed 状态请求 redeploy，被原 UI 状态校验拒绝，未执行部署。随后按既有界面“重试部署”恢复指定 UUID，再对同一对象执行重部署，两次均成功。记录从 failed 变 deployed，原 17 项未改；正式 Qt 回读 18 已部署，console 为空。证据 `approved-production/redeploy-result.json`（拒绝调用）、`redeploy-retry-result.json`（成功）及 `stellaris-final-redeploy-qt.png`。

SQLite 仅该对象的正常部署字段及其 `updated_at` 变化；大小校准另限已批准四列。没有其他 Mod 业务字段、其他表或 Schema 变化，托管归档资源守卫一致。精确字段前后值见 `approved-production/final-safety.json`，全部逐行差异见 `all-mod-diffs.json`。

## 分层结论

L1 SQLite：18 已部署，PASS。L2 文件：18/18 外层描述符及目标存在，缺失项由原 Launcher 恢复，PASS。L3 配置：新激活 Playset 和 dlc_load.json 都是批准的 18 项；两种启动后的配置集合一致，PASS。

L4：从 Launcher 开始游戏及直接 stellaris.exe 均实际进入主菜单，校验值同为 `102f`；UIOD 标记、字体、Azur Lane 菜单资源可见，新游戏日志记录 Mod 独有脚本解析。两次均正常从菜单退出，没有进入存档或创建/覆盖存档；5 个既有存档文件的路径、字节数、mtime 全部一致。证据 `launcher-game-main-menu.png` / `direct-exe-game-main-menu.png`、`after-launcher-game/logs` / `after-direct-game/logs`、`final-safety.json`（均位于 approved-production）。这证明引擎实际加载了 Mod，不能证明 18 项的每项游戏功能都兼容；新 error.log 仍有缺 trait 等内容错误，本轮不改 Mod 内容。

## 已接受的 Launcher 自动刷新结果

原 Launcher 的订阅发现不是单文件操作。批准前数据库 381 条注册，打开后 391 条：除目标 3818467946 外还发现 9 个已订阅对象；113 条既有注册的 displayName、requiredVersion、size、tags、thumbnailPath/Url、timeUpdated 或 version 被原生刷新。此处 tags 是 Launcher 元数据，不是 SMM 用户标签。没有删除注册，也没有改变两个原 Playset 成员。

精确文件清单是 **1 份缺失描述符恢复 + 7 份既有描述符更新**（早期口头“8 份已有”把恢复项包含在内）。既有项为 `2997295407, 3292390779, 3661244914, 937289339, 2735054420, 1623423360, 3243731913`。这些由原 Launcher 刷新，SMM 后端未写描述符。额外注册/元数据清单见 `launcher-mod-registration-diff.json`；描述符前后 SHA 见 `final-safety.json`。

冻结计划原本只批准恢复缺失描述符，这些额外变化已如实报告。用户随后明确回复“接受保留原 Launcher 刷新结果”。2026-10-19 的只读复核确认 Launcher mods/playsets/playsets_mods、18 份描述符 SHA、SMM 全部表及 Schema、存档清单仍与供审查结果一致。本次只记录验收，没有新增生产业务写入。决定原文、冻结计划 SHA 和复核结果见 `approved-production/launcher-native-refresh-user-acceptance.json`；原计划及历史证据保留。

旧描述符仅有基线 SHA，尚未找回匹配的原字节，仍不能承诺七份文件可字节级回滚；用户接受当前结果不改变这一限制。`descriptor-rollback-availability.json` 保留该事实。今后若要求恢复，须先找到与基线 SHA 匹配的原始字节，另定具体对象和方法；不能用整库回滚覆盖后来变化。

## 恢复证据与边界

`approved-production/before-launcher-v2.sqlite` 是启动 Launcher 前的一致性快照；`before-redeploy-retry-mod_manager.db` 是执行 SMM 恢复前的快照，dlc_load.json 另有即时副本。大小四列可按 size-ledger.jsonl 条件回滚；目标部署字段可按 final-safety.json 条件恢复。新 Playset 只能在确认未被后续编辑后通过原 Launcher 移除，原两套成员不覆盖。缺失项的新描述符仅能在当前 SHA 仍匹配创建后记录时撤销。原部署服务还生成了 `data/projection_recovery/281990` 恢复点，并按原行为更新顺序文件、清除 Stellaris 的旧 Playset 提示缓存（CK3 提示保留）。生产 SQLite、Playset、描述符及顺序文件不进入 Git 提交。
