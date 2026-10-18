# SMM 历史部署恢复与 Stellaris 加载取证 — 2026-10-18

**最终状态：NOT CLOSED。** 第一阶段只读取证、最小结果反馈修复和隔离回放已完成。生产部署、撤出、状态恢复、描述符与 Playset 修改均未执行；未启动真实游戏，不宣称 L4 生效。

代码提交：`fc26924`。本报告不构成生产写入授权。注册、Inspector、卡片、标签、类型及 UI 视觉未修改；开始前已有的工作区修改未混入提交。

## 1. Palworld：逐 Mod 证据与恢复候选

生产 SQLite / 正式 Qt 均为 0 个已登记部署。14 份 schema=0 Manifest 均通过既有 verified 身份迁移映射关联到当前 PK / Frozen UUID，并核对当前归档实体。迁移前原数据库备份目前缺失，因此未声称读取到了每个旧部署字段。

复用旧审计的原规则 E: 映射，逐文件哈希后确认 **106/129 个目标存在**，23 个缺失。14 个对象中仅 **PK 1966** 同时具有完整历史哈希、当前源、当前目标、明确的覆盖前所有权记录和无共享声明证据，可以进入“仅恢复登记状态”方案。

下列“相同”是当前源与目标内容相同；它不替代历史哈希和覆盖前所有权。仅凭文件相同的对象不进入恢复清单。

- **1904 — [Palworld 1.0] 4x Storage - Bigger Chests and Containers**：4/4 存在、4 个相同；历史哈希和 backup 键均缺失。不可安全恢复。
- **1928 — BetterNightLight**：1/1 存在；原源位于失效 import_cache，现有本体为 RAR，本轮未建立精确成员内容证明；缺历史哈希和所有权。不可安全恢复。
- **1957 — Fast Travel From Anywhere & Uncharted Teleport**：1/1 存在且相同；缺历史哈希和所有权。不可安全恢复。
- **1966 — Guild Feed Box Sync / 公会饲料箱**：9/9 存在；9 个当前源、Manifest source_hash、目标 SHA256 全部一致；每项明确 `backup:null`，不是省略 backup；已知同游戏历史声明中没有共享目标。**唯一状态恢复候选，尚未写入生产。**
- **1971 — HUD Locator All in 1 v2.10.0**：0/16 存在；16 个缺失，缺历史哈希和所有权。保留差异，不补拷。
- **1983 — Large Fridge Chillet**：4/4 存在且相同；缺历史哈希和所有权。不可安全恢复。
- **1995 — MapUnlocker**：2/2 存在；历史源是已失效缓存，现 ZIP 有顶层目录剥离及多个归档输入，未取得原解包选择证明；缺历史哈希和所有权。不可安全恢复。
- **2010 — Pal Analyzer**：1/1 存在，但当前归档 PAK 与目标内容不同；缺历史哈希和所有权。不能将它覆盖为当前版本。
- **2025 — PalSchema**：0/2 存在；发布版与开发版归档均在，原缓存来源未唯一重建；2 个缺失，且缺历史哈希和所有权。不可安全恢复。
- **2030 — Play as Mambo**：1/1 存在且相同；缺历史哈希和所有权。不可安全恢复。
- **2040 — RE-UE4SS**：E: 取证映射下 20/20 存在；发布版与开发版有重复成员，不能仅因候选成员字节相同就认定历史归档版本。缺历史哈希和所有权，另有 F: 自定义路径冲突。不可安全恢复。
- **2045 — Remove flying stamina cost**：3/3 存在；历史解包缓存路径与当前 ZIP 顶层结构未建立唯一关联；缺历史哈希和所有权。不可安全恢复。
- **2079 — WorldTreeDragonSuperEvolution**：E: 取证映射下 56/56 存在且相同；缺历史哈希和所有权，另有 F: 自定义路径冲突。不可安全恢复。
- **2092 — 水晶星辉·唤冬兽 Crystal Starlight Frostallion**：4/9 存在且与记录及源一致；全部 9 项均有历史哈希和显式 `backup:null`，但另 5 项缺失，不能恢复为完整部署。

23 个缺失目标仅归属 1971（16）、2025（2）、2092（5）。完整路径见 [palworld-missing-targets.csv](D:/smm-deployment-closure-20261018/palworld-missing-targets.csv)。没有补拷、删除或重写历史 Manifest。

