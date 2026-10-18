# SMM 全游戏注册与库界面验收 — 2026-10-18

本轮修复正式前端的来源准入缺口，保留原注册Service、DAL、归档及部署安全检查。功能验收与最终视觉评审分开；没有执行生产注册、导入、同步、部署或撤出。

| 验收项 | 状态 | 结论 |
|---|---|---|
| Nexus MHTML注册 | PASS | 用户REFramework真实页面完成解析、隔离注册、离线归档及重新打开数据库验证 |
| 全游戏手工来源注册 | PASS | 五种来源不再受离线适配层游戏白名单限制，详见能力矩阵 |
| 注册身份安全校验 | PASS | 未知映射明确确认；已知冲突拒绝；无效ID、伪域、变化输入和重复均独立处理 |
| 注册方式偏好 | PASS | 正式新进程恢复Nexus离线方式；跨游戏默认有效，路径为空；偏好不授予注册权限 |
| Inspector信息治理 | PASS | 正常状态不显示部署内部诊断，异常保留简明提示和底层证据 |
| 收藏与勾选视觉统一 | CODE FIXED / USER REVIEW PENDING | DPR1.25实际Qt测量及交互验证完成，外部布局相同，最终视觉待用户确认 |
| 生产数据安全 | PASS（核验范围见下） | 本轮起始一致性快照与最终库所有表及Schema一致；283份Manifest哈希不变，生产注册提交0 |

## 1. 拒绝来源的准确原因与历史

原错误来自现场基线 `61a762a` 的 `services/importers/offline_admission.py:78`、`inspect_page`。它已经完成原MIME提取和Nexus元数据解析，却用仅含5款游戏的 `NEXUS_GAMES` 查当前Steam AppID；查不到就抛出“当前游戏的 Nexus 离线来源对应关系尚未准入”，随后 `frontend.offline_registration.preview` 将该文件行归类为失败并禁用勾选。

Git可追溯首次含此文案的提交为 `f1dadba`（2026-10-15聚合基线，不能据此推断更早的开发时间）；`919aa51`（新UI重构）也含相同检查。`git log --reverse -S`及三版本错误行号记录于证据 `source-locations.json`。旧原生Worker的 `_do_batch_offline_html_import` 在未绑定新资源适配层时走原批量导入流程，不经该5游戏检查；正式Web入口绑定资源后进入 `import_scoped_batch`，使隐藏限制影响实际使用。

这不是Nexus API拒绝，也不是把Steam AppID直接当作Nexus游戏数字ID。它是适配层自建游戏slug表被当成手工登记权限。原 `core.mod_platform.get_available_sources:573` 已允许五种来源，后加适配限制与其冲突。

用户文件是批量界面中的一个文件项目；“批量”允许仅选一个文件。本次真实输入解析成功：

- 路径：`D:/Temp/REFramework-at-Dragon-s-Dogma-2-Nexus-Mods-and-community-www.nexusmods.com.mhtml`。
- SHA256：`362521d804166ad175c3797ad106adb0aa794ee32ea007b8d53b78136600061e`，大小2691610字节。
- 名称：REFramework；Nexus URL：`https://www.nexusmods.com/dragonsdogma2/mods/8`；55个离线资源。
- 原适配层对相同隔离副本重现旧拒绝；修复后状态为“待确认归属”，不再错误分类为解析失败。

## 2. 修复合同与完整调用链

正式路径：`registration.js / offline_registration.js` → `LibraryBridge` 的 `offline-preview` → `frontend.offline_registration.preview:11` → `offline_admission.inspect_page:46` → 原 `extract_mhtml / parse_nexus_offline_html` → 解析DTO及重复检测 → 明确选择/确认 → `offline_registration.start:39` → 原 `ImportWorker` → `offline_admission.import_scoped_batch:124` → 原 `NexusImporter / identity_service / materialize` → 原 `attach_nexus_offline_page`、CAS、DAL及内容状态持久化。

`services/importers/nexus_registration_identity.py:6` 校验官方HTTPS域名、slug和正整数Mod ID；`:21` 的 `check_game` 复用已有游戏身份映射及当前游戏既有Nexus来源作为证据。已知游戏冲突要求纠正，未知映射可明确确认本项归属，不修改官方API映射，不把AppID转换成Nexus游戏ID，不从中文游戏名编造slug。

