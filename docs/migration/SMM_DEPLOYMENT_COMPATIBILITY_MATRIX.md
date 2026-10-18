# SMM 部署兼容性矩阵 — 2026-10-18

**整体 NOT CLOSED。** 数据来自正式生产库一致性快照、原 Backend 路径规则和正式 Qt 只读界面。未执行生产部署、撤出、排序或状态重写。表内数量一致仅证明 L1 展示链一致，文件存在仅为 L2 部分证据，不等于游戏加载。

DB/Bridge/UI 为已登记部署数量；Manifest 为历史复制清单数量，不把它等同当前部署集合。未归属占位 AppID=0 不算已支持游戏。

| 游戏 / AppID | 实际 Backend / 历史策略 | DB / Bridge / UI | Manifest 数量 | 目标核验 | 风险与验收 |
|---|---|---|---|---|---|
| 暗黑地牢 / 262060 | folder_copy | 57 / 57 / 57 | 57 | 已部署记录的旧绝对目标 7546/7546 存在；全部历史目标 7546/7550 | L1 数量一致；files_present=56、unknown=1、inconsistent=1；NOT CLOSED |
| 群星 / 281990 | paradox_launcher | 18 / 18 / 18 | 0 | 描述符及本体目录 17/18；Launcher 激活 Playset：无 | L1 数量一致；unknown=1、configuration_warning=17；NOT CLOSED |
| 文明Ⅵ / 289070 | folder_copy | 32 / 32 / 32 | 33 | 已部署记录的旧绝对目标 1991/1991 存在；全部历史目标 2009/2009 | L1 数量一致；files_present=3、compatibility_required=30；NOT CLOSED |
| 巫师三 / 292030 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| 星露谷物语 / 413150 | stardew_valley | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| Wallpaper Engine / 431960 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| 杀戮尖塔 / 646570 | slay_the_spire；历史 custom_path/slay_the_spire | 29 / 29 / 29 | 32 | 已部署记录的旧绝对目标 29/29 存在；全部历史目标 30/32 | L1 数量一致；compatibility_required=31、unknown=1；NOT CLOSED |
| Anno 1800 / 916440 | anno_1800；历史 anno_1800/custom_path | 28 / 28 / 28 | 29 | 已部署记录的旧绝对目标 12989/12989 存在；全部历史目标 13696/13696 | L1 数量一致；unknown=2、compatibility_required=27；NOT CLOSED |
| 博德之门Ⅲ / 1086940 | folder_copy → flat PAK / custom + BG3 投影；历史 custom_path/folder_copy/pak_mod_path | 23 / 23 / 23 | 58 | 已部署记录的旧绝对目标 651/656 存在；全部历史目标 651/714 | L1 数量一致；files_present=2、unknown=35、compatibility_required=18、inconsistent=3；NOT CLOSED |
| 赛博朋克2077 / 1091500 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| 全面战争 战锤Ⅲ / 1142710 | warhammer3_pack | 43 / 43 / 43 | 0 | 原生 Workshop 包存在 43/43；used_mods 单独留证，未验证游戏加载 | L1 数量一致；native_present=43；NOT CLOSED |
| 十字军之王Ⅲ / 1158310 | paradox_launcher | 7 / 7 / 7 | 0 | 描述符及本体目录 3/7；Launcher 激活 Playset：无 | L1 数量一致；unknown=4、configuration_warning=3；NOT CLOSED |
| Palworld / 1623730 | palworld_pak；历史 custom_path/palworld_pak | 0 / 0 / 0 | 14 | 旧 F: 目标 0/129；原规则映射当前 E: 后 106/129，未覆盖或补拷 | L1 数量一致；compatibility_required=6、unknown=6、inconsistent=2；NOT CLOSED |
| 潜行者Ⅱ / 1643320 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| 天国拯救Ⅱ / 1771300 | kcd2_mod_manifest；历史 custom_path/folder_copy/kcd2_mod_manifest | 28 / 28 / 28 | 28 | 已部署记录的旧绝对目标 132/132 存在；全部历史目标 132/132 | L1 数量一致；files_present=28；NOT CLOSED |
| 小丑牌 / 2379780 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |
| 逃离鸭科夫 / 3167020 | duckov；历史 custom_path/duckov/folder_copy | 28 / 28 / 28 | 32 | 已部署记录的旧绝对目标 1321/1321 存在；全部历史目标 1321/1897 | L1 数量一致；files_present=28、inconsistent=2、compatibility_required=1、unknown=1；NOT CLOSED |
| 龙之信条Ⅱ / 2054972 | folder_copy | 0 / 0 / 0 | 0 | 无已部署或历史复制样本 | 无样本；NOT CLOSED |

## 证据边界

- Anno 1800：当前 28 个已登记，旧绝对目标全部存在；没有复现“当前全为未部署”。代表对象 PK=1230 / Frozen UUID=f5b7f6aa-f1b7-4602-bf37-4da414e89dea 的 187 个文件及覆盖前备份已做完整只读哈希核验，通过；不推断其余 28 份清单全部可写。
- Palworld：14 份 schema=0 清单保留旧 Workshop 数字身份及 F: 绝对路径，当前配置为 E:。106/129 文件存在，不代表 14 个完整或原始覆盖所有权已经证明。
- Stellaris：17/18 用户描述符存在且指向存在目录；3818467946 原生 Workshop 目录及 descriptor.mod 存在，但用户目录 mod/ugc_3818467946.mod 缺失。Direct 列表有18项，Launcher 无激活 Playset；L2/L3 均未全通过，L4 NOT CLOSED。
- CK3：3/7 描述符与目标存在，Launcher 无激活 Playset。保留7个 canonical 成员，不据此删减排序集合。
- BG3 的复制、PAK 与 modsettings 投影不能按 Paradox 或 WH3 推断。WH3 的原生包存在也不等于 used_mods 和实际加载已通过。
- 352 个有部署登记或历史清单的对象已逐项导出，117 个 files_present 仅表示清单目标存在，不能解读为117个游戏实际加载成功。
- 历史绝对目标中发现162个被多实体声明的路径。清单见 shared-targets.json；兼容写入前还按原路径规则核对同游戏的当前映射，发现共享就拒绝撤出/覆盖，不把多重声明自动判成可删除重复项。

## 对应文件

- `D:/smm-deployment-evidence-20261018/game-matrix.json`
- `D:/smm-deployment-evidence-20261018/manifest-inventory.json`
- `D:/smm-deployment-evidence-20261018/target-audit.json`
- `D:/smm-deployment-evidence-20261018/remapped-target-audit.json`
- `D:/smm-deployment-evidence-20261018/live-final/deployment-evidence.csv`
- `D:/smm-deployment-evidence-20261018/paradox-readonly.json`
- `D:/smm-deployment-evidence-20261018/other-projection-files.json`
- `D:/smm-deployment-evidence-20261018/anno-readonly-compatibility.json`

可重复只读审计：`python tools/deployment_consistency_audit.py --db <明确数据库路径> --out <独立证据目录>`。数据库使用 native mode=ro；输出目录不得与项目、归档、游戏或特定 Backend 用户配置目录重叠。此工具不执行部署、迁移或修复。
