# SMM P1 视觉质感与交互验收

状态：**PASS（本报告记录的实现与 Qt 验收范围）**。最终视觉取舍交由用户评审；不代表整个项目或所有 DPI 的发布认证。

## 实际改动

- `frontend/web/style.css`：框架、画布、卡片、Inspector 与浮层的表面明度分层；克制的边缘高光与阴影；统一 Hover、Pressed、Focus、Disabled；150ms 过渡与 `prefers-reduced-motion`。
- `frontend/web/app.js`：Inspector 未设置类型时明确显示“Mod 类型 / 设置类型”，仍调用原类型选择入口；滚动可见范围不变时跳过整批卡片重建，保留现有焦点、Hover 与封面绘制状态。尺寸算法、虚拟化范围、QWebChannel 合同不变。
- `tests/test_web_library.py`：菜单验证共享浮层表面与投影；验证同范围滚动保留实际聚焦节点。继续覆盖菜单键盘操作、详情、5,000 行虚拟化、搜索及浏览锚点。
- 本报告。上一轮主要视觉改动已经包含在 `7a7b31e`，本次在其上补齐 Token、滚动去重与验收。

没有修改 Worker、Service、DAL、SQLite 结构、部署、归档或权限规则。工作区已有的数据库和 `mapped_games.json` 修改不属于本次源码改动，没有回退它们。

## 表面与控件

沿用原 Token，画布 `--bg=#151c20`、框架 `--nav=#121e22`、卡片 `--surface=#222e34`、Inspector `--panel=#1e292f`、输入 `--input=#182329`、浮层 `--floating=#28363d`。强调色保持 `#70c9b2`；新增共享 `--card-hover`、`--error`，避免继续散落颜色常量。

卡片不抬升、不缩放；Hover 只改变表面、边缘和小范围阴影。当前对象采用固定 1px 几何边界加内侧强调，避免原 2px 选中边界挤压封面和文字。多选维持复选框语义，没有恢复整卡绿色填充。标题保持单行 ellipsis，收藏仍仅在封面右上角。

Inspector 名称与核心状态优先；Workspace ID 降低视觉权重；描述不再被 12 行截断，仍由原滚动区域承载；底部按钮固定可达，宽度仍为 330px。类型与用户标签分别显示，小封面没有星标。

## 真实 Qt 证据与方法

所有下列 PNG 均由实际 `WebLibraryWindow` / Qt WebEngine 的 `QWidget.grab()` 获取，加载现有生产资产；没有生成演示 Mod 或替换资源内容。采集进程调用正式 `main.main([])`，走正常启动、生产资源装配和单实例边界。窗口固定为 1728×900 **逻辑像素**，DPR 1.25，PNG 为 2160×1125 **物理像素**；窗口超过当前桌面可见范围的部分由 Qt 原生抓图完整保留。

“修改前”使用本聊天视觉增量的逆向样式回放：只在验收进程读取前端文件时注入旧样式、旧类型入口文案和滚动行为，没有回退项目源码、数据或业务。它是同一批当前真实资产的受控 A/B 回放，不能误称为改动前时间点的现场截图。原始现场截图另外保留为 `before-existing-qt.png`。

- 整体、普通与当前卡片：[修改前回放](D:/smm-ui-p1-evidence/before-replay-library.png)、[修改后](D:/smm-ui-p1-evidence/after-library.png)。Anno 1800，106 个现有 Mod。
- 原生鼠标 Hover：[修改前回放](D:/smm-ui-p1-evidence/before-replay-hover.png)、[修改后](D:/smm-ui-p1-evidence/after-hover.png)。两次均实测 `.card:hover` 数量为 1。
- 真实无封面资产：[修改前回放](D:/smm-ui-p1-evidence/before-replay-no-cover.png)、[修改后](D:/smm-ui-p1-evidence/after-no-cover.png)。来自已有 1,114 项的 Wallpaper Engine 库，搜索 `Mod_3739368228`；后端封面读取结果为空，未伪造占位对象。
- Inspector 换行名称与描述：[修改前回放](D:/smm-ui-p1-evidence/before-replay-long-name.png)、[修改后](D:/smm-ui-p1-evidence/after-long-name.png)。标题保持完整，底部操作区下边缘约为逻辑 y=867.2，小于视口高度 900。
- 搜索：[截图](D:/smm-ui-p1-evidence/after-search.png)；输入焦点：[截图](D:/smm-ui-p1-evidence/after-search-focus.png)；筛选浮层：[截图](D:/smm-ui-p1-evidence/after-filters.png)；游戏下拉：[截图](D:/smm-ui-p1-evidence/after-game-menu.png)；完整详情：[截图](D:/smm-ui-p1-evidence/after-detail.png)。

