# 封面呈现历史归因与防回归审计 — 2026-10-19

本次只修复卡片封面图片超过容器的布局缺陷。方案 C、卡片信息结构、标题、密度、Inspector、封面原图及业务接口不变。取证目录：`D:/smm-cover-audit-20261019`。

## 根因归因表

| 问题 | 证据 | 结论 |
| --- | --- | --- |
| 图片异常的直接技术原因 | `before-dom.json`：容器 212.4×120，IMG 212.4×213.2，内层 Grid 行 213.2px；图片 `min-height:auto`，外层 `overflow:hidden` | Grid 自动最小尺寸撑大 IMG，随后祖先容器裁去下部。不是正常居中 cover 裁切，也不是原图文字本来靠底 |
| 最早异常版本 | 历史 Web HTML/CSS/JS 在现有 Qt 和同一输入中重放：`df1bec3` 已出现 IMG 210.8px / 容器 120px；旧原生父版本绘图方法居中裁切正常 | 已证明最早 Web 实现可复现；缺少历史正常整窗实证，生产首次异常版本 **FIRST BAD COMMIT NOT PROVEN** |
| 引入变更的提交 | `git log -S 'display:grid;place-items:center' -- frontend/web/app.js` 指向 `df1bec3`（2026-10-15，重构UI）；百分比图片高度与 Grid 自动最小尺寸在该提交结合 | 代码来源为首次 Web 迁移；`3aa546d`、`b6a5d71` 重放均已异常，方案 C 不是首次引入证据 |
| 为什么现有测试没有发现 | `test_card_scheme_c.py` 的 12 条记录没有封面输入，只验证卡片行数/字段/高度；Web 库测试验证权限、加载错误和交互；原生 CoverLoader 测试用小纯色图 | 有效覆盖了各自合同，但没有断言实际 Web 图片可见源区域和绘制内容。此前把容器尺寸及功能通过扩大为封面视觉通过，是验收口径错误 |
| 当前验收规范缺失什么 | 既有正式截图可包含缺陷；缺少经用户批准的正确图片基线、非标准比例真实输入和内容检查；原安全门关注封面所有权而非呈现 | 缺少图像内容合同与独立人工签收，不能由“封面仍高 120px”推出“封面正确” |
| 如何避免同类问题再次出现 | 固定 9 份真实封面响应及哈希，加已有无图对象；真实 Qt 像素/源区域/Inspector 策略/缓存返回检查；原缺陷负对照；两视口两 DPI；显式用户基线流程 | 自动保护已实现。未获用户批准的截图只能是候选，正式探针此时退出 2，不能冒充视觉验收完成 |

## 原图、资源与实际呈现

目标为 Stellaris / AppID `281990`，`mod_id=3770023516`，UUID `9446648f-d183-45a3-ad89-574235cac8aa`，Workspace `3818467946`，名称“舰船预设蓝图”。

- 原始资源：`D:/project/steam-mod-manager/data/mod_backup/9446648f-d183-45a3-ad89-574235cac8aa/cover.png`，PNG **512×512**，SHA256 `da0993709412628b39c785b07c58560c2b33e300d4871b2cb7b62583b1bdef85`。
- 原始 DAL `cover_path` 为空；现有只读投影/解析回退到上述备份封面。不是读取错误的另一张图。中文主体约在原图 y=211–270，英文约在 y=281–301，均处于中央。
- `LibraryAdapter.cover_file` → `frontend.readonly_metadata.resolve_cover` → Bridge 读取 QImage，按 `640×400 / KeepAspectRatio` 缩放并以 JPEG 85 编码。目标变为 **400×400**，没有裁切；SHA256 `d7f3b9fb7376b91879f869f98d78dcf661b18ca23e17be05aa6c3d7d954e0d3c`。
- 卡片及 Inspector 使用同一个 `data:image/jpeg;base64,...` 响应，解码字节哈希一致。完整 URI 很长，外部证据保存了解码后的 `before-card-preview.jpg` 与 `before-inspector-preview.jpg`；没有把会话 token 当成持久资源身份。
- Web 内存 `S.covers` 以 token 为键，300 项 LRU、4 个加载任务；没有发现磁盘缩略图缓存或错误图片转换。卡片节点由 renderGrid 创建，加载结果按当前 token 更新；实际滚动离开再返回的暖缓存测试验证资源和画面一致。

