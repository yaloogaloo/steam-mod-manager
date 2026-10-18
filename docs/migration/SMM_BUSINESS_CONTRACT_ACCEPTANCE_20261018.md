# SMM P0/P1 业务契约修复与数据审计 — 2026-10-18

最终状态：**NOT CLOSED**。代码修复已分领域提交；Inspector 正式只读 Qt 对照通过，类型目录隔离可写 Qt CRUD 通过。未在生产库创建测试类型/标签、补齐真实归档或修改部署状态，因此不能将隔离写测试标记为正式写入验收通过。

## 1. 根因与分项状态

| 问题 | 根因与证据 | 修改 | 状态 |
|---|---|---|---|
| Inspector 尺寸 | Layla 来源描述开头两个未闭合 `[b]`，清洗器输出未闭合 HTML；浏览器把 Footer 按钮收进 b 元素，失去直接 flex 子项身份。魂魄妖梦没有该结构。 | description_view 闭合允许的标签片段，忽略不匹配结束标签；不以 CSS 固定宽度掩盖问题。 | ROOT CAUSE CONFIRMED / CODE FIXED / ISOLATED TEST PASS / FORMAL UI PASS |
| 标签覆盖类型 | 生产现存 4 条用户标签关联仍保留 type_id，未复现所述类型丢失。确认两个横向缺陷：部分用户字段更新隐式清空缺省字段；sidecar 把首个用户标签写成 category。不能断言它们就是本次 type_id 丢失的直接原因。 | 动态更新实际传入字段；用户标签不导出为类型。类型与标签双向矩阵、重读及独立连接验证。 | CODE FIXED / ISOLATED TEST PASS / PERSISTENCE PASS；原报告直接根因未证实，NOT CLOSED |
| 标签跨游戏 | list_all_category_tags 原来全局聚合，Bridge 直接返回全库候选。 | 候选经 mods.app_id 范围查询，含 app_id=0；同名标签在不同游戏独立。复用现有关联表，无 Schema 迁移。 | ROOT CAUSE CONFIRMED / CODE FIXED / ISOLATED TEST PASS / PERSISTENCE PASS；正式写入 UI 未验收，NOT CLOSED |
| 类型目录失效 | 正式前端缺少独立管理入口，增删绑在资产上下文；类型筛选只看已有绑定。另行解析 JSON，绕过原目录服务。 | 原 ModTypeCatalog 统一读写；游戏范围入口，增删改名；重命名保留 ID；按权威 ID 筛选。 | ROOT CAUSE CONFIRMED / CODE FIXED / ISOLATED TEST PASS / PERSISTENCE PASS；正式只读入口和隔离 Qt CRUD 通过，正式写入未验收，NOT CLOSED |
| 多压缩包补齐 | content-add 原来只接收单来源输入，Steam ZIP 与非 Steam 原包语义需要保留。 | 多选 ZIP/7z，整次预检与临时暂存；复用 extract_archive、原 source_files 和 local_file_index；异常撤回本次文件与索引。 | ROOT CAUSE CONFIRMED / CODE FIXED / ISOLATED TEST PASS / PERSISTENCE PASS；正式真实归档补齐未执行，NOT CLOSED |
| data 冗余 | 不能通过文件名或缺少字面引用证明冗余；CAS、备份和恢复目录有动态定位。 | 冻结基线 56,187 条目，逐路径 CSV 分类，未删除。C 类 0 bytes。 | 审计交付 PASS；疑似遗留关联未逐一解除，NOT CLOSED |

## 2. 业务合同恢复

