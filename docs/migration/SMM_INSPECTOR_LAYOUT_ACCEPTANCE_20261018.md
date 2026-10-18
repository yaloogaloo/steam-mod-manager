# Inspector 信息布局精修验收 — 2026-10-18

本轮代码和运行验收完成，最终视觉评审等待用户确认。仅修改 Inspector，未实施卡片信息区设计。

- **CODE FIXED**：正式前端结构和样式已修改。
- **ISOLATED TEST PASS**：真实 WebEngine / QWebChannel 类型、标签及详情操作通过。
- **FORMAL QT PASS**：正式入口、真实生产资产、只读模式，两种视口各 7 个样本通过运行和布局检查。
- **USER VISUAL REVIEW PENDING**：以下真实截图等待用户评审。
- **NOT CLOSED**：视觉任务最终关闭取决于用户评审，未自行宣称商业级视觉完成。

## 修改文件与核心变化

1. `frontend/web/app.js`：调整 `drawInspector` 身份组、类型值、标签和 Workspace ID；删除日常 Inspector 的部署诊断及描述来源展示。已清洗描述在独立容器中解析，避免未闭合标记把 Footer 吸入描述 DOM。
2. `frontend/web/style.css`：合并重复 Inspector 规则，保留当前宽度、背景和 Footer；集中设置封面、文字、标签、属性间距和复杂描述的换行规则。
3. `tests/test_inspector_four_fixes.py`：更新类型入口断言，增加未闭合描述、长标签、完整详情、编辑取消和标签入口回归；等待循环让出执行时间，避免后台 RPC 被紧密轮询拖慢。
4. `tests/test_web_library.py`：两处 Inspector 布局断言跟随本轮结构更新。
5. 本报告。

[本轮核心 diff](D:/smm-inspector-layout-20261018/inspector-layout.patch) 是任务开始时文件到最终文件的差异。开始前已有的虚拟宫格、全局视觉变量、测试及数据库等未提交改动不纳入本轮提交。

身份区由“封面及名称 + 下方来源/大小 Badge”改为“左封面 + 右侧完整名称、来源及大小”。未知大小直接省略；已部署只显示一次状态，普通未部署不显示状态。类型值透明显示、右侧小 chevron，继续调用原类型选择器。标签沿用原 `tags` 图标及真实用户标签，空标签区域隐藏。Workspace ID 水平排列，位于标签之后、描述之前。

描述沿用原格式化、安全清洗与空状态文案；仅改变 DOM 插入边界及 Inspector 排版。没有新增业务字段、按钮、内层滚动区或设计 Token。复用 `--text`、`--secondary`、`--muted`、`--edge`、`--surface`、`--raised`，间距主要使用 8 / 12 / 16px。类型及 Footer 事件绑定与任务开始时逐段比较一致，见测量证据。

## 正式 Qt 前后截图

全部来自正式 `main.main(['--read-only'])`，使用当前真实数据库及资产；探针仅设置窗口尺寸、切换资产、读取 DOM 和截图，不替换桥接数据。用户关闭原实例后正常获取单实例锁，未终止用户进程或删除锁。

标准逻辑窗口 **1728×900**，DPR **1.25**，物理截图 **2160×1125**：

- REFramework（DD2，未设置类型、标签 111）：[修改前](D:/smm-inspector-layout-20261018/before-reframework.png) / [最终](D:/smm-inspector-layout-20261018/final-reframework.png)。
- Sunreach（Palworld，类型“拓展”、用户标签“建筑”）：[修改前](D:/smm-inspector-layout-20261018/before-type-tags.png) / [最终](D:/smm-inspector-layout-20261018/final-type-tags.png)。
- 无类型、无标签对象（Darkest Dungeon）：[修改前](D:/smm-inspector-layout-20261018/before-no-type-no-tags.png) / [最终](D:/smm-inspector-layout-20261018/final-no-type-no-tags.png)。
- Console Commands（WH3，完整长标题）：[修改前](D:/smm-inspector-layout-20261018/before-long-title.png) / [最终](D:/smm-inspector-layout-20261018/final-long-title.png)。
- Mystras Spells（BG3，已部署、长描述）：[修改前](D:/smm-inspector-layout-20261018/before-deployed-long-description.png) / [最终](D:/smm-inspector-layout-20261018/final-deployed-long-description.png) / [滚动到底](D:/smm-inspector-layout-20261018/final-description-bottom.png)。
- Layla（Slay the Spire，复杂 BBCode）：[修改前](D:/smm-inspector-layout-20261018/before-layla.png) / [最终](D:/smm-inspector-layout-20261018/final-layla.png)。
- 枯星龙（Palworld，无封面、大小未知）：[修改前](D:/smm-inspector-layout-20261018/before-no-cover.png) / [最终](D:/smm-inspector-layout-20261018/final-no-cover.png)。

较小逻辑窗口 **1180×760**，DPR **1.25**，物理截图 **1475×950**。同样覆盖上述 7 个样本，文件位于证据目录，前缀为 `before-small-` 和 `final-small-`。重点：[长标题修改前](D:/smm-inspector-layout-20261018/before-small-long-title.png) / [长标题最终](D:/smm-inspector-layout-20261018/final-small-long-title.png)，[Layla 最终](D:/smm-inspector-layout-20261018/final-small-layla.png)。