已知清单中本次 14 个对象的映射目标没有交叉声明；此结论不表示游戏目录中所有文件都归 SMM 所有。其余游戏原审计发现的 162 个共享历史目标仍沿用原拒绝规则，未放开。

**路径需要分开解释。** 上述 106/129 使用既有审计不带自定义覆盖的原规则映射。2040 当前 `custom_deploy_path=F:/SteamLibrary/steamapps/common/Palworld/Pal/Binaries/Win64`，2079 为该目录下 `ue4ss/Mods`；实际安装位于 E:。严格携带当前自定义配置的 Backend 仍投影到不存在的 F:，因此不能把 E: 的 76 个文件当作这两项当前配置已正确。JSON 同时保存 `target` 与 `backend_target_with_current_custom_path`，未静默改配置。

逐 Mod CSV 包含当前 PK、Frozen UUID、旧 mod_id、身份结论和阻断原因：[palworld-per-mod.csv](D:/smm-deployment-closure-20261018/palworld-per-mod.csv)。逐文件源路径、旧目标、映射目标、所有哈希和备份键见 [palworld-item-evidence.json](D:/smm-deployment-closure-20261018/palworld-item-evidence.json)。

### 历史状态丢失机制

`tools/identity_full_rebuild_apply.py::_insert_mod` 的 INSERT 明确将部署字段置为 `not_deployed / 空时间 / 空路径 / 空错误`。`tools/identity_rebuild_runtime_migrate.py` 虽恢复 deployment_records/items，却没有选择或恢复 `mods.deploy_status/deploy_time/deploy_path/deploy_error`。2026-09-06 的迁移报告记录恢复了 3 个记录、59 个记录项，并不等于恢复了逐 Mod 字段。

这是可以定位的丢失机制，不代表已证明全部 14 个旧数据库行原先都是 deployed。没有重跑旧迁移工具，也没有给所有有目标文件的 Mod 补状态。

## 2. Stellaris：描述符、直接启动与 Launcher

沿原 `paradox_launcher` Backend、`map_to_launcher_id`、`sync_paradox_launcher` 和用户目录 resolver 检查，实际目录为：

- 游戏：`E:/SteamLibrary/steamapps/common/Stellaris`，安装配置版本 Cygnus v4.5.2。
- 用户配置：`C:/Users/king/Documents/Paradox Interactive/Stellaris`；安装目录没有 `userdir.txt`。
- Steam 原生内容：`E:/SteamLibrary/steamapps/workshop/content/281990/3818467946`。

**缺失对象**为 `mod/ugc_3818467946.mod`，Mod 名称“舰船预设蓝图”，当前 PK `3770023516`，Frozen UUID `9446648f-d183-45a3-ad89-574235cac8aa`。原生内容和内部 `descriptor.mod` 存在；Steam appworkshop ACF 也有该 ID 的 Installed / ItemDetails 记录，但 Launcher `mods` 表没有对应 steamId / gameRegistryId 行。因此不能将“Workshop 已下载”当成“Launcher 已注册”。

原 Backend 只使用已有用户描述符；缺失时进入 unresolved，不创建 `.mod`。Playset 同步仅 UPDATE 已有成员，不 INSERT 注册行或成员。重复 SMM 部署不会修好缺失描述符或 Launcher 注册链。无法从现有日志唯一确定 Launcher 未完成该项注册的历史原因；未推断为杀毒拦截或下载损坏。

**当前 Playset 精确状态：**

- `06d06b13-9c0a-4bcd-97a2-6622d408e495` / `Initial playset`：380 条成员、19 条启用、`isActive=0`。
- `3696fce0-ee93-440f-b014-587ccd572a72` / `111`：0 条成员、0 条启用、`isActive=0`。
- SMM 目标 18 项中，Initial playset 有 17 条成员，仅 10 条启用，7 条关闭；第 18 项 3818467946 无注册行。
- Initial playset 另启用 9 项，它们在 SMM 中均是 not_deployed。现有 Backend 按全部受管成员同步，会关闭这 9 项；不能不经选择就激活并覆盖这个组合。逐项见 [initial-playset-extra-enabled.json](D:/smm-deployment-closure-20261018/initial-playset-extra-enabled.json)。

