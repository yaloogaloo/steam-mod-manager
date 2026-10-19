# 部署工作台三项 UI 整改验收 — 2026-10-19

范围仅为说明区压缩、原位 Inspector、快照空白。没有修改部署/排序 Backend、快照 Service、SQLite/DAL/Worker、Manifest、CAS、归档、注册或卡片布局。证据根目录：`D:/smm-deployment-ui-20261019`。用户视觉评审 **PENDING**，候选截图不能代替最终视觉签收。

## 根因与改动

1. 当前部署标题区高 68px：h2 上下 margin 与说明段默认 margin 叠加；`#deployment-status` 另有 24px min-height，加上下 margin 共占 48px，且初始化文字和成功信息常驻。现有规则改为一行次级说明，标题区 48px，空状态不占位。完整成功保留 3.5 秒；保存中、错误、部分投影失败及成员警告仍显示原真实反馈。文案仅在 `data.enabled` 时承诺自动保存；只读、受限或无 Backend 时仍显示原能力原因。
2. `deployment.js` 的列表详情和 `snapshots.js` 的成员详情均显式调用 `showSpace('library'); inspect(asset)`，因此导航跳转是现有处理器的直接行为。提取原 Inspector 的信息部分为共用 `inspectorContent` 和 `renderInspectorDescription`；工作台按同一 token 请求原 `detail` DTO，复用原封面、权威类型、用户标签、大小及安全描述 HTML。没有第二套 DTO 映射或描述清洗实现。
3. 快照 `#snapshot-status` 空段虽然高度为 0，但仍贡献上下共 28px margin；标题段另有上述间距。空段移出布局，标题到列表设为 12px，保留原左右结构、成员集合和比较规则。

工作台 Inspector 仅包含同样的信息区域与关闭按钮，不复制资产库完整详情/部署/编辑/撤出/更多操作栏。类型值沿用原控件外观，在查看场景禁用写入型属性操作。当前部署复用原右侧辅助区，关闭恢复；快照详情临时使用原成员比较区域，关闭恢复原比较和滚动位置，不新增第三列或永久面板。

## 文件与关键入口

| 文件 | 本轮变更 |
| --- | --- |
| `frontend/web/app.js` | 提取信息渲染 `inspectorContent`、`renderInspectorDescription`；原资产库仍调用相同内容；封面完成时更新工作台当前图片；load 清理失效工作台详情 |
| `frontend/web/deployment.js` | `orderFeedback` 管理既有状态文本；`openDeploymentInspector` / `drawDeploymentInspector` / `closeDeploymentInspector`；详情改走原位显示；排序 RPC、顺序构造和 Backend 不变 |
| `frontend/web/snapshots.js` | 详情调用同一个原位函数；切换快照/页签清理旧详情；CRUD 请求和比较规则不变 |
| `frontend/web/acquisition.js` | 离开工作台时清理工作台详情；没有修改获取/注册逻辑 |
| `frontend/web/index.html` | 原辅助区域容纳共享详情节点；原快照右区增加可切换展示槽 |
| `frontend/web/style.css` | `.deployment-heading`、状态空段、`.deployment-body`、列表/侧栏及快照右区的高度约束；复用 Inspector 规则，资产库选择器既有呈现不变 |
| `tests/test_deployment_workbench_ui.py` | 两视口真实 Qt，临时资源内执行排序、失败/重试和快照操作；测试滚动、无图、多标签、长描述及迟到响应 |

产品新增约 110 行，主要是原位详情的状态生命周期；没有新增前端依赖。沿用源文件 CRLF，差异检查使用 `git -c core.whitespace=cr-at-eol diff --check`。

## 正式入口测量

所有正式取证通过 `main.main(['--read-only'])` 使用实际数据库和资产。用户关闭原实例后正常获取实例锁，没有终止用户进程或删锁。修改前使用本轮冻结的原 HTML/CSS/JS，由正式入口和同一真实 Qt 装配回放；不是静态 HTML Demo。冻结源保存在证据根目录的 `before-*` 文件。

