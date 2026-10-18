# Inspector P1.2 描述阅读空间验收 — 2026-10-19

**实现、正式 Qt 与回归：PASS。用户视觉评审：NOT CLOSED。**

REFramework 描述标题文字从 **Y=524.4px → 464.4px**，上移 **60px**。Footer 上方的描述可用高度从 **264.4px → 324.4px**，增加约 **22.7%**。坐标均为相同 1728×900 逻辑窗口内的 Qt WebEngine CSS 坐标。

## 实际占高根因

正式 Qt 的 computed style 与 DOM 测量确认，类型、标签、Workspace ID、描述标题之间的生效间距逐段累加：

- 身份组结束后有 12px margin；类型行还有上下合计 16px padding，使 27.6px 的类型控件占用 43.6px 行高。
- 类型行后再留 8px 标签间距，标签后再留 12px Workspace ID 上间距。
- Workspace ID 下方有 16px margin，描述标题另有 16px 顶部 padding 和 0.8px 实际边线，导致 ID 到标题文字间距达到 32.8px。

没有固定空占位或空标签元素。身份组 72px 高度由保留的封面决定；长标题可以使其自然增高。`flex:1` 只作用于主滚动区，类型行的 `justify-content` 沿水平方向排布，没有纵向空间分配问题。全局 `.facts` 被现有 Inspector 局部规则覆盖。根因是当前生效的多段 margin / padding 累计，而非错误推测的固定高度。

## 修改范围及原因

产品仅修改 `frontend/web/style.css` 的 4 条原有规则，没有修改 JavaScript / DOM / 字号 / 封面 / Footer：

- `.inspector-type-property`：`margin-top:12px → 4px`，`padding:12px 0 4px → 0`，释放 **24px**；类型按钮本身仍高 27.6px。
- `.inspector-user-tags`：`margin-top:8px → 4px`，释放 **4px**；横排、换行、图标和胶囊保持原样。
- `.inspector-scroll > .inspector-workspace`：上下 margin 从 `12px / 16px → 4px / 4px`，释放 **20px**。
- `.inspector-scroll > .section-line`：`padding-top:16px → 4px`，释放 **12px**，保留原弱分隔及标题下方间距。

总计释放 **60px**；无标签对象没有标签行及该行间距，因此实际释放 **56px**。全部采用正常文档流，没有 absolute、负 margin、位移或固定描述高度。

另在 `tests/test_inspector_four_fixes.py` 为固定的短标题、三标签隔离样本增加阅读空间回归断言：Header 底部到描述正文顶部不得超过 210px。本轮前为 261.7px，修复后为 201.7px，能够检出此次间距回退。第三个文件为本报告。[完整 diff](D:/smm-inspector-p12-20261019/p12.patch)。

## REFramework 逐段坐标与高度

以下“前 → 后”均来自本轮正式 Qt，不复用上一轮截图：

- Inspector Header 底部：**288.3 → 288.3**。
- 身份组顶部 / 底部：**304.3 / 376.3 → 304.3 / 376.3**，高 **72 → 72**。
- 身份组到类型行间距：**12 → 4**。
- 类型行顶部 / 底部：**388.3 / 431.9 → 380.3 / 407.9**，高 **43.6 → 27.6**。
- 类型行到标签间距：**8 → 4**。
- 标签顶部 / 底部：**439.9 / 462.0 → 411.9 / 434.0**，高 **22.1 → 22.1**。
- 标签到 Workspace ID 间距：**12 → 4**。
- Workspace ID 顶部 / 底部：**474.0 / 491.6 → 438.0 / 455.6**，高 **17.6 → 17.6**。
- Workspace ID 到描述标题区边线间距：**16 → 4**；到标题文字间距：**32.8 → 8.8**。
- 描述标题区高：**34.4 → 22.4**；标题文字 Y：**524.4 → 464.4**，文字高仍为 **17.6**。
- 描述正文顶部：**550.0 → 490.0**；Header 底部至正文顶部总占高：**261.7 → 201.7**。
- Footer 顶部：**814.4 → 814.4**；Inspector 总高仍为 **635.3**。

完整逐样本坐标、区域高度及 `margin / padding / gap / min-height / line-height / justify-content / flex` 记录：[对比与断言](D:/smm-inspector-p12-20261019/measurements.json)、[修改前原始记录](D:/smm-inspector-p12-20261019/before-qt.json)、[修改后原始记录](D:/smm-inspector-p12-20261019/after-qt.json)。