`dlc_load.json` 当前含 18 项，其中 1 项指向缺失描述符；17 个其他描述符及其目标目录存在。SMM 按 canonical 成员写这个直接启动列表，不以 Playset 激活情况决定是否写它。

本机已安装 Launcher 2026.11.1 的 `app.asar` 中，`getActiveWithMods` 仅选择 active Playset；`vqe.execute` 取其 enabled 成员，无 active 时取空集合；LFV0 的 `x2.save` 将有效成员的 gameRegistryId 保存到 enabled_mods。激活、排序、增删成员或刷新加载列表都会调用导出。因此 **dlc_load.json 不是一份能独立覆盖 Launcher Playset 的永久设置**；后续 Launcher 操作可能重写它。直接启动通常沿现有加载文件，启动方式与 Launcher 导出之间的关系不能混为一次验收。

以上是本机安装代码和配置证据，没有执行 Launcher 导出或实际游戏运行。精确源码偏移和哈希见 [launcher-primary-chain.json](D:/smm-deployment-closure-20261018/launcher-primary-chain.json)、[launcher-installed-source.json](D:/smm-deployment-closure-20261018/launcher-installed-source.json)。现有游戏日志有描述符版本警告及历史 Mod 内容痕迹，不能据此逐一证明这 18 项在当前两种启动方式下实际加载。

验收分层：L1 登记 / UI 18 一致 **PASS**；L2 描述符与本体目录 **17/18，NOT CLOSED**；L3 Direct 含 1 个无效引用、Launcher 无 active Playset，均 **NOT CLOSED**；L4 两种启动方式均 **NOT CLOSED**。

## 3. 最小代码修复和隔离验证

`fc26924` 仅在 `ModDeployer._annotate_paradox_launcher_result` 增加结果判定：有预期启用成员，但无 active Playset 或 Launcher 不含全部预期成员时，整体返回 `projection_failed / partial_state_saved`。直接文件写入成功字段仍如实保留，canonical 已部署成员不抹掉，`game_load_verified=False`。最后一个成员撤出后空集合不因没有 Playset 误报失败。

没有修改描述符生成算法、sync 算法、Playset 选择、生产准入或 UI。正常测试夹具现在明确建立已经注册的 Launcher 成员，避免用空 Playset 冒充完整成功；失效描述符和 inactive 场景明确断言部分失败。

实际修改文件为：

- `services/deploy.py`。
- `services/deploy_manifest_compat.py`：仅校正说明，明确部分 v0 项也记录过所有权，未改变拒绝写入规则。
- `tests/helpers/paradox.py`。
- `tests/test_paradox_activation.py`。
- `tests/test_stellaris_activation.py`。
- `tests/test_paradox_result_completeness.py`。
- `tests/test_deployment_compatibility_evidence.py`。
- 本报告。

证据：

- 静态：6 个 Python 文件 AST 解析通过；按 CRLF 配置的 Git diff 空白检查通过。
- 相关隔离测试：`targeted-final.txt` **79 passed**，包含真实 Service / Worker / Bridge 临时根回放、inactive Playset 不改激活状态、原文件不重写、重复部署和结果反馈。
- 增量旧 schema 门禁：`legacy-schema-gate.txt` **21 passed**，包含 schema=0/1 即使当前内容相同仍不能取得生产写入准入。与 79 项有重叠，不相加。
- AGENTS 强制 canonical 八文件门禁：`canonical-gate.txt` **136 passed / 2 failed**。失败仍是 WH3 `test_sort_mode_library_order_and_viewport`（按钮父容器不符）、`test_filter_row_does_not_keep_phantom_height`（68 > 68 不成立）。测试销毁还有旧 QScrollArea 回调日志。未更改旧 UI 或削弱断言，门禁未通过。
- 真实源数据副本：PK1966 状态恢复在独立数据库中由 0→1，并将整行精确恢复；updated_at 未改变。仅克隆 DB 被写入，真实源、目标和 Manifest 不变。

本轮已知历史库存为 227 份 schema=2、56 份 schema=0，**无 schema=1 生产样本**。schema=1 仅有隔离拒绝验证，不宣称现场兼容通过。原 Anno PK1230 的 187 文件及备份证明沿用前轮证据，未重扫；不据此放开其余历史清单。

## 4. 真实正式 Qt 只读验收

用户关闭原实例后，经正式 `main.main(['--read-only'])` 启动 Qt WebEngine，读取现有真实资产。未绕过实例锁、未使用浏览器或 Demo 代替。逻辑尺寸 1728×900，DPR 1.25，截图物理尺寸 2160×1125。

