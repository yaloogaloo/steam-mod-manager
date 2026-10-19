# 正式 Qt 前端性能与渲染稳定性审计 — 2026-10-19

历史证据现位于 `D:/project/steam-mod-manager/_tmp/protected_recovery/smm-performance-20261019`，由并行临时目录治理任务迁入。最终测试、安全及交付索引位于 `D:/project/steam-mod-manager/_tmp/agent_runs/web-performance-20261019`。只读正式入口 `main.main(['--read-only'])`，真实 Stellaris 390 个 Mod、Wallpaper Engine 1,114 个 Mod。产品改动仅 `frontend/web/app.js` 的节点复用及浏览位置恢复时机，没有改视觉、CSS、尺寸算法、Inspector 内容、工作台、Worker/Service、QWebChannel 合同或数据。没有关闭 GPU、减少封面质量/密度、禁用动画或增加产品延时。

## 结论与归因

1. **快速滚动的主要瓶颈已确认：跨虚拟范围时全量销毁可见卡片。** 原 `renderGrid` 每次新建全部 article/IMG，再 `replaceChildren(fragment)`；连续选择触发 reflow/restoreAnchor，也经过同一重建路径。同范围滚动的跳过逻辑有效，但跨一行后仍重建全部重叠项。正式 A/B 中只替换这一函数的节点管理，快速滚动长尾与脚本/布局耗时同步下降，建立了可复核的因果证据。
2. **用户随后明确症状为“快速滚动资产库时，卡片或封面闪烁”。** 此触发路径与已证实的 DOM/图片节点连续性被破坏、大量重复 IMG load 事件及滚动掉帧一致。修复保留重叠卡片与 IMG，消除了这条无意义失效路径；但是否完全消除用户所见闪烁仍需本人对照录像/实际操作。不能把它直接归因于 GPU，也没有证据确认扫描线撕裂、错误资产图、GPU 旧纹理或驱动故障。没有用静态 QWidget.grab 宣称动态闪烁已修复。
3. 两者可能共享节点失效/重绘开销，但**所有用户可见异常具有同一根因尚未证明**。GPU 实际为 RTX 3060、ANGLE/D3D11，gpu_compositing/rasterization enabled，GPU 在主 Python/Qt 进程内；没有因只看到一个 QtWebEngineProcess 就认定 GPU 未运行。短程正式复核无渲染进程终止，日志未记录对应 GPU 错误；不能据此排除未复现故障。
4. Bridge 读请求本就在 QThreadPool（4 并发）执行。本轮没有把已有 Worker 改为另一套业务实现。读接口/消息形状、预览尺寸和编码不变；桥接消息量没有明显减少，图片工作线程读取/编码成本仍存在。浏览器 load 事件减少不能直接当成同数量的图片解码减少；Chromium Trace 中保留实际 Decode Image 等事件。
5. **续测确认并去掉第二处重复渲染。** `inspect`、`closeInspector` 和密度切换先 `reflow()` 按旧滚动位置绘制，再 `restoreAnchor()` 绘制第二次；CPU profile 的搜索长任务窗口中，renderGrid/restoreAnchor 占主要采样。最终改为计算几何后先恢复浏览位置、仅绘制最终范围。相同三轮 CPU 采样由 56/66ms 两次长任务变为 0，最终 601 秒混合验证也没有 ≥50ms Chromium 长任务。Qt 回调及少量 rAF 长帧仍未全部关闭，不能宣称长期完全流畅。

## 历史责任边界与漏检原因

`git blame` 和 `git log -S 'function renderGrid'` 指向 `df1bec3`（2026-10-15，重构 UI）：当时已有创建全部可见节点及 `replaceChildren` 的实现。最近的 `5ac77fb` 加入同范围跳过，`b6a5d71` 改方案 C 信息内容，`81b024a` 复用工作台详情；它们不是上述全量替换模式的首次引入者。没有同硬件、同资产的历史运行基线，**FIRST BAD PERFORMANCE COMMIT NOT PROVEN**；不能仅凭源码归属宣称 df1bec3 当时已经发生同等掉帧，也不能把最近提交当作已证明的性能退化提交。

