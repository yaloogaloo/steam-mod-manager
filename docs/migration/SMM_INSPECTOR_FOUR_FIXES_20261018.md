# Inspector 四项纠偏验收 — 2026-10-18

**CODE FIXED：四项完成。FORMAL UI PASS：现有生产样本的正式 Qt 只读验收通过；类型写入、清除、多标签与键盘取消通过隔离正式 Qt 验收。** 本轮没有实现卡片信息区重排，没有新增页面入口或业务字段。

## 修改文件与核心 diff

仅四个正式前端文件：

- `frontend/web/index.html`：删除工具栏 `manage-types` 按钮，原位置直接连接密度切换，无占位；未移到其他页面。
- `frontend/web/app.js`：仅修改 `drawInspector`。普通未部署 Badge 不渲染；类型独立为内联属性值；删除资产状态 dt/dd；用户标签逐项渲染胶囊并移到类型后，Workspace ID 成为描述前最后一项技术属性。
- `frontend/web/style.css`：只调整 Inspector 类型、用户标签和技术属性的样式；合并被替换的标签样式、移除不再使用的 Inspector 类型 Badge 样式；Workspace ID 只保留上方分隔，避免双线。
- `frontend/web/type_management.js`：移除被删除按钮的事件绑定；保留现有权威类型选择器和 `type-set` 调用。补齐展开状态、Escape 取消和保存后焦点恢复，不打开完整用户信息编辑表单。

回归文件：`tests/test_inspector_four_fixes.py`（实际 WebEngine/Bridge 隔离集成）；`tests/test_web_library.py`（已部署详情的 Badge 数量从4改为3，类型已移到独立行）。另有本报告。开始前已有的卡片滚动、视觉 Token、其他测试和数据库修改均未混入本轮提交。

完整核心补丁：[core-ui.patch](D:/smm-inspector-four-fixes-20261018/core-ui.patch)。其变化对应：

```text
工具栏：删除“类型目录”及专属事件绑定，无替代入口。
未设类型：Mod 类型  [+ 设置类型] → 原选择器。
已设类型：Mod 类型  [实际类型值] → 原选择器，无额外编辑按钮。
日常属性：已部署时的部署 Badge / 来源 / 大小 → 类型 → 实际用户标签 → Workspace ID → 描述。
删除：Inspector 的“资产状态”整行；普通“未部署”Badge；“无标签”占位不渲染。
```

已有部署异常提示与底部业务操作没有扩展。未改 ModTypeCatalog Service、`data/mod_types.json`、DTO、SQLite Schema、归档、部署、注册、类型或标签保存合同。

## 正式 Qt 只读 UI

通过正式 `main.main(['--read-only'])` 启动实际 Qt WebEngine，读取真实生产库和归档；没有静态 HTML 或浏览器 Demo。逻辑1728×900，DPR1.25，截图2160×1125。最终控制台异常0。

- **Dragon's Dogma 2 / REFramework，PK3770023576**：未设类型，真实用户标签“111”，未部署。Inspector 仅显示 Nexus Mods /636 B；加号类型胶囊、标签、Workspace ID 8、描述顺序正确，资产状态行删除。截图：[修改前](D:/smm-inspector-four-fixes-20261018/before-reframework.png)、[最终](D:/smm-inspector-four-fixes-20261018/final-reframework.png)。
- **有类型、有标签 / Sunreach 传送门远征站，PK3770021746**：类型“拓展”独立显示，用户标签“建筑”紧随其后，无重复分类。截图：[修改前](D:/smm-inspector-four-fixes-20261018/before-type-tags.png)、[最终](D:/smm-inspector-four-fixes-20261018/final-type-tags.png)。
- **无类型、无标签 / [leper skin] meow，PK5**：仅加号类型入口；不渲染空标签容器或“无标签”。截图：[最终](D:/smm-inspector-four-fixes-20261018/final-no-type-no-tags.png)。
- **长名称 / Console Commands，PK2791241084**：Inspector 显示完整多行名称，未遮挡属性和 Footer。截图：[最终](D:/smm-inspector-four-fixes-20261018/final-long-title.png)。
- **已部署、长描述 / 密斯特拉的法术，PK1402**：显示“已部署”Badge、来源、大小及类型；标签为空不占位。长描述可正常滚动，Footer 滚动前后矩形完全相同。截图：[顶部](D:/smm-inspector-four-fixes-20261018/final-deployed-long-description.png)、[描述底部](D:/smm-inspector-four-fixes-20261018/final-description-bottom.png)。