实际正式入口基线为逻辑 **1728×900**，DPR **1.25**，原生截图 **2160×1125**。图片样式为 `object-fit:cover; object-position:50% 50%; transform:none`，没有 background 图片或图片 aspect-ratio 干预。内层 Grid 高 120，但自动行最小尺寸约 213.2，IMG 随之超出外层裁剪框。

修复前目标图约只显示原图顶部至 y=289 的区域（底部英文被截断）；正确受约束的居中 cover 显示约 y=112–400。Inspector 自身有明确 92×72 尺寸，显示约 y=56–456，所以它原本就显得更完整。

分类结论：A“原图内容靠下”不成立；B“cover 策略对边缘文字可能有固有限制”成立，但不能解释当前底部额外裁切；C“Web 布局实现缺陷”成立；D“解析或缩略图错误”、E“虚拟化污染”在本次证据内均未发现。

## 历史边界与重放方法

没有 checkout/reset 生产工作区。历史完整 Web HTML/CSS/JS 从 Git 读取到外部目录，在现有 Qt、只读 Bridge、同一冻结图片输入下重放。保存的 `history-*-dom.json` 包含源文件哈希与方法限制。

| 对象 | 测量 | 能证明什么 |
| --- | --- | --- |
| `84a56b9`（`df1bec3^`）原生 `_set_cover` | 原方法 AST 原样调用，180×112；原图可见 y≈96.7–415.3，标题完整 | 旧绘图方法有受约束的居中裁切；不是完整历史应用重现 |
| `df1bec3`，2026-10-15 | 容器 120，IMG 210.8 | 首次 Web 源码已具备并可重现此缺陷 |
| `3aa546d`，方案 C 之前 | 容器 120，IMG 213.2 | 问题早于方案 C |
| `b6a5d71`，方案 C | 容器 120，IMG 213.2 | 方案 C 未解决已有问题，不能据此归责其首次引入 |
| 本轮修复前 | 容器 120，IMG 213.2 | 当前真实生产只读 UI 直接复现 |

原生中心裁切政策可追至 `894d068`（2026-08-07）；这项政策和后来 Grid 造成的上对齐额外裁切不是同一个问题。`object-fit` 的近期 blame 会落到格式/边框修改，不能用最近一条 blame 代替行为追溯。

用户提到的 `image(20261018-163338).png`、`image(20261018-173731).png` 未在可访问的附件、临时目录和项目中找到原文件，不能提供哈希或声称重新检查过它们。用户说明与重放结论一致，但没有被冒充独立的历史截图证据。

因此 **代码引入来源已定位；最后一个正常完整历史 UI 和生产首次异常提交仍未证明**。不将方法重放包装成这两项证明。

## 影响样本与漏检分类

只读有界采样记录 111 份现有封面（`real-cover-inventory.json`），按比例、文字位置、平台和游戏选出 9 个实际资源。它不是全库受影响数量统计。实际正式 Qt 修复前 9 例中 **6 例** IMG 高度比容器超过 1px：目标、Real Space、UI Overhaul（各 213.2）、Factionwide（285.9）、Nagash（278.44）、Additional powerplants（123.59）；Nexus REFramework、GitHub SMOD、Other Anno 样本未超出 1px 容差。9 例卡片/Inspector 资源哈希均一致。

| 类别 | 具体漏检点 | 本轮保护 |
| --- | --- | --- |
| 开发实现 | 百分比高度图片成为 Grid 项后，未解除固有最小尺寸；只检查父容器 | 检查 IMG 与容器四项边界、可见源范围；恢复原缺陷的负对照必须失败 |
| UI 设计规则 | “cover”没有区分正常居中裁切与布局溢出，也没有批准边缘文字取舍 | 固定声明中心 cover、目标中央文字 ROI；顶部/底部文字及纵图列入人工评审 |
| 测试覆盖 | 方案 C 样本没有真实图，纯色原生加载测试无法暴露文字偏移 | 真实方/横/纵/文字封面和无图；输入哈希、原生截图和数学参考图差异 |
| 验收流程 | 截图被采集但缺少逐图参考与签收；120px 高度/交互 PASS 被扩张为视觉 PASS | 自动检查、正式 Qt 技术结论、用户视觉签收分开；缺少批准基线返回 pending |
| 历史兼容 | Web 迁移丢失旧原生图像的固定绘制边界，未有跨实现呈现合同 | 明确图片实际可见区域合同及历史重放限制；AGENTS.md 新增封面呈现门禁 |