具体漏检点：

- `test_library_performance*`、旧 benchmark 主要测 SQL/cache、旧 QWidget 资产库与合成数据，不是当前 WebEngine 连续绘制。
- `test_web_library` 的 5,000 行用例只断言可见节点数量上限。节点少于 100，不代表每秒销毁上千节点的成本低。
- 新增的同范围检查只测 scrollTop 1→2 的节点/焦点保留。跨范围则只检查数量，没有检查重叠卡片、已解码 IMG、键盘顺序的连续性。
- 过去没有同条件帧尾、DOM churn、Chromium 累计 Script/Layout、消息体积与桌面录像的发布证据，静态截图无法揭示帧重复。

本轮用确定性的真实 Qt 节点/IMG/焦点连续性测试防止恢复全量替换，并保留正式读库探针和原始指标。性能分位数由专用硬件复测；不把采集完成的退出码 0 当作全场景性能 PASS。

## 最小修改与文件

- `frontend/web/app.js`：依据原 token 查找当前宫格节点；只删除离开范围的项、创建新进入的项。重叠项保留 article 与 IMG；只在几何变化时更新原有定位，更新原有勾选/当前状态；插入顺序保持视觉顺序与 Tab 顺序。缓存仅为本次可见节点 Map，没有增加长期隐藏 DOM 池。`reflow(anchor)` 先恢复原位置再 render，去掉三个调用点的中间宫格绘制。卡片 HTML 和封面内容策略不变。
- `tests/test_web_render_stability.py`：真实 Qt 隔离资源，覆盖跨行/反向滚动、解码图片连续性、焦点、多选、密度、筛选、快照身份更换和无重复节点。合成 200 行只用于确定性测试，不作为真实资产性能证据。
- `tools/web_performance_probe.py`：正式入口只读取证、Qt 计时、Bridge 请求/确认字节及耗时、进程采样、CDP metrics/Trace/GPU 信息和可选 CPU profile；支持冻结原 app.js 的同入口 A/B，不 checkout/reset 工作区。
- `tools/web_performance_scenario.js`：固定只读操作序列、rAF/Long Task、DOM 创建删除、函数同步耗时、IMG load、输入到两次 rAF 的反馈及实际视口/布局数据。
- `tools/web_performance_summary.py`：按场景输出中位数/P95/P99、>50/>100ms、DOM/调用计数、GUI 计时、资源和桥接分布。分位数取排序后 floor((n−1)×p) 项。同步函数计时有嵌套，不能简单相加当作纯 ScriptDuration；Chromium 累计时间另列。
- 本报告。其他生产数据工作区差异保留，不纳入提交。

## 硬件、窗口和测量限制

Windows 11 家庭版 build 26200；i5-14600KF（14 核/20 线程）；约 32GiB RAM；RTX 3060，驱动 32.0.16.1714；显示器 1920×1080、60Hz，桌面 DPR 1.25。Python 3.11.9、PySide6/Qt 6.11.2、Chromium 140、V8 14.0.365.10。`hardware.json` 及各次结果中的 `gpu`/`cdp` 保存原值。

最终 A/B 实际 DOM 视口为 1728×900 和 1180×760，DPR 1.25；`javascript.samples[].viewport`、columns/width/cell 可核对，不用请求窗口尺寸冒充实际尺寸。标准视口物理大小超出本机显示器，持续桌面录制采用完全可见的小窗口，1474×986 裁切范围来自同一 SMM 原生窗口内部，60fps Desktop Duplication；未以放大图片模拟大屏。

初轮探针发现并保留的自身问题：QWebSocket 构造签名、与产品 `frame` 命名冲突、Python 只读规则拒绝 urllib 网络调用；相应运行不算产品失败。正式 CDP 改用 Qt 原生 localhost 通道，没有修改生产只读规则。缩放探针最初在 null 状态过早继续，结束后已排队的缩放回调再显示窗口；已修正为等待明确 false，产品关闭逻辑未动。最终 `verified-*` 正常自动退出。

