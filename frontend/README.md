# SMM WebEngine 正式前端

在项目目录执行 `python main.py`，默认连接既有 SQLite 和 QSettings 指定的归档目录（当前为 `E:\mod`），启用已迁移的业务写入。`python main.py --read-only` 保留原生 SQLite 只读安全模式。兼容脚本最终也调用同一个 main.py。

资产库支持收藏、用户信息、标签和合集；获取与同步、WH3 加载顺序及部署快照使用既有 Worker、Service 和 DAL。游戏菜单中的“管理当前游戏”可读取、选择目录并保存原有游戏路径，不移动归档。输入文件和目录需由原生选择器明确授权；Workshop 源始终只读。

部署按 Mod 检查原策略、配置和 manifest；生产路径不再误套隔离目录限制。folder_copy 已完成正式界面往返，其他策略不能据此视为全部验收。历史复制 manifest 继续明确拒绝。部署工作台复用 WH3、Stellaris、CK3、BG3 各自的 Backend，投影操作前为具体配置文件创建恢复点；后面三个游戏的正式界面写入往返仍待验收。手动 Steam 官方元数据刷新可更新已同步对象；未迁移下载、凭据功能不冒充可用。

生产入口在持有实例锁后验证独立 SQLite/配置恢复点。恢复点仅含 SQLite、配置和设置，不复制 CAS 或 Mod 本体。业务原有备份和恢复逻辑继续使用；正式程序不依赖 tools/web_*_sandbox.py。Worker 完成后先销毁 View/Page，再销毁 Profile、关闭数据库和释放实例锁。

旧 Widgets MainWindow 已移除，ui 包仅保留仍需的 Worker 和共享/历史合同辅助模块，不再导出另一个正式前端。代码回退点：`smm-pre-release-retirement-20261015`。历史组合测试原生中止仍保留风险记录，不宣称根因彻底关闭；实际正式程序验证见 `D:/smm-production-entry-evidence/`。

游戏切换菜单的“添加游戏”使用既有正整数 AppID / 部署类型合同，可保存路径并立即切换。Inspector 已接通合法来源网页、已有离线页面安全阅读、Nexus HTML/MHTML 关联和用户封面覆盖；Nexus 联网刷新及未配置凭据的 mod.io 明确禁用并说明原因。

Windows 打包使用 `python -m PyInstaller SMM.spec`，生成带原版图标和应用身份的 SMM.exe。打包程序通过既有 QSettings 的 application_dir 指向现有数据目录，不复制数据库或归档。

游戏菜单支持搜索，并固定显示“添加游戏”和“管理当前游戏”；齿轮打开独立应用设置，使用原 QSettings 保存代理与 Steam Cookie。归档根目录显示当前值，运行中不切换或移动既有归档。

获取与同步的“扫描并同步全部 Mod”读取当前游戏 Workshop 配置，直接运行原 SyncWorker / ModSyncService，以 Steam time_updated 做新增、更新和跳过判断；不使用内容哈希决定更新。已部署与 manifest 保护继续生效。原生来源刷新仍在单 Mod Inspector 中。

收藏仅位于资产卡片封面星标。Mod 类型使用原 mods.type_id 与游戏类型目录；用户标签仍独立保存在 mod_tags。新版不展示或编辑历史 mods.category 字段，也不清空其历史数据。无类型 Mod 可从 Inspector 的“未分类”提示直接选择类型，完整编辑页保留类型设置。Steam / mod.io / GitHub 的“保存离线网页”调用原 OfflineManager，Nexus 使用“导入离线网页”；Playwright 来源保留原浏览器快照合同。

“从 SMM 注销 Mod…”位于 Inspector 更多菜单底部，调用原注册删除服务，仅移除记录及关系并保留文件；已部署、失败及未知状态拒绝注销。打开离线网页复用原 prepare_offline_open，将 manifest/CAS 恢复到 cache/offline_view，再交给系统默认浏览器，不再使用内部简化阅读器。原存档未捕获的远端资源或已损坏内容不会由打开操作重新下载。