| 逻辑视口 / DPR | 测量项 | 修改前 | 修改后 |
| --- | --- | --- | --- |
| 1728×900 / 1.25 | 标题及说明整体高 | 68 | 48 |
| 同上 | h2 实际高 / 说明实际高 | 27 / 17 | 27 / 17 |
| 同上 | 状态段实际高 / 加 margin 占用 | 24 / 48 | 0 / 0（无操作时） |
| 同上 | 第一条 Mod 顶部 Y | 324.4 | 268.4（上移 **56px**） |
| 1180×760 / 1.25 | 第一条 Mod 顶部 Y | 274.1 | 218.1（上移 **56px**） |
| 1728×900 / 1.25 | 快照标题顶部 Y | 212.4 | 208.4 |
| 同上 | 标题说明区域底部 Y | 276.4 | 256.4 |
| 同上 | 快照列表顶部 Y | 304.4 | 268.4（上移 **36px**） |
| 同上 | 标题说明底部到列表空白 | 28 | **12** |
| 1180×760 / 1.25 | 快照标题 / 说明底部 / 列表顶部 Y | 162.1 / 226.1 / 254.1 | 158.1 / 206.1 / 218.1 |
| 同上 | 中间空白 | 28 | **12** |

原生截图分别为 2160×1125、1475×950。字体和说明行高没有缩小，没有负 margin、位移或绝对定位。高度收益来自去掉多余间距和空状态占位。

## 原位详情与交互证据

修改前点击链：`deployment-list.onclick → showSpace('library') → inspect → rpc('detail')`。修改后：`deployment-list.onclick → openDeploymentInspector → S.assets 的原身份/token → rpc('detail') → 原 Bridge/Adapter/安全描述渲染 → 共用 Inspector 信息组件`。快照成员使用同一函数。请求及页面状态见每轮 `results.json` 的 `reads`、`stages` 和 `navigationCalls`。

正式样本包括 UI Overhaul Dynamic、舰船预设蓝图、`[简中字体]旗黑 大字号 26.01.01（兼容高清UI）`、小狐狸的底层框架（原描述 4,126 字符，安全 HTML 4,007 字符）。逐一与资产库 Inspector 的身份区、来源/大小、类型、用户标签、Workspace ID、安全描述 HTML 和封面 data URL 比较，要求完全一致；详情点击期间页面始终为 deployment、游戏仍为 281990、18 项顺序不变、没有资产库导航调用。

小窗口首轮发现打开详情后滚动从 696.8 变为 703.2：右侧宽度变化触发浏览器滚动锚定。现已显式保留列表位置并禁用该列表自动锚定；最终小窗口同样样本保持 **696.8→696.8**，标准视口为 **607.2→607.2**。快照比较区也保留关闭前滚动位置。不是通过改排序或重建列表实现。

当前 18 个真实已部署对象**全部有封面**，见 `deployed-cover-inventory.json`。没有为验收创建生产 Mod 或修改封面。无封面覆盖采用两份独立证据：隔离已部署对象（真实 Backend、临时资源）及现有真实快照中的非当前部署成员 `6ae75f49-c88c-4b68-85dc-8ef4422ebd02`。后者和资产库内容一致，但不冒充“当前已部署无封面”。这项指定生产样本覆盖缺口保留。

生产现有快照只有一份，完成其打开、成员详情和关闭恢复。不同快照切换、创建等写入测试使用两份隔离快照，没有写生产快照。快照仍只记录成员集合，不能恢复部署或保存顺序。

隔离 Qt 检查同时覆盖：长标题、120 行描述、多标签/无标签、无封面、详情切换不替换排序列表节点；上移、下移、置顶、置底及 HTML DragEvent 触发实际隔离 Backend；OS 只读投影文件触发部分失败，状态和重试保持可见；完整重试成功后状态自动收起；跨游戏迟到响应不能恢复旧详情。没有将合成 DragEvent 宣称为物理鼠标拖动验收。

## 证据与验收状态