早期资源采样放在 GUI QTimer 内，可能污染 GUI 延迟。最终探针移到诊断线程，并记录 sampler_ms 与 GUI watchdog。旧 10 分钟结果的 7 次 >100ms GUI 回调间隔**不能直接归因于产品主线程阻塞**；它们既不是 Chromium 长任务，也没有对应栈证据。Qt 20ms Python 回调计时还受 GIL/系统调度影响，不能等同 C++ 事件循环完全停止。独立复核结果见补充索引。

## 第一处修复的同条件 A/B（标准视口，各两轮）

数据为 `verified-before-standard` / `verified-after-standard`，原始结果、source hashes、逐场景分布均保存。两轮操作相同，耗时分别 38.05 / 37.15 秒；不同帧数是实际帧率差异，不强制对齐帧样本数。

| 场景 / 指标 | 修改前 | 修改后 |
| --- | --- | --- |
| Stellaris 快滚帧间隔 median / P95 / P99 ms | 33.3 / 33.4 / 33.5 | 16.7 / 16.7 / 16.8 |
| 同场景 >50 / >100ms 帧 | 0 / 0 | 0 / 0 |
| 同场景创建卡片 / 同步渲染计时 ms | 7,207 / 2,137.0 | 980 / 284.6 |
| Wallpaper Engine 快滚 median / P95 / P99 ms | 33.3 / 33.4 / 33.4 | 16.7 / 16.7 / 16.8 |
| 同场景 >50 / >100ms 帧 | 0 / 0 | 0 / 0 |
| 同场景创建卡片 / 同步渲染计时 ms | 7,246 / 1,681.9 | 1,014 / 166.2 |
| Stellaris 普通滚动 P95 / P99 ms | 33.4 / 33.4 | 16.8 / 16.8 |
| 连选输入到双 rAF反馈 median / P95 ms（12 次） | 38.2 / 49.8 | 25.1 / 32.9 |
| Chromium ScriptDuration / LayoutDuration 秒 | 7.130 / 2.283 | 1.248 / 0.856 |
| Chromium RecalcStyleDuration / TaskDuration 秒 | 1.782 / 18.459 | 0.490 / 8.134 |
| LayoutCount / RecalcStyleCount | 1,198 / 1,294 | 1,404 / 1,502 |
| Qt GUI 回调间隔 P95 / P99 / max ms | 22.4 / 25.5 / 39.4 | 20.8 / 25.2 / 49.8 |
| 搜索组合 Chromium 长任务 | 2 次，61–67ms | 1 次，53ms |
| renderer CPU 中位数（100%=一逻辑核） | 68.0% | 42.2% |
| 主进程 / renderer RSS peak MiB | 478.6 / 286.7 | 445.0 / 337.7 |

更新次数可略增（更多真实绘制帧），耗时下降才是关键。没有以“>50ms 为 0”掩盖原先 33ms 的持续掉帧。资源数据包含诊断开销及主进程内 GPU；renderer 峰值内存增加，**不宣称全局内存下降**。独立的初轮三轮对比同样复现标准快滚 33.3→16.7ms；见 `before-standard-cdp` / `after-standard-1`，不混用它们的原始计数。

小窗口两轮（两边均开启 Trace 和相同桌面录制）：Stellaris 快滚 P95/P99 **33.3/33.4→16.7/16.8ms**；Wallpaper Engine **16.8/33.4→16.7/16.8ms**；Stellaris 新建卡片 **5,152→576**。输入反馈 P95 **66.8→32.8ms**。逐场景原始分布在 `verified-*-small-summary.json`，Trace 保存真实 Paint/Layout/Decode Image 事件，不以重复 IMG load 等同解码。

## 第一处修复的十分钟混合验证

`after-mixed-10min/results.json`：**600.132 秒、34 轮**，含普通/快速滚动、连选、切游戏、搜索/筛选、密度、部署滚动/详情、快照详情、窗口缩放/最大化/恢复。JS 错误和 console 均为空、业务命令 0。