- [Palworld 正式 Qt](D:/smm-deployment-closure-20261018/qt-readonly-1623730.png)：200 个资源、0 个已部署；PK1966 仍为未部署，详情证据为 compatibility_required，9 个目标存在。
- [Stellaris 正式 Qt](D:/smm-deployment-closure-20261018/qt-readonly-281990.png)：390 个资源、18 个已部署；缺失描述符对象保持 canonical 成员，同时提示部署状态待确认。
- 已部署筛选返回 0 / 18；浏览、详情和 QWebChannel 读取完成；最终运行 JS 控制台异常为 0。[qt-readonly.json](D:/smm-deployment-closure-20261018/qt-readonly.json) 保存实际读数，adapter.writable=false。

这是生产只读 UI 验收，不能替代生产修复后或真实游戏 L4 验收。未点击生产部署、撤出、重试投影、收藏或配置保存。

## 5. 待审批生产方案、影响和回滚

### A：Palworld 仅恢复一个历史登记

对象仅为 **PK1966 / ecaeb451-b764-4586-ace7-42b0e8dba976**。完整可审查数据见 [palworld-production-plan.json](D:/smm-deployment-closure-20261018/palworld-production-plan.json)。

拟影响仅 `data/mod_manager.db` 的该行四个部署字段：status→deployed；time→Manifest 的 `2026-08-30T12:51:29+00:00`；path→已证明的 `E:/SteamLibrary/steamapps/common/Palworld/Pal/Binaries/Win64/ue4ss/Mods/Guild Feed Box Sync`；error→空。path 是历史目标的共同目录重建值，不伪称取自已丢失的旧数据库，也不填入当前通用 `Palworld/Mods` 路径。

执行前需重新确认精确 PK/UUID/app/path、四个旧值、Manifest SHA、9 组源/历史/目标 SHA、显式 backup:null 和同游戏共享声明，并建立即时 SQLite 一致性备份。任何变化即停止。生产执行必须仍走正式入口的 mutation context 和受控维护动作，本轮没有实现或使用绕过它的生产脚本。

不改 updated_at，不改归档、游戏文件、类型、标签、历史 Manifest；不授予 schema=0 撤出或覆盖能力。此恢复只证明历史文件部署，不证明 UE4SS 和游戏实际加载。今后重新部署仍须单独核实当前策略规划与历史 UE4SS 目标的差异。

回滚只在该行仍等于批准后的值时恢复原四字段：`not_deployed / 空时间 / 空路径 / 空错误`。不使用旧整库覆盖后续用户更改。副本已验证整行精确回滚。

其他 13 项不进入本次状态写入；23 个缺失目标保持缺失。2040 / 2079 的 F: 配置也不在此包内自动纠正。

### B：Stellaris 单个描述符

只读生成的单文件草案：[ugc_3818467946.mod](D:/smm-deployment-closure-20261018/isolation/descriptor-proposal/ugc_3818467946.mod)。详见 [stellaris-production-plan.json](D:/smm-deployment-closure-20261018/stellaris-production-plan.json)。

拟新增且仅新增 `C:/Users/king/Documents/Paradox Interactive/Stellaris/mod/ugc_3818467946.mod`，保留原生 descriptor 的作者元数据，仅增加实际原生目录 path。源 SHA256=`de0cf1da9e045c07c020c012b1d73c9e31e311d75208b0e174819715938f9103`；草案 SHA256=`35eae93a026150c194357144aedf45ba24c288859e14093719c8f9e9a133b2fb`。

执行采用“目标必须仍不存在”的排他创建；源 SHA 或原生目录变化即停止，不覆盖后来生成的用户文件。当前 Direct 列表已含该引用，B 不需要改 dlc_load.json。回滚仅删除仍等于批准 SHA 的这个新文件，保留修复前快照。

此包仅修复文件级缺口，**不会补 Launcher 注册行或激活 Playset**。原 `supported_version="v4.5.2"` 保留未改，元数据兼容性仍需实际游戏验证。更优先的原生恢复途径仍是 Steam/Launcher 完成该项注册；若采用它，应先观察生成结果，再决定是否仍需 B，不能重复覆盖。

### C：Launcher 配置与两种启动方式闭环

