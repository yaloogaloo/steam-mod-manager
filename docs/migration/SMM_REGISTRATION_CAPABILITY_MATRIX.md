# SMM 注册能力矩阵 — 2026-10-18

游戏归属来自用户当前选择，来源平台独立选择。`core/mod_platform.py:573` 的 `get_available_sources` 对所有游戏提供 Steam、Nexus、mod.io、GitHub、Other；映射与凭据只限制相应的自动能力。下列 PASS 指手工登记/隔离归档合同，不代表远端资源存在或所有自动下载均受支持。

| 来源 / 方式 | 全游戏手工登记 | 身份和本地内容条件 | API / 自动能力边界 | 验证证据 |
|---|---|---|---|---|
| Steam URL / ID | PASS | 正整数Workshop ID；链接验证官方HTTPS域名；允许无本体 | 登记无需API Key；不联网确认远端条目 | 任意游戏999999登记、重复检测、0及伪域拒绝 |
| Steam本地目录 | PASS | 明确选择目录、真实文件、独立Workshop ID；目录名不推断ID | 不要求先创建空Mod | 原ImportWorker回归，归档内容和UUID/PK持久化 |
| Steam ZIP | PASS | 明确选择ZIP及Workshop ID，保留原压缩包及安全解包检查 | 现有Steam ZIP体积/路径限制继续生效 | `test_web_acquisition.py` |
| Steam Workshop目录同步 | 按原同步合同 | 需真实原生Workshop路径、所选AppID及条目数字目录；不能用普通目录名称伪造身份 | 游戏没有Workshop目录时无法扫描，不影响手工Steam登记 | `test_web_workshop.py`；未扫描或同步生产资源 |
| Nexus URL / ID | PASS | 有效URL或正整数ID；已知游戏冲突拒绝；未知映射/仅ID需要明确确认当前归属 | 不要求API Key、不编造Nexus游戏ID；纯ID不补猜游戏slug | DD2 URL及ID、Stellaris Nexus、未知游戏；重复与冲突回归 |
| Nexus HTML / MHTML 单文件 | PASS | 真实页面URL/ID/名称，或页面ID与用户补充URL一致；未知映射确认后继续 | 仅本地解析；不执行页面脚本或网络下载 | 用户REFramework MHTML，隔离注册并重开数据库验证 |
| Nexus HTML / MHTML 批量 | PASS | 各项独立解析；新项、待确认、重复、失败分开；同来源只选一个快照 | 使用原ImportWorker和离线网页归档/CAS；不部署Mod | 真实浏览器保存页面混合批次、重复、单项变化及补偿回归 |
| Nexus本地内容并附网页 | PASS | 页面与填写来源同时比较游戏slug和Mod ID；确认归属后直接归档本体 | 无需先登记空Mod；不自动联网刮削 | 隔离DLL内容、离线页归档、直接命令不能绕过确认 |
| mod.io URL / ID | PASS | `/g/<game>/m/<mod>`链接或正整数API Mod ID，来源slug不当作Steam AppID | 手工登记不受已知官方游戏映射限制、不要求API Key；自动API仍按原映射/凭据合同 | Anno1800 URL/ID、任意游戏URL；0及平台首页拒绝 |
| GitHub | PASS | 有效owner/repository链接；可无本体或直接归档已有目录/压缩包 | 手工登记无需抓取令牌；联网请求与私有仓库访问仍按原能力 | Palworld GitHub；本地目录、ZIP及多压缩包回归 |
| Other | PASS | 来源链接可选；无链接使用原有独立本地身份，不按名称合并 | 无API和游戏映射要求 | 任意游戏无本体登记及本地内容归档 |
| 本地目录 / 压缩包 | PASS | 明确来源身份和目标游戏；ZIP/7z/RAR按现有平台及归档支持处理 | 保留路径、资源授权、档案格式与冲突校验 | Steam ZIP、非Steam ZIP/7z、多包与目录批次回归 |
| 无本体登记 | PASS | 有效来源身份或Other本地身份；提交后才创建托管目录与业务记录 | 无需预先创建空Mod，不自动下载/部署 | DB、归档、`content_missing`及重启持久化 |
| 后续内容补齐 | PASS | 明确已有Frozen UUID及选择内容，重名/替换冲突拒绝 | 复用 `frontend.registration.complete_content` 和原索引/内容状态服务 | 同一身份、用户封面/描述保留，失败补偿及状态持久化 |

`ui/registration/{appid}` 只保存四项UI选择，不保存来源身份、临时路径或凭据，不参与上表的准入判断。正式重启及切换游戏结果保存在 `D:/smm-registration-evidence-20261018/registration-restart.json`。

完整验收、截图、代码位置及安全边界见 [SMM_FREE_REGISTRATION_ACCEPTANCE_20261018.md](SMM_FREE_REGISTRATION_ACCEPTANCE_20261018.md)。自动API、真实Steam同步和生产注册均没有在用户资产上执行；不能把上述隔离合同验证表述为远端下载或真实游戏加载验证。