- 两个真实库快速滚动各 6,222 个 rAF 间隔，P99 均 16.8ms，>50/>100ms 均 0；不能据此宣称整程稳定 60FPS。
- 搜索/筛选组合 34 次长任务，51–61ms；其帧 P95/P99 为 50/50ms，3 帧略超过 50ms。组合不是单个按键的输入延迟，仍需保留余量。
- 主进程 RSS peak 588.3MiB、renderer 347.7MiB，最后采样分别约 568.1/301.0MiB；诊断也保留原始数组。10 分钟不足以排除长期内存增长。
- 旧 GUI 采样受观测开销污染，见前述限制。不能将其直接写成已确认产品阻塞，也不能删掉旧结果。
- 正式库仅一份部署快照，因此正式序列是重复打开现有快照及成员；不同快照切换由本轮回归中的两份隔离快照覆盖，没有创建生产快照。生产排序/投影没有执行。
- 原位详情和工作台保持原样；真正的扫描线撕裂、旧帧残留/错图没有可证明的触发闭环，仍 **NOT CLOSED**。录制未覆盖所有用户硬件/窗口状态。

## 状态

- **PERFORMANCE ROOT CAUSE CONFIRMED**：跨范围与连选的无意义 DOM/IMG 重建；其他残余卡顿归因未全部关闭。
- **RENDERING ROOT CAUSE NOT CLOSED**：节点失效已证明，用户全部撕扯/残影未复现归因。
- **CODE FIXED**：现有虚拟宫格的最小节点连续性修复。
- **FORMAL QT PERFORMANCE NOT CLOSED**：搜索长任务已在最终验证消失；仍有少量 rAF 长帧、GUI 回调延迟及一次诊断运行渲染进程崩溃未闭环。
- **VISUAL STABILITY NOT CLOSED**：自动视觉回归/录像属于证据，不代替用户真实症状闭环或视觉认可。
- **PRODUCTION DATA SAFETY PASS**：12 张非内部表逐行及结构不变，9 张固定原图哈希不变，7 份正式运行记录业务命令均为 0；范围及限制见下方。

## 最终回归、数据安全与证据入口

新增连续性测试用冻结旧 `app.js` 从同一测试入口复跑，准确失败于 `keptCard.isConnected && document.activeElement===keptCard`，不是导入或环境失败（`baseline-test.txt`）；只含第一处修复的冻结版本则准确失败于 `renderCount===1`（最终目录 `baseline-anchor-restored.txt`）。最终测试还覆盖原位重排后焦点恢复。当前完整实现通过：六个相关测试文件 **33 passed / 87.79s**（最终目录 `regression-release.txt`）。第一处修复时为 33 passed / 67.98s，第二处修复首次重跑为 33 passed / 89.01s；后一次包含 Windows `0x8001010d` 日志，但进程最终通过，不能把日志直接等同未处理 Python 异常或声称日志全空。期间旧 `--basetemp` 被并行新增规则拒绝的调用不计测试通过；最终按 `_tmp/tests/` 规则执行。未改动其他 Python 业务文件，不扩大声明为全部项目测试通过。

封面正式只读探针在 **1728×900、1180×760 × DPR 1.25、1.0** 四组运行，各 10 个真实案例：`automatic_pass=true`，console 空；第一处修复对应 `cover-*`，第二处修复后对应 `final-cover-correct-*`。原图哈希、可见源区域、原生绘制像素、Inspector 资源一致性、占位和滚动返回检查通过；既有人工基线未提升，探针保留 `USER VISUAL REVIEW PENDING`（result_exit=2）。这组结果证明布局/内容策略未倒退，不代替快速滚动的动态闪烁评审。

前后标准视口的 Stellaris 普通/快速滚动样本均为 **7 列、卡宽 199 CSS px、cell 185 CSS px**；打开 Inspector 后均为 **5 列、卡宽 214、cell 193**。没有调整 cover 高度计算、标题或 CSS。其他阶段和小窗口几何值见各自 `javascript.samples`。