- 类型：原权威为 `data/mod_types.json` 按游戏维护目录，Mod 保存 type_id。现在独立管理入口读写原服务，不依赖选中 Mod；重命名不换 ID，删除按原服务解除当前游戏绑定。类型筛选展示完整目录并按 ID 匹配。原文件存在时，显式变更前保存 `data/type_catalog_recovery/<UUID>/mod_types.json` 恢复副本。
- 用户标签：仍为 SQLite 的 category tag 关联，仅为兼容内部旧命名；不等同 Mod 类型。候选按权威 app_id 归集，不改 Mod 类型、收藏、来源与部署字段。
- 部分字段：过去 display_name、description、notes、favorite 缺省时可能被整包清空；现在只有实际存在的键参与 UPDATE。显式空值与未传字段分开处理。
- 来源刷新与侧车：用户标签不再成为可携带的 category 字段。没有重写生产侧车，没有把旧标签迁移成类型，也未触发真实来源刷新。
- 内容：Steam 保留归档根目录结构解包；Nexus/GitHub/Other/mod.io 按原 source-unit 合同保留原包。多个包仍关联同一 Frozen UUID/PK，不注册成多个 Mod。
- 补齐只接纳未部署、没有现有本体的对象。重名、大小写冲突、文件/目录冲突、路径逃逸、保留元数据路径、链接、加密或超限均拒绝，不覆盖既有资源。
- 单次上限：32 包、4096 文件、解包总量 256 MiB、单文件 128 MiB。ZIP 分块限额并检查取消；7z 要求 py7zr 支持 max_extract_size，结束当前受限解包后检查取消。RAR 缺少受控限额组件，明确拒绝且选择器不承诺支持。
- 取消仅向已有任务发 Event，不创建第二写入任务；失败删除的仅是本次已创建文件/空目录，并恢复原索引。未声称进程被强杀或断电时具备跨 SQLite/文件系统崩溃原子性。

## 3. 代码与数据改动

基线 HEAD：`7a7b31e28feae350dc2ea57e237a3e6046de4c85`。开始时已有工作区改动全部保留，未纳入本轮领域提交：生产数据库、旧视觉 token/滚动优化、mapped_games.json、旧测试和旧视觉报告。

本轮提交：

- `3742cf6`：部分字段与游戏内标签范围。
- `cd1c349`：Inspector 描述片段边界。
- `c504d8b`：原类型目录 CRUD 与正式入口。
- `ccd78a2`：侧车导出与标签隔离。
- `f174a14`：多压缩包补齐与失败补偿。
- `0962a0a`：冻结目录审计生成工具。
- `8e043ae`：输入包暂存复制也实行分块限额与取消检查。

实际修改文件：

- `D:\project\steam-mod-manager\core\db_manager.py`
- `D:\project\steam-mod-manager\frontend\acquisition.py`
- `D:\project\steam-mod-manager\frontend\description_view.py`
- `D:\project\steam-mod-manager\frontend\registration.py`
- `D:\project\steam-mod-manager\frontend\type_management.py`
- `D:\project\steam-mod-manager\frontend\web\app.js`
- `D:\project\steam-mod-manager\frontend\web\index.html`
- `D:\project\steam-mod-manager\frontend\web\registration.js`
- `D:\project\steam-mod-manager\frontend\web\type_management.js`
- `D:\project\steam-mod-manager\frontend\web_bridge.py`
- `D:\project\steam-mod-manager\services\content_completion.py`
- `D:\project\steam-mod-manager\services\importers\archive.py`
- `D:\project\steam-mod-manager\services\info_sidecar.py`
- `D:\project\steam-mod-manager\services\mod_type_catalog.py`
- `D:\project\steam-mod-manager\tests\test_business_field_contracts.py`
- `D:\project\steam-mod-manager\tests\test_content_completion_contract.py`
- `D:\project\steam-mod-manager\tests\test_description_fragment_boundary.py`
- `D:\project\steam-mod-manager\tests\test_web_type_catalog_management.py`
- `D:\project\steam-mod-manager\tools\data_contract_audit.py`

生产数据：没有业务数据迁移，没有改 Schema，没有生产测试 Mod/标签/类型，没有删除 data 文件，也没有修改或批量复制 E:\mod。

恢复方案：代码按上述领域 commit 回退，保留原有脏工作区。生产库基线为 `D:/smm-contract-evidence-20261018/baseline/mod_manager.db`，一致性检查通过；本轮未做业务数据变更，无需恢复。今后显式类型变更的目录恢复副本只能结合当时数据库绑定核实后恢复，不能直接以老目录覆盖新绑定。

## 4. 测试与实际 Qt 证据

以下为不同批次，存在重复测试，不能相加当作独立用例总数：

