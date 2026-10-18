# Inspector P1.1 收尾验收 — 2026-10-18

**CODE FIXED / FORMAL QT PASS / USER VISUAL REVIEW PENDING / NOT CLOSED**。
三项细节已实现并通过运行检查，最终关闭等待用户视觉评审。

## 修改范围

产品代码仅修改 `frontend/web/style.css` 的 4 条原有规则，另新增本报告。JavaScript 与任务开始时字节一致，没有重写 DOM、控件、描述、Footer 或业务。未调整任何卡片、导航、工具栏。

- `.inspector-user-tags`：图标与胶囊水平间距 **8 → 5px**，原多标签横排与换行机制不变。
- `.inspector-user-tags > svg`：上偏移 **3 → 3.25px**，按胶囊行盒调整图标垂直位置。颜色、图标和胶囊样式不变。
- `.inspector-type-property`：身份区之后的 margin **16 → 12px**，移除该行顶部的身份区分隔线；原内部 padding、类型入口样式与交互不变。
- `.inspector-scroll > .inspector-workspace`：上间距 **16 → 12px**，下间距仍为 16px。描述前的原有弱分隔保留。

[本轮完整 CSS diff](D:/smm-inspector-p11-20261018/p11-css.patch)。没有新增选择器、Token、高优先级覆盖或依赖。

## 实际 Qt 测量

[逐样本测量与前后断言](D:/smm-inspector-p11-20261018/measurements.json)。

- 图标至首个胶囊水平间距 **8 → 5px**。
- 图标中心减首个胶囊中心的垂直差 **−0.05 → +0.20px**；最终中心偏差为 0.20 逻辑像素，未出现肉眼可辨的错位。
- 身份组底部至类型行外框间距 **16 → 12px**；类型/标签之后至 Workspace ID 间距 **16 → 12px**。
- 身份组底部至描述标题区的距离，各样本均减少约 **8.8px**。其中原分隔线在 DPR 1.25 下实测为 0.8px，修改后为 0。
- 标准窗口 Inspector 仍为 **330px**，较小窗口仍为 **300px**；封面仍为 **92×72px**。身份区、来源行及封面矩形逐样本前后一致。
- 标准宫格仍为 **5 列、卡宽 214px、封面高 120px、行步长 230px**；小窗口仍为 **3 列、卡宽 202px、封面高 113px、行步长 223px**。
- 每个样本的 Footer 和各按钮矩形前后一致，滚动前后 Footer 位置一致。正文和 DOM 顺序一致，无横向溢出、未新增滚动容器；无标签时仍隐藏标签区。

## 同视口真实截图

生产样本来自正式 `main.main(['--read-only'])`，使用真实数据库及原资产；探针只切换对象、读取 DOM、滚动和截图。原实例锁阻止首次启动后，由用户关闭 SMM，再正常获取锁；未删除锁或终止用户进程。

标准逻辑窗口 **1728×900**，DPR **1.25**，物理截图 **2160×1125**：

- REFramework（有标签、无类型）：[修改前](D:/smm-inspector-p11-20261018/before-reframework.png) / [修改后](D:/smm-inspector-p11-20261018/after-reframework.png)。
- Sunreach（有类型、有标签）：[修改前](D:/smm-inspector-p11-20261018/before-type-tags.png) / [修改后](D:/smm-inspector-p11-20261018/after-type-tags.png)。
- 无标签对象：[修改前](D:/smm-inspector-p11-20261018/before-no-type-no-tags.png) / [修改后](D:/smm-inspector-p11-20261018/after-no-type-no-tags.png)。
- Console Commands 长标题：[修改前](D:/smm-inspector-p11-20261018/before-long-title.png) / [修改后](D:/smm-inspector-p11-20261018/after-long-title.png)。
- Mystras Spells（已部署、长描述）：[修改前](D:/smm-inspector-p11-20261018/before-deployed-long-description.png) / [修改后](D:/smm-inspector-p11-20261018/after-deployed-long-description.png)。

另覆盖 Layla 复杂 BBCode 和无封面对象。较小窗口 **1180×760**，物理 **1475×950**，同样覆盖全部 7 个对象：[小窗口长标题前](D:/smm-inspector-p11-20261018/before-small-long-title.png) / [后](D:/smm-inspector-p11-20261018/after-small-long-title.png)。其余截图位于同目录，前缀 `before-`、`after-`、`before-small-`、`after-small-`。

生产库没有多个用户标签的现成样本，复用上一轮已存在的隔离资源，通过正式 `--validation-root` 入口只浏览、不执行业务命令：[多标签前](D:/smm-inspector-p11-20261018/before-multi-multi-tags.png) / [后](D:/smm-inspector-p11-20261018/after-multi-multi-tags.png)。该截图与生产截图明确区分，不以隔离样本代替生产正式 Qt 验收。

## 回归与数据保护

`python -m pytest tests/test_inspector_four_fixes.py tests/test_web_library.py tests/test_web_type_tag_independence.py -q`：**32 passed / 14.65s**，见 [测试记录](D:/smm-inspector-p11-20261018/regression-tests.txt)。复用现有隔离测试，覆盖类型设置/取消、标签增删及长标签换行、类型标签独立、详情/编辑取消、长描述与 Footer。没有新增测试业务或修改原测试文件。

正式生产 Qt 两种窗口各 7 个样本及隔离多标签样本均正常退出、控制台错误 0，未使用静态 HTML 或普通浏览器代替。原始记录：[标准](D:/smm-inspector-p11-20261018/after-qt.json)、[较小窗口](D:/smm-inspector-p11-20261018/after-small-qt.json)、[隔离多标签](D:/smm-inspector-p11-20261018/after-multi-qt.json)。

[生产安全比较](D:/smm-inspector-p11-20261018/production-safety.json)：开始和回归结束后的只读 SQLite 快照，Schema 及全部表内容一致；4995 个归档根目录的存在性/修改时间、57 个文件校验项、REFramework 全目录文件哈希及类型目录哈希均一致。此项不是全归档逐字节扫描。生产业务命令 0，写入测试只在 pytest 临时资源根内进行。

工作区原有未提交改动保留，仅本轮 CSS 差异和报告进入独立 Git 提交。隔离资源与证据未清理。没有新增待修业务问题；仅用户最终视觉评审尚未关闭，不继续扩大 Inspector 调整范围。