需要用户指定 Playset。建议通过原 Launcher 新建一份只含预期 18 项的专用 Playset，保留 Initial playset 和 111 的成员、顺序和启用状态；不自动执行。若选择使用 Initial playset，必须单独批准其 7 个关闭成员及另 9 个启用成员的具体差异，不能沿用 A/B 的授权。

拟影响范围为 Launcher 的注册表/所选 Playset 成员和激活状态、导出的 dlc_load.json，以及 Launcher 自身可能维护的 Playset 备份；实际文件包括用户目录 `launcher-v2.sqlite`（活跃时连同 WAL/SHM 一致性）、`dlc_load.json`、相应 `playsets_backup` 文件。**成员与顺序批准前，C 没有可执行的生产差异清单。** 不直接用 SQL 猜测 Launcher 内部注册合同，不重写全部描述符。

实施前关闭游戏和 Launcher、做一致性快照并记录所选集合；实施后逐行对比，任何非批准 Playset 变化停止。回滚先核实没有并发用户更改，再恢复批准的差异；只有完全独占且无其他变化时才可使用完整 Launcher 恢复点。

配置验收后分别执行 Launcher 启动和直接 EXE 启动，记录启动参数、userdir、游戏版本、实际消费配置 SHA 和逐 Mod 可观测加载证据。加载列表或主菜单出现不算全部 18 项 L4 证据。没有这些证据，继续保持 NOT CLOSED。

## 6. 数据完整性、历史差异和跨游戏风险

[production-safety.json](D:/smm-deployment-closure-20261018/production-safety.json) 的本轮开始 / 结束一致性快照比较：SQLite schema 与所有表指纹完全相同；Launcher schema 和所有表相同；14 份 Palworld Manifest 哈希相同；129 个映射目标存在性、大小和 mtime 相同；18 个 Stellaris 用户描述符状态/哈希及 dlc_load.json 相同。生产业务写入命令为 0。最终目标检查是元数据检查，不冒充再次全量内容哈希。

前轮两处数据库差异继续追踪到具体时间线：[prior-write-provenance.json](D:/smm-deployment-closure-20261018/prior-write-provenance.json)。games.app_id=2054972 在前轮部署起点不存在，注册轮起点已存在，updated_at=09:54:39Z；本轮起点已为11:57:24Z，其他配置相同。Mod PK3770023545 的 updated_at 在前轮由10月17日10:52:00Z变为10月18日07:13:42Z，此后两个起点及本轮结束均相同。

`frontend/game_configuration.py` → DAL 可创建/保存游戏；`_append_mods_updated_at` 及合法 user_edit/import/create 路径可更新时间。现有 identity_audit_log 只记录该 Mod 的身份创建，没有对应游戏配置或本次时间字段变动的操作者记录。因此**能定位发生区间和合法入口，不能证明具体写入来源，归因 NOT CLOSED**。没有为了指纹相同而删除游戏行或回滚更新时间。

复用 [18 游戏矩阵](D:/project/steam-mod-manager/docs/migration/SMM_DEPLOYMENT_COMPATIBILITY_MATRIX.md)，仅增量核实 Palworld、Stellaris 和强制测试中的 WH3 风险。其余游戏沿用前轮证据，不升级验收结论，不扫描全量归档。[game-matrix-increment.json](D:/smm-deployment-closure-20261018/game-matrix-increment.json) 明确哪些是复用结果。

本轮隔离副本、描述符草案和中间验收日志作为审查/回滚证据保留；未绕过审批清理隔离目录。

## 7. 最终结论

- 第一阶段逐项取证及生产只读边界：**PASS**。
- Service 对 Launcher 部分完成结果的最小修复与相关隔离测试：**PASS**。
- 正式 Qt 只读加载、详情、部署筛选：**PASS**。
- Palworld 单候选安全方案及副本回滚：**PASS**；生产状态恢复及其余对象：**NOT CLOSED**。
- Stellaris 文件部署 / 配置有效 / 游戏实际加载：分别 **NOT CLOSED / NOT CLOSED / NOT CLOSED**。
- schema=1 现场兼容、强制 WH3 两项门禁、历史两处写入归因：**NOT CLOSED**。

**整体 NOT CLOSED；下一步是审批具体生产对象和恢复方法，并确定 Launcher Playset，不能用部署数量变化替代游戏加载验收。**