进程采样移出 GUI 后另做同条件各三轮复核（`observer-before` / `observer-after`）：20ms GUI 回调间隔 P95/P99/max **29.19/30.07/52.28 → 24.51/31.33/49.10ms**；>50ms 次数 **1→0**、>100ms 均 0。后台资源采样中位数约 **5.15→5.03ms**、最大 **9.62→9.08ms**；两边 ≥60ms GUI watchdog 栈捕获均 0。它没有复现旧十分钟里的 >100ms 回调间隔，也不足以把旧异常唯一归因于诊断采样。

最终目录 `production-safety.json` 将最初有效正式基线 `before-standard-cdp/before.db` 与最终生产库以只读连接比较：**结构相同、12 张非内部表所有行相同、integrity_check=ok**；固定的 **9 张源封面 SHA-256 相同**。历史目录中 22 份含 commands 的有效 JSON 记录均为业务命令 0（包含未通过诊断，不等同 22 份验收通过），最终运行单独列入索引。迁移前保存的 7 份原始运行哈希在恢复目录复核一致。生产写保护仍由正式只读入口执行，没有修改其规则。没有全量扫描游戏文件/归档字节；不把本次检查扩大为整个磁盘的逐文件无变化证明。数据库、mapped_games.json 和并行临时目录治理工作区差异保留并排除本次提交。

证据文件（历史目录见开头，索引使用迁移后的实际路径）：

- 最终目录 `acceptance-index.json`：有效运行、摘要、回归、数据安全及提交索引；各运行 `results.json` 保存原始数据和源码哈希。
- `verified-before-standard-summary.json` / `verified-after-standard-summary.json`：标准窗口逐场景全部分布；`verified-*-small-summary.json` 为小窗口，`trace.jsonl.gz` 保留 Chromium 原始事件。
- `verified-before-desktop.mp4` / `verified-after-desktop.mp4`：相同小窗口、60fps Desktop Duplication，各 14 秒，含实际快速滚动；视频时段存在运行相位差，不把压缩帧差直接解释成真实呈现帧率。
- `verified-before-video-frame.png` / `verified-after-video-frame.png`：上述原生录像第 9 秒帧，供布局对照；不是动态稳定性证明。
- `after-mixed-summary.json` / `after-mixed-10min/results.json`：600 秒原始混合验证，保留早期 GUI 观测限制。
- `cover-{125,100}-{standard,small}/results.json` 及同目录截图：两视口、两 DPR 的正式封面回归证据。

## 继续开发后的最终结果与异常保留

`residual-cpu-profile` / `anchor-after-profile` 为第二处修复前后各三轮、相同 CPU profiler 条件。前者搜索长任务 56/66ms，后者 0；该场景创建卡片 **529→372**。旧长任务窗口的约 119ms CPU 采样中，renderGrid 约 62.6ms、restoreAnchor 约 10.9ms；采样不是精确函数计时，父子不能相加。冻结 `before-anchor-render.js` 的 SHA-256 与修复前运行源码记录一致。

最终 `final-standard`（1728×900，DPR 1.25，两轮）：Stellaris 快滚 median/P95/P99 **16.7/16.8/33.3ms**、max 83.4ms、>50/>100 为 **1/0**；Wallpaper 快滚 **16.7/16.8/16.8ms**、max 33.3ms、**0/0**。两库卡片创建分别为 **980/1,014**。Chromium Script/Layout/RecalcStyle/Task 累计 **1.220/0.778/0.486/8.332s**；搜索长任务 0。后续结果存在环境波动，不用第一轮更漂亮的 16.8ms P99 覆盖它。主进程/renderer RSS peak 约 **459.2/352.2MiB**，CPU 中位数约 **150.0/55.4%**；主进程包含四路预览 Worker 与 GPU，不能仅凭 renderer 降低宣称整体 CPU 都降低。

最终 `final-mixed-10min`：**601.346 秒、31 轮、Chromium ≥50ms 长任务 0、JS/console 错误 0、渲染进程终止 0、业务命令 0**。Stellaris 快滚 5,564 个间隔，P95/P99/max **16.8/33.3/66.6ms**，>50/>100 **1/0**；Wallpaper 快滚 5,599 个间隔，**16.8/33.3/50.0ms**，**0/0**。搜索组合 P95/P99 **33.3/33.4ms**，不再出现上一版本每轮一次的长任务。主进程/renderer RSS peak 约 **607.5/355.3MiB**，结束约 **580.7/310.3MiB**，含持续保留的诊断数组，不能据此排除长期增长。