`.cover img` 只有一个规则入口；Inspector 存在基础与后续细化样式，但没有证据表明其覆盖链导致当前卡片问题。本轮没有顺带整理这些样式。

## 最小修复及比较

只在 `frontend/web/style.css` 现有 `.cover img` 中增加：

```css
min-height: 0; /* Do not let the Grid item's intrinsic minimum exceed the cover. */
```

正式 Qt 内存比较了三种策略并保留 `trial-*-formal-qt.png`：解除 IMG 最小高度、显式约束 Grid 行、约束后全局 contain。前两者都恢复图片 120px；contain 在方图/纵图两侧留下明显空白。因此选择一行最小高度约束，保留中心 cover，不改 Grid DOM、不增加规则覆盖、不做逐 Mod 特例。

这恢复中心 cover 的预期呈现，**不保证任何比例图片的所有边缘文字都完整**。顶部/底部文字和纵图仍可能按中心 cover 正常裁切；是否需要进一步显示策略，必须经本轮样本视觉评审，不能由自动技术通过推导。

## 小规模回归机制

- `tests/fixtures/cover_visual/manifest.json` 固定 9 份真实 Bridge JPEG 响应，总计 369,235 bytes，含原图路径/尺寸/哈希、预览哈希及关注点；另用真实无封面对象 UUID `ad10403f-6418-438a-a40d-85d07771fe77`。覆盖 Steam、Nexus、mod.io、GitHub、Other，没有创建生产样本。
- `tests/test_web_cover_visual.py` 在临时只读数据库中用 30 行隔离记录触发虚拟化；保留原 Bridge 身份/读取检查，只替换隔离图片输入。两视口逐图验证、暖缓存返回，并把 IMG 最小高度恢复 auto 作负对照，要求可见文字与像素断言均拒绝它。
- `tools/cover_visual_probe.py` 通过 **`main.main(['--read-only'])`** 启动正式装配，读取现有生产游戏/资产，不用静态 HTML。逐图保存整窗、实际封面、独立中心 cover 数学参考、差异热图、DOM 和哈希。
- 先等待两次动画帧，再检查封面区域连续稳定的原生帧；仅等待 img.complete 不足以保证 Qt 合成已提交。Qt 测试与探针串行执行，避免互相抢前台。
- 像素比较只遮罩现有星标/勾选和边缘；保留原始 MAE、超过 24/255 的像素比例。Canvas 与 CSS 的缩放/子像素路径不同，自动门限为 **8×8 物理像素块的平均颜色误差 ≤6/255**，另外独立验证源区域和目标文字 ROI，避免靠容差放过位移。负对照检测原缺陷。
- Inspector 同资源、自然尺寸、center-cover、无 transform 及 92×72 几何亦受检查。无图占位单列验证。
- `approved_references` 当前为空，**没有伪造用户认可截图**。批准记录必须含文件哈希及 `approval_evidence`，存在时自动计算差异；没有自动更新/强制通过开关。退出 0=自动及批准参考通过，1=失败，2=自动通过但人工待评审。

本次探针开发中有一轮并行截图混入旧合成帧，另一轮全窗稳定检查被非封面区域变化干扰。相关 `final-regression-125`、`formal-final-125-standard`、`stable-gate-125` 记录保留但不作为通过证据；随后收窄到封面帧稳定检查并串行重跑。这是取证程序的缺陷，不能隐藏为产品随机失败，也不能忽略失败截图继续声称通过。

## 最终验收记录

自动测试：`verified-tests-125.txt` 为 **29 passed / 37.79s**（封面视觉、方案 C、Web library）；`verified-tests-100.txt` 为 **2 passed / 21.38s**（标准 DPI 封面视觉）。两者使用真实 Qt WebEngine，临时数据库与冻结实际图片；它们和下面读取生产资产的正式入口取证独立记录。

最终正式入口串行取证全部自动通过，40 个状态、4 次暖缓存返回、JavaScript console 均为空。以下每行对应外部同名目录的 `results.json`：