- `field-tests.txt`：28 passed，1 skipped。
- `description-tests.txt`：5 passed。
- `type-tests.txt`：21 passed。
- `type-ui-tests.txt`：47 passed。
- `final-regression.txt`：62 passed，包含规定的四项 UI/封面安全门及本轮字段、补齐测试。
- `completion-limit-final.txt`：最终暂存限额修订后 13 passed。
- `completion-final.txt`：40 passed，含新增 Steam 多包不扁平化、独立连接重读、ZIP+7z、冲突/逃逸/特殊链接/限额、取消、损坏输入与索引失败撤回。
- `isolated-qt-results.json`：真实 Qt WebEngine 通过 UI 点击新增、重命名、删除类型；ID 稳定、现有 Graphics 标签保留，JSON 重新读取通过。隔离根为 `D:/smm-contract-evidence-20261018/qt-write-isolated`，不是正式资产截图替代品。
- `release-cases.json` / `release-qt-log.txt`：调用正式 main.main 的只读模式，加载真实资产，Layla→魂魄妖梦→Layla 切换，类型目录只读入口；退出正常，数据库主文件内容 hash 未变。未观察到日志中新增 JS 错误。

Inspector 测量：两例均属权威 app_id=646570（杀戮尖塔），不是其他游戏。相同 1728×900 逻辑视口，DPR=1.25，截图物理尺寸 2160×1125。Layla 主按钮修复前宽 53.6 px、修复后 208.8 px，与魂魄妖梦一致；Footer/Flex 原样，修复后按钮都是命令行直接子项。完整 rect、computed style、DOM、来源描述记录在 before/release-cases.json。

正式截图：

- 修复前 Layla：`D:/smm-contract-evidence-20261018/before-layla.png`
- 修复前魂魄妖梦：`D:/smm-contract-evidence-20261018/before-youmu.png`
- 最终 Layla：`D:/smm-contract-evidence-20261018/release-layla.png`
- 最终魂魄妖梦：`D:/smm-contract-evidence-20261018/release-youmu.png`
- 再次切回 Layla：`D:/smm-contract-evidence-20261018/release-layla-again.png`
- 正式类型目录只读：`D:/smm-contract-evidence-20261018/release-type-catalog.png`
- 隔离 Qt 类型改名：`D:/smm-contract-evidence-20261018/isolated-qt-type-renamed.png`

未清零的回归：`metadata-regression.txt` 为 49 passed / 5 failed。这 5 项在将原提交的 update_mod_user_metadata 注入测试进程后同样失败，见 `baseline-ownership-all.txt`（3 passed / 5 failed）。均涉及旧测试以 PK 调用 Frozen UUID 备份路径合同；未为此改生产身份/备份逻辑，不声称整个仓库测试全绿。

生产收尾审计 `production-end-audit.json`：逐表行内容 hash 及 Schema 与一致性基线相等。主库、WAL/SHM 时间变化及实例锁退出删除属于运行状态，不能当成业务写入。对其余基线文件逐项大小/mtime 检查未发现变化；没有对 11.7 GB 文件全部重哈希，因此不作逐字节全目录不变的额外承诺。

## 5. data 清理清单

- `D:/smm-contract-evidence-20261018/audit/DATA_AUDIT.md`
- `D:/smm-contract-evidence-20261018/audit/data-audit.csv`：全部 56,187 个精确路径，类型、大小、时间、Git 状态、用途、源码依据、恢复依赖、分类和建议。
- `D:/smm-contract-evidence-20261018/audit/data-suspects.csv`：B 类精确路径。

A：11,645,177,051 bytes，必须保留。B：31,619,532 bytes，身份修复隔离资料，恢复依赖未排除。C：0 bytes。可证明安全释放空间 0；实际删除 0。未按体积相同、文件名或时间推断重复。未调用旧审计工具可能跟随链接的 os.walk，基线扫描使用 lstat、reparse 检查和长路径前缀。

## 6. 尚未关闭

1. 用户报告的“添加标签直接清空 type_id”未在现存生产对象复现；需保留 NOT CLOSED，不能以横向修复代替直接根因证明。
2. 游戏内标签与类型目录、内容补齐的生产写入没有执行；只读正式 Qt 与隔离可写 Qt/测试分别记录，不互相替代。
3. RAR 补齐不支持；7z 依赖受限 py7zr API。大于上述输入限额的包会拒绝。
4. 补齐对可捕获错误有补偿，尚未验证强杀/断电恢复；正式真实归档多包流程未验收。
5. B 类隔离恢复资料尚不能归入可删除 C 类；本轮审计不授权清理。
6. 5 个历史元数据所有权测试继续失败，已确认同一基线失败签名，未扩展修改身份业务。