同一最终十分钟的 Qt 20ms 回调间隔 P95/P99/max **31.47/38.33/74.74ms**，55 次 >50ms、0 次 >100ms。watchdog 有 11 个操作期间样本（9 个位于 `acknowledge` 的 RLock 入口、2 个在 Qt 事件循环）和 1 个结束导出样本。最后一个是探针 `json.loads` 的约 185ms，明确属于测量导出，不算用户操作阻塞。操作期间的 Python 栈位置尚不能区分真实锁等待、GIL 和调度；不据此修改桥接锁语义，补充独立确认回调计时留证。

最终明确传入 1180×760 的 `final-before-small-correct` / `final-after-small-correct`，两边均原 Trace 类别、各两轮：Stellaris 快滚 P95/P99 **50.0/50.1→16.7/16.8ms**，卡片创建 **4,196→576**；Wallpaper 快滚 P95/P99 **50.0/50.0→16.8/33.2ms**。但修改后 Stellaris 出现 **1 次 150ms 帧间隔**，同期 Qt 回调 max 154.8ms；没有 Chromium ≥50ms 长任务或 renderer termination，不能以良好的 P99 掩盖它。确认回调本体 P95/P99 约 **0.008/0.010ms**、max **39.72ms**，请求入口 max 29.81ms，不足以唯一解释该 150ms 事件，仍需保留渲染/调度调查。

续测异常和无效配置全部保留：

- `residual-search-trace` 在新增 `toplevel/blink.user_timing` 追踪类别时收到 `Inspector.targetCrashed`、renderer CrashedTerminationStatus **-1073741819**，窗口白屏；另有 `0x8001010d` 日志。正常关闭本轮窗口后，恢复原类别的 `residual-control` 三轮正常完成。**时间相关不能证明类别就是根因**，该崩溃的产品/Qt/驱动归因仍 NOT CLOSED；没有关闭 GPU 或改变部署/注册合同。
- 名为 `final-small` 的续测漏传 `--viewport`，实际仍为 1728×900；它保留为标准窗口 Trace 证据，不计为小窗口验证。第一处修复的 `verified-*-small` 仍是真正 1180×760 的配对录像/Trace；最终再以明确实际尺寸运行小窗口。
- `final-cover-*` 首轮把目标 DPR 当作 Qt 的乘数，实际为 1.5625/1.25，门禁正确拒绝，不能算通过。正确乘数应为原生 1.25 × **1.0/0.8**；最终结果使用 `final-cover-correct-*`，不修改系统 DPI。
- 并行目录治理迁移时，`release-smoke/results.json` 留为空文件，虽有正常结束日志，也不算完整验收证据。最终在新规则目录重新运行 `release-canonical`，与当前 `app.js` 哈希一致：真实 1180×760、DPR 1.25、37.148 秒两轮，两库快滚 P95/P99/max 均约 **16.7/16.8/16.8ms**、>50/>100 均 0；搜索组合长任务 0、JS/console 错误 0、renderer termination 0、业务命令 0。该短程重现不能抹去前述 150ms 事件。

交付索引为最终目录 `acceptance-index.json`：16 份完成运行的原始路径/哈希、逐场景摘要、40 个最终封面案例、33 项测试、生产安全与最终提交。早期运行缺少实际 viewport 字段时索引留空，不用请求尺寸冒充测量。既有原生录像为第一处修复的配对证据；最终截图还包括 `final-cover-correct-*` 的真实 Qt 捕获。待调查数据和人工视觉评审继续保留，不自动清除恢复目录。

**整体 NOT CLOSED**：两处冗余渲染修复及搜索改善已有正式重复证据；少量长帧、GUI 延迟、补充诊断中的 renderer 崩溃归因、用户快速滚动闪烁最终确认及更长期稳定性仍未关闭。没有扩大改动到未定位的 GPU/驱动或其他业务。