| 目录 | 逻辑视口 | 实际 DPR / 原生截图 | 每轮样本 | 最大块颜色误差 /255 | 自动结论 |
| --- | --- | --- | --- | --- | --- |
| `formal-stable-125-standard` | 1728×900 | 1.25 / 2160×1125 | 9 图 + 1 无图 | 3.133 | PASS |
| `formal-stable-125-small` | 1180×760 | 1.25 / 1475×950 | 9 图 + 1 无图 | 5.066 | PASS |
| `formal-stable-100-standard` | 1728×900 | 1.0 / 1728×900 | 9 图 + 1 无图 | 2.541 | PASS |
| `formal-stable-100-small` | 1180×760 | 1.0 / 1180×760 | 9 图 + 1 无图 | 2.009 | PASS |

汇总：`D:/smm-cover-audit-20261019/final-matrix.json`。正式探针四次 `result_exit=2` 均表示技术通过、人工待评审，而不是通过的用户基线。没有将退出 2 当作最终关闭。

同视口 1728×900 / DPR1.25 的修改前后：

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| 卡片 / 列数 / 行步长 | 214×181 / 5 / 193 | 214×181 / 5 / 193 |
| 封面容器 | 212.4×120 | 212.4×120 |
| IMG / Grid 行高度 | 213.2 / 213.2 | 120 / 120 |
| 目标源图可见纵向范围 | 约 0–289px | 112.33–399.67px |
| Inspector IMG | 92×72 | 92×72 |

小视口仍按现有算法为 3 列、202×174、封面 113px；没有改动宽度或密度算法。未对小视口编造修复前正式截图，直接前后比较依据仅为上述标准视口，其他组合是修复后回归覆盖。

主要真实 Qt 截图（均为完整原生截图，下面路径以取证根目录为前缀）：

- 修改前目标：`before-formal-qt.png`；修改后同视口：`formal-stable-125-standard/square-caption-full.png`。
- 纵图文字前：`before-portrait-text-formal-qt.png`；后：`formal-stable-125-standard/portrait-text-full.png`。
- 方图底部文字前：`before-square-bottom-text-formal-qt.png`；后：`formal-stable-125-standard/square-bottom-text-full.png`。
- 横图及不同来源：最终四个目录内 `wide-nexus-full.png`、`wide-modio-full.png`、`wide-github-full.png`、`wide-other-full.png`。
- 顶底文字、纵图主体、无封面：各目录 `square-top-bottom-full.png`、`portrait-subject-full.png`、`no-cover-full.png`。
- 每图 `*-actual.png`、`*-expected.png`、`*-difference.png` 及 results 中的截图 SHA256 保留自动比较依据。未把数学参考图命名为用户批准基线。

生产安全检查：`production-safety.json` 与起点一致的 **13 张表结构/内容**，`PRAGMA integrity_check=ok`，**111 份原封面 SHA256 未变**。没有生产写入操作，没有修改归档、Manifest、描述符、Playset；没有扫描或重新生成全库封面。证据输出都在外部显式目录，测试修改仅在临时数据库。正常启停应用的日志/实例锁不等同于业务写入，没有删除用户实例锁或终止用户进程。

本轮工作期间工作区已有内容被外部提交到 `5ac77fb`，其中包含一行产品修复及初版测试/门禁，也包含先前遗留改动。本轮不重写该历史，不把其中其他文件归入封面修复。最后的探针稳定性、Inspector 呈现检查和本审计记录单独提交；不夹带生产数据或其他 UI 改动。

状态：

- **ROOT CAUSE CONFIRMED**：Grid 固有最小高度造成的额外裁切。
- **FIRST BAD COMMIT IDENTIFIED：未满足**。代码来源 `df1bec3` 已定位；生产整窗历史仍为 **FIRST BAD COMMIT NOT PROVEN**。
- **REGRESSION PREVENTION IMPLEMENTED**：输入/源区域/实际像素/负对照/DPI/缓存/门禁已执行；用户批准基线仍待签收。
- **FORMAL QT PASS（技术范围）**：40 个正式只读样本状态通过，不代表所有封面主体或边缘文字完整。
- **USER VISUAL REVIEW PENDING**：候选图未自动升格。
- **NOT CLOSED**：等待视觉评审；历史证据限制继续保留，不编造责任归属。