只提供数字ID时明确提示尚未确认网页游戏，可补完整链接或确认当前归属。有效网页只有ID、缺少URL时，原预览行提供补充链接输入，核对页面ID一致后重新预览；没有有效Nexus页面身份的HTML不会因为填了链接就被强行接受。

确认与所选游戏、快照、页面指纹和具体条目绑定。提交前重验文件和来源，单文件同时核对伴随资源指纹；批次逐项失败保留其他成功结果，重复项跳过而不覆盖已有用户字段。URL/本地内容预览与直接导入命令都保留确认检查，不能绕过预览直接放行。附加网页与手填URL同时比较游戏slug及Mod ID，不能只比较数字。

单项HTML/MHTML适配也复用相同身份检查和原离线归档服务，消除原单项仅HTML/4MiB与正式批量HTML/MHTML路径的不一致。资源大小、路径、归档所有权、失败补偿及原Mutation Context没有放宽。

横向审计发现的其他问题：Steam输入先提取ID会丢失原链接域名，mod.io首页可缺乏真实Mod身份；已分别补充原始官方域名及Mod链接/正整数检查。不是新增游戏平台白名单。本地输入也应用来源地址校验。各类能力与自动API边界见 [SMM_REGISTRATION_CAPABILITY_MATRIX.md](SMM_REGISTRATION_CAPABILITY_MATRIX.md)。

## 3. 真实解析与隔离持久化

`D:/smm-registration-evidence-20261018/real-registration-result.json` 保存原错误、完整预览DTO、Worker逐项结果及重开数据库后的字段。实际结果为成功1、跳过0、失败0；归属AppID=2054972，来源为上述真实URL，名称REFramework，内容状态 `content_missing`。托管目录及 `.info/offline/index.html`、`.info/offline/manifest.json` 均存在；原Worker同时检查离线CAS可用。

最终验收使用的真实回放归档位于 `D:/smm-registration-evidence-20261018/registration-persistence/`，与生产库、`E:/mod`和游戏目录隔离。无本体状态符合本次网页注册，不把网页归档成功说成本体下载或游戏部署成功。单项有本体直接归档、缺URL补充后注册、混合批次、重复、变更输入和失败补偿另有隔离回归。

正式 `python main.py` 路径只做实际UI预览：未知映射确认前按钮禁用，确认后可用，**没有点击注册提交**；记录 `production-preview.json`、截图 `production-preview.png`。不能用该生产预览代替前述隔离持久化证据。

## 4. Inspector与卡片控件

原技术文案来自 `services.deployment_inspection.inspect_deployment`，由Bridge的 `deployment_evidence` 原样传给 `drawInspector`。本轮只改正式渲染：`frontend/web/app.js:302` 的 `deploymentNotice` 对正常未部署、文件存在、配置存在不生成诊断段落；未知、缺文件、兼容待核验和启用配置异常保留简短用户提示。底层诊断DTO、日志、审计工具、部署准入和所有权检查都保留，未把冲突改成“未部署，一切正常”。没有新增诊断面板、空白占位或分隔线，类型、Workspace ID、描述和底部操作结构保持不变。

控件共用 `frontend/web/style.css:1604` 的外框规则；内部保留方框/check与金色空心/实心SVG星标。勾选键盘事件交给原生checkbox处理，避免Space冒泡后重复执行卡片选择。

| Qt computed style / DOM rect | 修改前勾选 / 收藏 | 修改后勾选 / 收藏 |
|---|---|---|
| 点击区 | 20×20 / 32×32 | 32×32 / 32×32 |
| 顶部距离 | 10 / 9 px | 9 / 9 px |
| 左 / 右边缘距离 | 10 / 9 px | 9 / 9 px |
| 圆角 | 4 / 8 px | 8 / 8 px |
| 默认背景 | 不同 | 同为rgba(19,28,36,0.92) |
| 默认边界 | 不同 | 同为1px rgba(255,255,255,0.14) |
| Hover过渡 | 不同 | 同为140ms；Focus共用accent轮廓 |
| Inspector打开时卡片 / 封面 | 214×218 / 212.4×120 | 相同 |