所有五个生产样本均验证：工具栏按钮不存在；无资产状态行；标签等于详情 DTO 的原用户标签集合；Workspace ID 紧邻描述上方；Footer 固定可达；卡片 width / coverHeight / cell / columns 前后一致。详情读数见 [state-comparison.json](D:/smm-inspector-four-fixes-20261018/state-comparison.json)。没有修改卡片渲染或卡片信息区样式。

正式只读环境还验证搜索返回真实匹配，部署状态与类型筛选正确，名称排序正确；[final-qt.json](D:/smm-inspector-four-fixes-20261018/final-qt.json) 保存读数。只读类型控件按原权限禁用，未借视觉验收写入生产 type_id。

## 类型选择与多标签：隔离真实 Qt

使用正式 `main.main(['--validation-root', ...])`，全部数据、游戏文件和类型目录位于 `D:/smm-inspector-four-fixes-20261018/isolation`，显式 test mutation context。调用实际 WebEngine → QWebChannel → 原 Bridge → 原类型 Service / DAL，没有替换业务实现。

- Enter 打开原类型选择器，焦点进入 select；未打开完整信息编辑表单。
- Escape 关闭选择器、保留 Inspector，焦点返回内联类型控件；SQL total_changes 没有增加，业务命令数0。
- 选择类型调用一次原 `type-set`；清除类型再调用一次原 `type-set`。实际数据库分别得到 type_id=3 / NULL，保存后控件焦点恢复。
- 三个用户标签 `Graphics / 内容 / 自建标签` 始终不变；其中“内容”与类型同名，也仍是用户主动建立的独立标签。没有将类型自动写入标签。
- 类型目录文件字节不变，既有类型 CRUD/重启与标签独立性测试通过；没有删除 Service 或目录数据。
- 隔离 Qt 退出正常，JS 控制台异常0。[acceptance.json](D:/smm-inspector-four-fixes-20261018/isolation/acceptance.json) 记录实际结果。

截图：[原选择器](D:/smm-inspector-four-fixes-20261018/isolation/multi-tags-type-picker.png)、[类型值与三个用户标签](D:/smm-inspector-four-fixes-20261018/isolation/multi-tags-assigned.png)。这些明确是隔离样本，未冒充生产资源。

## 测试与生产安全证据

- 静态：两个修改的 JavaScript 文件通过 Node 语法检查；Git diff 空白检查通过（CRLF 配置）。
- 实际 Qt/Bridge 与类型合同回归：四个测试文件共 **33 passed**，原始日志 [targeted-final.txt](D:/smm-inspector-four-fixes-20261018/targeted-final.txt)。保存后焦点的最后增量另由隔离正式入口完整验证；最终单项重跑 **1 passed**，见 [focus-final-test.txt](D:/smm-inspector-four-fixes-20261018/focus-final-test.txt)，与33项重叠不相加。
- 正式只读和隔离可写运行分开记录；没有运行生产部署、收藏、编辑、标签或类型保存。
- 本轮开始和结束通过 SQLite backup API 建立一致性快照，Schema 和所有表指纹相同，包括原 type_id 与用户标签。
- 资源防护比较通过：4995 个管理根的存在性/mtime、57 个受检文件项（含选中对象 sidecar/Manifest、完整 REFramework 目录文件、类型目录）无变化。该资源比较覆盖隔离可写验收阶段；没有声称对整个归档库做全字节哈希。正式生产读取由 native mode=ro 和只读运行时限制。
- 生产类型目录 SHA256：`1abfb26cb3c0ab5d1aaa628270e02e3199b1d43591add2bb90b8964a74c3bf81`。生产业务写入命令0。[production-safety.json](D:/smm-inspector-four-fixes-20261018/production-safety.json)。

## 未完成项目与边界

现有生产库最多只有1个用户标签/Mod，没有真实多标签对象；多标签展示及类型操作已在隔离正式 Qt 用3个标签完成验收，未创建生产测试标签来补样本。

本轮四项没有剩余代码缺口。最终视觉评审使用上述真实 Qt 截图。Mod 卡片信息区重新排版没有实施，仍等待独立设计评审；本轮没有新增优化建议或顺带实施其他功能。
