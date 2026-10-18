# Card Scheme C — 2026-10-19

状态：CODE FIXED / FORMAL QT PASS / USER VISUAL REVIEW PENDING。

只修改资产库卡片的信息区和与之对应的虚拟列表行高。标题保持单行省略；第二行只含来源、已有权威类型、已部署标记。无类型、普通未部署不生成占位。封面、星标、勾选及点击语义保持原样。

`frontend/web/app.js`：`reflow` 的信息区占高从 98 改为 61；`renderGrid` 使用一行 `.card-meta`。`frontend/web/style.css`：替换旧 `.card-line` 并删除其重复覆盖，类型值可省略，来源与部署标记不收缩。未新增视觉 Token。

正式入口 `main.main(['--read-only'])`，实际 Qt WebEngine，逻辑视口 1728×900、DPR 1.25、截图 2160×1125。标准样本卡片宽 214、封面高 120，前后不变；卡片高 218→181，行距 230→193；信息区统一 59，第二行 20。1180×760 小视口卡片宽 202、封面高 113、卡片高 174，未加第三行。

证据根目录：`D:/smm-card-size-stellaris-20261019`。

- 修改前：`before-reframework.png`、`before-deployed-long-description.png`、`before-qt.json`。
- 修改后：`final-reframework.png`、`final-deployed-long-description.png`、`final-no-cover.png`、`final-qt.json`。
- 小视口：`final-small-*.png`、`final-small-qt.json`。
- 坐标与独立大小校验：`ui-size-validation.json`。

正式只读验收覆盖 13 个真实资产、五种来源、有/无类型、已/未部署、长标题、无封面。搜索、筛选、排序及卡片第二行边界检查通过，控制台为空。`tests/test_card_scheme_c.py` 在真实 WebEngine 中覆盖 12 种类型/部署/来源组合、统一高度和单行边界；与本轮相关测试合计 95 passed / 1 skipped（之后增加的大小回滚测试另有 1 passed）。只读阶段数据库全部表和资源守卫一致，见 `production-safety-readonly-phase.json`。