数据见 `controls-comparison.json`。正式1728×900逻辑窗口，DPR=1.25，物理2160×1125；未降低宫格密度。正式窗口验证星标Hover、星标与勾选Focus、Space选择及Inspector无星标；Qt鼠标事件送入真实WebEngine，未以强制CSS伪类替代。隔离Qt另验证单选、多选、嵌套SVG点击不打开详情/不改变勾选、选择不改收藏、虚拟化滚动后状态及实际收藏持久化。

截图均在 `D:/smm-registration-evidence-20261018/`：

- Inspector正常未部署前后：`before-1623730.png` → `after-1623730.png`；正常模式补充 `inspector-normal.png`。
- 真实历史异常前后：`before-916440.png` → `after-916440.png`，异常仍有简短提示。
- 文件缺失异常前后：`before-281990.png` → `after-281990.png`。
- 控件实际状态：`controls-star-focus.png`、`controls-check-focus.png`、`controls-selected.png`、`controls-hover.png`；来源确认：`production-preview.png`。

## 5. 偏好、测试和安全证据

偏好仍为原 `QSettings('SteamModManager','WorkshopLibrary') / ui/registration/{appid}`，仅四项UI选择。没有把偏好用作来源准入。正式新进程 `registration-restart.json` 验证Nexus离线方式恢复、Palworld另一游戏方式有效、临时文件路径为空；INI隔离测试确认不保存凭据、来源ID或文件路径。

最终完整相关集合 `delivery-regression.txt` 为 **129通过**；多选及补充链接的真实Qt交互扩展 `controls-interaction-regression.txt` 为1通过。此前 `release-regression.txt` 98通过、`final-identity-regression.txt` 61通过，集合重叠，不合计为独立测试总数。静态diff检查与Qt/真实Worker回放分别记录，没有用普通浏览器预览替代Qt运行。

本轮没有修改AGENTS列出的cover/lifecycle或canonical门禁入口。上一轮两个WH3旧布局门禁失败属于仍存在的项目问题，不在本轮注册/控件范围内，也未放松相关断言。

开始前以SQLite backup API建立一致性恢复点 `baseline/mod_manager.db`，同时保存初始Git状态及二进制diff。`production-safety.json` 核对所有表行内容指纹、Schema、4994个原归档路径存在性、283份部署Manifest哈希及原输入MHTML哈希；均与本轮基线一致。正式注册提交0、生产导入/同步/部署命令0；预览只使用临时缓存，不改业务归档。**没有对612GB归档的每个文件字节做全量哈希**，不作超出核验范围的保证。

## 6. 实际文件、提交与剩余限制

核心提交：`3e80181 fix(registration): restore manual Nexus admission and align library controls`。

实际修改：`frontend/acquisition.py`、`frontend/offline_registration.py`、`frontend/registration.py`、`frontend/web/app.js`、`frontend/web/offline_registration.js`、`frontend/web/registration.js`、`frontend/web/style.css`、`services/importers/offline_admission.py`、新增 `services/importers/nexus_registration_identity.py`；回归为 `tests/test_free_registration_contract.py`、`tests/test_web_registration.py`、`tests/test_web_acquisition.py`。本报告及能力矩阵单独记录交付。

开始前的DB、视觉变量/滚动优化、映射与测试改动均保留，未混入核心提交；期间出现的 `.gitignore` 工作区改动也未提交或覆盖。

剩余限制：最终视觉待用户评审；本轮没有实做任何生产注册；手工登记不证明远端条目存在，不补齐缺失的官方API游戏映射/凭据，也不增加联网下载能力。上一轮部署有效性及WH3布局问题保持原验收状态，本轮没有以移除Inspector文案冒充修复部署异常。

临时目录清理被自动审批拒绝，工具仅返回 `blocked by policy`，未给具体原因。因此证据目录下四个未采用的回放目录 `real-registration`、`confirmed-real-registration`、`final-real-registration`、`verified-real-registration` 保留，未尝试绕过审批删除。它们均为隔离测试数据，不是生产归档。