正式截图与最终测试结果见下方补充索引。生产数据库按关闭实例后的 `production-before.db` 逐表比较；正式探针拦截并拒绝业务命令，同时记录所有只读请求。启动前已存在的数据库及 `mapped_games.json` 工作区差异保留且不提交。旧封面审计中的全库采样不作为本轮全量归档安全基线，本轮不声称扫描或逐字比较了全部归档。

| 验收项 | 状态 |
| --- | --- |
| 加载顺序说明区压缩 | PASS，第一行上移 56px |
| 部署工作台原位 Mod 详情 | PASS（可用生产样本）；指定的生产已部署无图样本不存在，覆盖缺口 NOT CLOSED |
| Inspector 数据与资产库一致 | PASS，原接口/原渲染复用且逐样本比对 |
| 部署快照间距优化 | PASS，空白 28→12px，列表上移 36px |
| 加载顺序行为保护 | PASS，实际写入仅在隔离资源执行 |
| 生产数据安全 | PASS，13 张表及 schema 一致、integrity_check=ok、9 份固定原图哈希一致、正式业务命令 0 |
| 用户视觉评审 | PENDING |

整体保持 **USER VISUAL REVIEW PENDING / NOT CLOSED**。不把自动技术通过等同于用户视觉批准。

## 最终证据索引

路径均在 `D:/smm-deployment-ui-20261019`；汇总为 `acceptance-index.json`。

- 修改前正式 Qt：`before-formal-1728/results.json`、`before-formal-1180/results.json`，均 PASS。截图 `01-current-default.png`、`02-detail-existing-navigation.png`、`03-snapshots-default.png`。
- 修改后正式 Qt：`verified-formal-1728/results.json`、`final-formal-1180/results.json`，均 PASS。两个视口 JavaScript console 为空、业务命令均为 0。详情 `detail-0.png`（UI Overhaul）、`detail-1.png`（舰船预设蓝图）、`detail-2.png`（长标题）、`detail-3.png`（长描述）；快照为 `04-snapshots-default.png`、`06-snapshot-detail.png`、`07-snapshot-restored.png`、`07b-snapshot-no-cover.png`。原位详情期间 `navigationCalls=[]`，顺序仍为原 18 项。
- `regression.txt`：**50 passed**。覆盖 `test_deployment_workbench_ui`、`test_web_order`、`test_web_snapshots`、`test_web_deploy`、`test_web_type_tag_independence` 及必需的封面三个测试文件。最终滚动保护修改后，`final-interaction.txt` 再验 **3 passed**（本轮两视口 + `test_inspector_four_fixes`）；没有将重复运行次数当作新增用例数。
- 封面加载回调涉及共享资源，因此执行项目封面门禁：`cover-125-standard`、`cover-125-small`、`cover-100-standard`、`cover-100-small` 各自 `results.json` 自动检查 PASS，真实 Qt、两个视口 × DPR 1.25/1.0。未修改卡片布局或已批准参考；退出码 2 表示仍需用户视觉批准，不是技术失败。
- `production-safety.json`：关闭原实例后的数据库逐表/逐行及 schema 比较 PASS；13 张表、9 份固定封面原图。未以 Git 文件状态代替数据库内容核对。正式入口为只读，探针额外拒绝所有业务命令；没有生产排序、快照或归档写入。工作区原有两个数据文件差异不纳入提交。
- 可复核探针保留 `probe.py`、冻结 `before-*` 源、隔离测试截图/布局 JSON、全部通过与失败记录；没有清理隔离资源或删除历史证据。

一次标准视口正式探针在快照无图成员读取时超时，记录保留在 `final-formal-1728/results.json`。没有改产品代码或扩大等待预算，复核同一路径于 `verified-formal-1728` 和小窗口均通过；不能据此编造超时根因或宣称性能稳定性已证明。增加只读请求计时的复核中，单次最长读取约 11.95 秒；详情读取延迟作为现存待观察问题，本轮未改读取服务。

本轮自动化拖拽使用真实 Qt 页面中的合成 DragEvent，隔离投影文件实际变化；没有将其或只读生产禁用按钮冒充物理鼠标拖拽写入验收。手动视觉评审与生产已部署无封面样本覆盖仍未关闭。
