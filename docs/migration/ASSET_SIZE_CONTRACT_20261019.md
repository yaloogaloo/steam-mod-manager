# 资产大小口径与更新 — 2026-10-19

状态：CODE FIXED / FORMAL QT PASS；批准清单内大小校准 PASS。

唯一口径为当前 UUID 可证明的托管目录内本体存储字节。Steam 展开内容按文件字节求和，其他来源保留的压缩包按压缩包当前存储字节计入，不等于远程下载大小或安装展开大小。排除 SMM `.info` / `info`、缓存、备份和管理 Manifest，不跟随符号链接或 Windows reparse point；读取失败不把部分结果当作完整大小。已知空本体为 0，目录缺失或无法证明身份为未知。

旧计量包含管理元数据；注册时的约 600 B 是当时的元数据目录观测。新 Web 详情只读 SQL 历史观测，补齐流程没有触发重新计量，所以后来补齐的本体没有立即反映。旧版详情会强制重新观测，因此新前端暴露并放大了陈旧缓存问题。

修复范围：`services/dir_size.py` 统一排除规则和失败语义；`frontend/library_adapter.py`、`frontend/web_bridge.py` 在原有受限详情读线程中只测当前选中资产，禁止用陈旧 SQL 值回退；`frontend/web/app.js` 绑定返回的真实大小。`frontend/registration.py` 在原补齐/重新识别完成返回前调用原大小观测服务，并在失败回滚后恢复观测；未改变注册、类型、标签或身份合同。列表和翻页不执行递归计量。

证据目录：`D:/smm-card-size-stellaris-20261019`。

- REFramework：636→13,270,161 B；mod.io Additional power plants：638→50,027,498 B；GitHub 样本：15,257,009→10,278,537 B；Other：20,641,629→20,198,167 B。全部 13 个真实样本的文件计量与正式 Qt 显示一致，详见 `ui-size-validation.json`。
- 正式 Qt：`final-*.png`；五来源隔离注册/补齐回归见 `tests/test_asset_size_contract.py`。
- 正式 `main.py --validation-root`、真实 QWebChannel 补齐：0→65,672 B，同一 UUID、同一窗口立即显示，数据库先于成功响应更新，标签未变化。证据 `business-isolation/acceptance.json` 和 `size-before-completion.png` / `size-after-completion.png`。
- 相关测试 95 passed / 1 skipped；随后补充失败回滚测试 1 passed，见 `all-targeted-final.txt`、`size-rollback-final.txt`。

用户明确批准后，通过正式可写入口及原 `observe_mod_size(force=True)` 完成冻结 3,862 条记录的校准，最多两个后台任务。UUID、路径、旧四列及目录所有权均核实；3862 updated、0 skipped、0 errors。四列以外没有任何 Mod 字段变化，其他 SQLite 表均一致，`updated_at` 未变化。实际变更列为 `local_size_bytes` / `local_size_observed_at` 各 3862 条，`local_size_root_mtime` 220 条，状态仍为 ok。

即时一致性恢复点：`approved-production/before-size-mod_manager.db`；批准原文和计划 SHA：`approved-production/approval.json`；逐行回滚数据：`approved-production/size-ledger.jsonl`；结果：`size-result.json` / `size-db-diff.json`（均位于 `approved-production`）。回滚只恢复清单内四列，并要求当前值仍等于本次 after，防止覆盖之后的观测；不得恢复整库。此校准不修改任何本体、归档、Manifest、部署、类型或标签。