## 密度与滚动

前后尺寸自动比对一致：标准密度 5 列，卡片 214×218，封面高度 120；紧凑密度 6 列，卡片 176×197，封面高度 99。Inspector 330px，标题 nowrap / ellipsis。Hover 卡片仍为 214×218。所有尺寸单位均为逻辑像素，没有用增加卡片高度换取质感。

相同真实库、相同前台窗口、45 次滚动步进，使用实际 scroll 事件触发现有绘制路径；各取 44 个 requestAnimationFrame 间隔。修改前中位数约 33.3ms、P95 50ms；修改后中位数约 16.7ms、P95 33.4ms。两者最多渲染 30 张卡片。原始时间序列见 [修改前 JSON](D:/smm-ui-p1-evidence/before-replay.json)、[修改后 JSON](D:/smm-ui-p1-evidence/after.json) 和 [自动比较](D:/smm-ui-p1-evidence/comparison.json)。

这是单机短样本，不是全程 60fps 保证；修改后仍记录到一个约 66.7ms 的长帧。早先后台窗口、重复主动调用绘制的探针不用于此结论。

## 验证结果与范围

- 静态检查：`node --check frontend/web/app.js` 通过；按仓库 CRLF 设置检查本次差异，无空白错误。
- 浏览器预览：没有使用普通浏览器预览替代 Qt 验收。
- Qt 隔离交互测试：**28 passed**，包含资产库、键盘菜单、搜索、详情、滚动焦点保持、5,000 行压力检查、收藏写回、编辑取消与保存、类型与用户标签分离。测试中的写入仅落在临时库；[完整结果](D:/smm-ui-p1-evidence/final-tests.txt)。
- Qt 正式资源冒烟：真实加载、搜索由 106 项缩到 1 项、筛选与游戏菜单打开、当前对象选择、完整详情打开及返回、密度切换、原生 Hover 均完成。
- 另直接执行未加采集包装的 `python main.py`：正常显示暗黑地牢现有 550 个 Mod、57 个已部署成员；原生窗口可访问性读取确认页面和卡片正常加载。验收结束后保留此正式窗口供用户检查。
- JavaScript：Qt 控制台未记录错误；额外的真实启动错误观察器没有捕获 `error` 或 `unhandledrejection`，Qt 已解析减少动态效果的 CSS 规则；[结果](D:/smm-ui-p1-evidence/js-proof.json)。没有切换系统动态效果偏好，因此不把规则解析表述为系统偏好切换验收。
- 每次采集进程运行前后生产 SQLite SHA-256 一致。生产采集没有执行收藏、部署、编辑保存或归档命令；收藏与写回通过隔离测试验证。正常启动仍会产生原有日志、锁和界面偏好；验收脚本恢复了触及的界面偏好。

## 已知限制与复现资料

仅验证当前 Windows / Qt WebEngine、DPR 1.25；没有扩展至其他 DPI、所有游戏或长时间连续性能测试。偶发长帧仍存在，未将性能采样扩大为没有证据支持的全面结论。

证据目录：`D:\smm-ui-p1-evidence`。保留 Qt 采集脚本、旧样式逆向回放脚本、当前前端源文件快照及 SHA-256，便于核对。脚本只用于本地验收，没有加入正式启动或业务流程。