正式生产样本没有多个用户标签的对象，未制造生产标签。多标签和超长标签采用正式入口的隔离资源模式验证：[类型选择器](D:/smm-inspector-layout-20261018/isolation-v2/multi-tags-type-picker.png)、[类型赋值后](D:/smm-inspector-layout-20261018/isolation-v2/multi-tags-assigned.png)、[四标签含超长标签](D:/smm-inspector-layout-20261018/isolation-v2/long-user-tag.png)。这些隔离截图不替代上面的生产正式 Qt 截图。

## DOM / CSS 测量

[逐样本前后测量](D:/smm-inspector-layout-20261018/layout-comparison.json)；[正式标准窗口原始记录](D:/smm-inspector-layout-20261018/final-qt.json)；[较小窗口原始记录](D:/smm-inspector-layout-20261018/final-small-qt.json)。

- 标准窗口 Inspector 前后均 **330px**；小窗口前后均 **300px**。
- Inspector 封面改为固定 **92×72px**。修改前图像容器实测约 76×61.2px，无封面容器 76×58px。该调整只影响 Inspector 小封面，保留原图裁切方式。
- 标准窗口宫格前后均 **5 列**，卡片宽 **214px**，封面高 **120px**，行步长 **230px**。
- 小窗口宫格前后均 **3 列**，卡片宽 **202px**，封面高 **113px**，行步长 **223px**。
- 14 个最终生产样本均满足 `scrollWidth <= clientWidth + 1`，Footer 均为 Inspector 直接子节点，滚动前后 Footer 矩形一致。完整标题不省略，长标签可换行。
- 普通对象标准窗口底部主按钮约 **208.8px**、编辑 **53.6px**、更多 **30px**；已部署对象保留原“重新部署 / 撤出 / 编辑 / 更多”语义及相应宽度分配。描述不参与 Footer 的宽度计算。
- 无封面样本大小为 `null`，Inspector 未显示未知大小占位。标签、Workspace ID、描述的 DOM 顺序符合要求。

## 验证结果及边界

静态检查：`node --check frontend/web/app.js` 通过。浏览器 Demo 未作为验收依据。

真实 WebEngine 测试：`python -m pytest tests/test_inspector_four_fixes.py tests/test_web_library.py tests/test_web_type_tag_independence.py -q`，**32 passed / 15.24s**，见 [测试日志](D:/smm-inspector-layout-20261018/targeted-tests-final.txt)。补齐截图绘制等待及未知大小断言后，Inspector 集成测试再次运行，**1 passed / 4.84s**，见 [最终 Inspector 测试](D:/smm-inspector-layout-20261018/inspector-final.txt)。首次增加操作步骤时触发 90 秒测试超时；更长时限运行通过，随后修正测试等待循环让出执行时间，最终使用默认时限通过。

正式隔离入口 `main.main(['--validation-root', ...])`：正常退出，见 [操作记录](D:/smm-inspector-layout-20261018/isolation-v2/acceptance.json)。覆盖键盘 Enter 打开类型选择器、Escape 取消与焦点返回；取消数据库变更 0；设置类型与清空类型各一次；More 原标签入口添加与移除超长标签各一次。命令记录仅 `type-set, type-set, tag-add, tag-remove`。类型操作不改变用户标签和类型目录，标签操作不改变类型。完整详情打开/返回、编辑打开/取消、切换 Mod、未闭合 BBCode/HTML、超长连续文本及描述滚动通过。没有点击生产部署、撤出或编辑保存。

正式生产 Qt：两种窗口各 7 个真实样本，均正常退出、控制台错误 0。搜索、已部署筛选、类型筛选、名称排序的只读冒烟检查通过。Footer 部署事件以代码绑定一致性和现有测试验证，本轮未执行生产部署验证，不把它记作部署 Service 验收。

未修改 Python 生命周期或部署成员契约门禁列出的文件，未触发对应门禁扩展范围。

## 生产数据保护

[生产保护结果](D:/smm-inspector-layout-20261018/production-safety.json)：任务开始及结束通过 SQLite 只读连接备份，Schema 与**全部表内容**比较一致；正式 Qt `writable=false`，生产业务命令 0。生产数据库原有未提交状态不纳入本轮提交。

资源保护覆盖 **4995** 个已管理归档根目录的存在性及修改时间，**57** 个小文件校验项，包含选定 sidecar / Manifest、REFramework 完整目录文件哈希和 `mod_types.json` 哈希，前后一致。该证据不是全归档逐字节扫描。类型目录 SHA-256：`1abfb26cb3c0ab5d1aaa628270e02e3199b1d43591add2bb90b8964a74c3bf81`。

隔离资源与证据保留于 `D:\smm-inspector-layout-20261018`，未绕过审批清理隔离目录。证据中的 `isolation` 是首轮截图绘制未等待充分的版本；最终隔离截图采用 `isolation-v2`。

## 未关闭事项

用户对真实截图的视觉评审待完成。附件同时提及卡片“类型未知”文案，但本轮反复明确禁止修改卡片信息区，因此该文案保留，归入独立卡片设计评审，不在本轮实施。历史部署与游戏加载问题也没有借此次 UI 工作改动。