## 正式 Qt 截图与覆盖

通过正式 `main.main(['--read-only'])` 加载真实生产资产，未用浏览器或静态 HTML 代替。标准逻辑窗口 **1728×900**，DPR **1.25**，物理截图 **2160×1125**：

- REFramework：[修改前](D:/smm-inspector-p12-20261019/before-reframework.png) / [修改后](D:/smm-inspector-p12-20261019/after-reframework.png)。
- Sunreach，有类型、有标签：[修改前](D:/smm-inspector-p12-20261019/before-type-tags.png) / [修改后](D:/smm-inspector-p12-20261019/after-type-tags.png)。
- 无类型、无标签：[修改前](D:/smm-inspector-p12-20261019/before-no-type-no-tags.png) / [修改后](D:/smm-inspector-p12-20261019/after-no-type-no-tags.png)。
- Console Commands 长标题：[修改前](D:/smm-inspector-p12-20261019/before-long-title.png) / [修改后](D:/smm-inspector-p12-20261019/after-long-title.png)。
- Mystras Spells，已部署、长描述：[修改前](D:/smm-inspector-p12-20261019/before-deployed-long-description.png) / [修改后](D:/smm-inspector-p12-20261019/after-deployed-long-description.png) / [滚动到底](D:/smm-inspector-p12-20261019/after-description-bottom.png)。

同时覆盖真实 Layla 复杂 BBCode 及无封面对象。较小窗口 **1180×760**，物理 **1475×950**，同样覆盖 7 个对象；REFramework 标题 **470.1 → 410.1px**，仍提前 60px：[小窗口修改前](D:/smm-inspector-p12-20261019/before-small-reframework.png) / [修改后](D:/smm-inspector-p12-20261019/after-small-reframework.png)，[长标题修改后](D:/smm-inspector-p12-20261019/after-small-long-title.png)。其余截图以 `before-small-` / `after-small-` 命名保存在证据目录。

生产库没有多个用户标签的现成对象，复用已有隔离资源，以正式 `--validation-root` 入口浏览，未添加生产标签：[多标签前](D:/smm-inspector-p12-20261019/before-multi-multi-tags.png) / [后](D:/smm-inspector-p12-20261019/after-multi-multi-tags.png)。其描述标题同样提前 60px。长标签自然换行另由隔离回归覆盖。

## 回归与安全结果

- 15 个最终 Qt 窗口样本（14 个生产只读样本、1 个隔离多标签样本）均无区域重叠、横向溢出及新增控制台错误。无标签行实测高度为 0，无空占位。长标题保持自然增高，没有锁定描述位置。
- Inspector 宽仍为标准 **330px** / 小窗口 **300px**，封面仍为 **92×72px**。标准宫格仍为 **5 列 / 卡宽 214px / 封面高 120px**，小窗口仍为 **3 列 / 202px / 113px**。Footer 及每个按钮矩形前后一致，滚动前后 Footer 不动。
- 身份组位置及高度不变；标准窗口 Sunreach 原有纵向滚动条因内容可容纳而消失，文字栏自然多出 6.4px 宽度，未扩大 Inspector。没有新增嵌套滚动区。
- `python -m pytest tests/test_inspector_four_fixes.py tests/test_web_library.py tests/test_web_type_tag_independence.py -q`：**32 passed / 14.94s**，见 [日志](D:/smm-inspector-p12-20261019/regression-tests.txt)。类型、标签、详情/编辑取消、长标签、复杂描述与 Footer 现有回归通过；未触及业务门禁列出的产品文件。
- 正式 Qt 探针记录的业务命令列表全部为空；生产窗口 `writable=false`。回归写入只发生在 pytest 隔离根目录。
- [生产保护结果](D:/smm-inspector-p12-20261019/production-safety.json)：SQLite Schema 和全部表内容前后一致；4995 个归档根目录存在性/修改时间、57 个文件校验项、REFramework 全目录文件哈希及类型目录哈希一致。该检查不是全归档逐字节扫描。

工作区原有改动保留，本轮仅提交上述 CSS、阅读空间回归断言及报告。生产 Mod、类型、标签、部署和 Manifest 未写入。证据及原隔离目录保留。功能和空间目标已通过验证，最终视觉评审等待用户确认。
