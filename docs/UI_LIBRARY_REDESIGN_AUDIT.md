# Mod Library UI 信息架构与主题审计报告

> 审计范围：`ui/library_view.py`、`ui/mod_card.py`、`ui/mod_detail_panel.py`、`ui/styles.py`、`ui/game_deploy_view.py`、`ui/mod_import_dialog.py`，以及 `ui/` 下相关 theme / stylesheet。  
> 审计性质：只读架构审计；本文档为 redesign 计划，不修改业务逻辑。  
> 日期：2026-08-08

---

## 1. 当前 UI 架构

### 1.1 应用壳层（上下文）

`MainWindow`（`ui/main_window.py`）应用全局 `APP_STYLE`（`ui/styles.py`），左侧 `QListWidget#navList` 导航：

- 同步中心
- **Mod 库** → `ModLibraryView`
- 游戏部署 → `GameDeployView`

---

### 1.2 Mod Card（`ModCardWidget` / `QFrame#modCard`）

固定宽度 `CARD_WIDTH = 200`，固定高度由 6 段内容计算。

```
ModCardWidget (QFrame#modCard)
├── cover_label (QLabel) — 封面 180×112
│   ├── tag_badge (QLabel#modTagBadge) — 左上覆盖：Conflict / Invalid / Disabled（互斥优先）
│   ├── platform_badge (QLabel#modPlatformBadge) — 右上覆盖：Steam / Nexus / GitHub
│   └── relation_badge (QLabel#modRelationBadge) — 左下覆盖：⚠ conflicts · ↑ deps
├── title_label — 显示名（最多 2 行，可带 ★ 收藏前缀）
├── steam_label — 「Steam: {steam_name}」（始终占位一行）
├── meta_label — 「Workshop ID: {mod_id}」（始终占位一行）
├── offline_label / status_label — 「离线页已同步」/「离线页未同步」
└── deploy_badge (QLabel#deployBadge) — 「已部署」/「未部署」/「部署失败」
```

**右键菜单（`QMenu`）**：查看详情 · 编辑信息 · 部署 · 打开目录 · 打开 Steam 页面 · 收藏/取消收藏。

**问题摘要**：卡片承载了 ID、Steam 原名、离线文案、部署徽章、关系计数等过多信息；平台徽章与状态徽章并存，但 `steam_label` / `meta_label` 仍用 Steam 语义命名，对 Nexus/GitHub 不友好。

---

### 1.3 Mod Detail Panel（`ModDetailPanel` / `QWidget#modDetailPanel`）

`QStackedWidget`：Empty / View / Edit。View 为「可滚动 body + 固定 footer」。

#### Empty（`QFrame#detailPanelInner`）

```
detailEmptyHint: 「选择一个 Mod\n在右侧查看详情」
```

#### View 页完整分区树

```
_view_page (QWidget)
├── _view_scroll (QScrollArea)
│   └── body (QFrame#detailPanelInner)
│       ├── [Header] QFrame#detailSection
│       │   ├── cover_label + btn_change_cover「更换封面」
│       │   └── 「概览」(detailPanelSection) + view_title (detailPanelTitle)
│       │
│       ├── [基本信息] QFrame#detailSection
│       │   ├── view_name_caption + view_steam + btn_copy_name「复制」
│       │   ├── view_id_caption + view_id + btn_copy_id「复制」
│       │   ├── view_platform（平台：…）
│       │   ├── view_source_caption「来源：」+ view_source_url + btn_copy_source_url「复制链接」
│       │   ├── view_external_id（hidden，兼容测试）
│       │   ├── 字段「Mod Files」+ mod_files_host（多文件 Checkbox 列表）
│       │   └── view_game（游戏：… · AppID …）
│       │
│       ├── [状态] QFrame#detailSection          ← 仅离线
│       │   └── view_offline（离线状态 / Provider / 更新时间）
│       │
│       ├── [部署状态] QFrame#detailSection
│       │   ├── view_deploy（状态：已部署/未部署/部署失败）
│       │   ├── view_deploy_time
│       │   ├── view_deploy_path
│       │   ├── view_deploy_type
│       │   ├── view_deploy_error（红字）
│       │   ├── view_deploy_audit（一致性提示）
│       │   └── view_deploy_conflict（冲突提示）
│       │
│       ├── [用户元数据] QFrame#detailSection
│       │   ├── view_favorite
│       │   ├── 「自定义介绍」+ view_custom_desc
│       │   └── 「备注」+ view_notes
│       │
│       ├── [状态] QFrame#detailSection          ← 与上方「状态」重名！
│       │   ├── status_run_label「运行状态」
│       │   ├── status_enabled_label「Enabled」
│       │   ├── status_invalid_label「失效」
│       │   ├── status_conflict_label「冲突」
│       │   ├── status_version_label「Version」
│       │   ├── status_installed_label「Installed」
│       │   ├── status_update_label「Status」
│       │   ├── 「原因」+ status_reason_edit
│       │   ├── status_check_time_label「最后检测」
│       │   ├── Enable / Disable Mod / 标记失效 / 标记正常 / 标记冲突 / 清除冲突
│       │   ├── btn_view_conflicts「冲突详情」(checkable)
│       │   └── status_conflict_detail（默认隐藏）
│       │
│       ├── [Tags] QFrame#detailSection
│       │   ├── category_tags_label
│       │   └── category_tag_edit + 「+ Add Tag」+ 「Remove Tag」
│       │
│       ├── [Relationships] QFrame#detailSection
│       │   ├── Dependencies / Conflicts / Addons / Patches
│       │   └── 各一组 QListWidget + Add / Remove
│       │
│       └── [用户标记] QFrame#detailSection
│           ├── tag_invalid_check「已失效（标签）」
│           ├── tag_invalid_reason
│           ├── tag_conflict_check「存在冲突（关联）」
│           ├── tag_conflict_list（勾选其它 Mod）
│           ├── btn_save_tags「保存标记」
│           └── view_tag_deploy_hint
│
└── _view_footer (QFrame#detailFooter)
    ├── 「操作」(detailPanelSection)
    ├── row1: 打开目录 · 原链接 · 保存/导入离线页面 · 打开离线页面
    ├── row2: Deploy · 重新部署 · 取消部署
    ├── row4: 复制链接 · 复制全部信息
    └── row3: 编辑 · Remove Mod
```

#### Edit 页

```
「编辑 Mod 信息」
├── edit_hint（Mod ID / Steam 原名）
├── 显示名称 (edit_display_name)
├── 自定义介绍 (edit_custom_desc QTextEdit)
├── 备注 (edit_notes QTextEdit)
├── 收藏 (edit_favorite QCheckBox)
└── footer: 取消 · 保存
```

**问题摘要**：两个同名「状态」分区；失效/冲突在「生命周期状态」与「用户标记」双轨；Version 混在状态区；Actions 虽固定底栏但按钮过多（4 行）。

---

### 1.4 Library View（`ModLibraryView`）

三栏 `QSplitter`：

```
ModLibraryView (QWidget)
├── Header: 「Mod 库」+ count_label + 「导入 Mod」(#browseButton) + 「刷新」
├── deploy_audit_banner (subtitleLabel，警告色，可隐藏)
├── path_hint「库路径：…」
└── QSplitter
    ├── Left: QFrame#controlPanel
    │   ├── 「按游戏筛选」
    │   └── game_list (QListWidget#gameList) — 「全部游戏 · N」/「{game} · N」
    ├── Center
    │   ├── search_box (#librarySearchBox)
    │   ├── Status chips (#libraryFilterChip): 全部 / 收藏 / 已部署 / 失效 / 冲突 / 已禁用 / 离线页面缺失
    │   ├── Platform chips: 全部平台 / Steam / Nexus / GitHub
    │   ├── 「标签」category_combo (#librarySortCombo) + 「排序」sort_combo
    │   ├── scroll (QScrollArea) → library_host + FlowLayout(ModCardWidget…)
    │   ├── empty_overlay (#libraryEmptyOverlay)
    │   └── loading_overlay (#libraryLoadingOverlay)
    └── Right: ModDetailPanel（单例，生命周期随页面）
```

---

### 1.5 Game Deploy View（相关部分）

```
GameDeployView
└── QFrame#controlPanel
    ├── 「游戏部署设置」
    ├── 说明 subtitleLabel
    ├── 「选择游戏」+ game_combo (#deployGameCombo) + 「刷新列表」
    ├── 「游戏名称」name_edit
    ├── 「Steam AppID」app_id_edit
    ├── 「游戏安装目录」install_edit + 浏览
    ├── 「Mod 部署目录」mod_path_edit + 浏览
    ├── 「部署类型」deploy_type_combo (#deployTypeCombo)
    ├── 「保存配置」(#syncButton) + 「测试路径」
    └── status_label (#statusLabel)
```

与 Library 的关系：Detail 部署动作依赖此处配置的游戏级路径；不执行部署本身。

---

### 1.6 Import Dialog（相关部分）

```
ModImportDialog (QDialog#modImportDialog)
├── 「目标游戏：」+ game_context_label
├── 说明 hint
├── Platform radios: Steam Workshop / Nexus Mods / GitHub
├── QStackedWidget
│   ├── Steam: Workshop ID / 显示名称 / 本地目录
│   ├── Nexus: URL·ID / 显示名称 / 导入方式 / 本地目录|压缩包
│   └── GitHub: URL / 显示名称 / 导入方式 / 本地目录|压缩包
├── 「展示图片（可选）」
├── 「离线页面（可选）」(Nexus only) + 「优化离线页面布局」
├── status_label
└── OK「导入」/ Cancel
```

主题：依赖 `QDialog { background-color: #121820 }`；无专用 objectName 样式；`QRadioButton` / `QCheckBox` / `QFormLayout` 行标签未统一 dark token。

---

## 2. 信息重复问题

| Information | Current locations | Suggested location | Reason |
|---|---|---|---|
| **显示名称 (Name)** | Card `title_label`；Detail Header `view_title`；Detail「基本信息」`view_steam`；Edit `edit_display_name`；搜索索引 | Card + Detail Header；Edit 仅编辑入口 | 浏览扫一眼 / 详情确认即可；基本信息里再列一次名称冗余 |
| **Steam 原名** | Card `steam_label`（始终显示）；Detail tip；Edit hint | Detail Metadata（折叠）或仅当 display≠steam 时显示副标题 | Card 不应固定占一行；非 Steam 平台字段名误导 |
| **Platform** | Card `platform_badge`；Detail `view_platform`；Library platform chips；Import radios | Card badge + Detail Header 一行徽章；筛选保留 chips | 平台是扫视关键，详情用徽章即可，不必长文案重复 |
| **External / Workshop ID** | Card `meta_label`；Detail `view_id` + 复制；tooltip；Edit hint | **仅 Detail「基本信息/Metadata」** | 低频查阅；Card 禁止展示 |
| **Source URL** | Detail `view_source_url` + 复制；Footer「原链接」「复制链接」 | Detail Metadata + Footer 一个「打开来源」 | 链接不属于 Card；Footer 与行内复制功能重复 |
| **Deploy status** | Card `deploy_badge`；Detail「部署状态」整段；Library filter「已部署」；audit banner | Card 轻量 badge（或合并进状态槽）；Detail Status 高优区展开路径/时间/错误 | 状态扫视在 Card；细节只在 Panel |
| **Offline** | Card `offline_label` 全文；Detail 第一个「状态」；filter「离线页面缺失」；Footer 离线按钮 | Card：仅缺失时小 badge（可选）；Detail Status；操作在 Footer | Card 全文「离线页已同步」占高且与成功态视觉噪音大 |
| **Conflict** | Card `tag_badge` + `relation_badge`；Detail 生命周期「冲突」；`status_conflict_detail`；「用户标记」冲突勾选；Relationships Conflicts；`view_deploy_conflict`；filter「冲突」 | Card：单一高优 Conflict badge；Detail Status 聚合（检测冲突 + 关系冲突）；用户声明关系进 Relationships | 三套冲突模型并存（status / tag / relationship）造成 IA 混乱 |
| **Invalid** | Card `tag_badge`；Detail `status_invalid_label` + 按钮；「用户标记」`tag_invalid_check`；filter | Card badge；Detail Status 单一真相源；废弃或合并「用户标记」失效轨 | 双轨标记易不一致 |
| **Enabled / Disabled** | Card `tag_badge` Disabled；Detail `status_enabled_label` + Enable/Disable；filter「已禁用」 | Card badge；Detail Status 顶部 | 与 Invalid/Conflict 同属核心状态 |
| **Version / Installed / Update Status** | Detail 第二个「状态」内三条 Version 行 | Detail **Version** 独立区（可折叠） | 非扫视信息，不应夹在 Enable/Conflict 中间 |
| **Game / AppID** | Detail `view_game`；Library 左侧游戏列表；Import 目标游戏；Deploy 表单 | Library 筛选侧 + Detail Metadata 一行 | Card 无需游戏名（已在左侧筛选上下文） |
| **Mod Files** | Detail「基本信息」内嵌列表 + enable checkbox | Detail **Files** 独立区 | 文件管理不是「基本身份信息」 |
| **Notes / 自定义介绍 / 收藏** | Card 收藏仅 ★；Detail「用户元数据」；Edit；搜索 notes | Detail Metadata / Edit；Card 仅 ★ | 备注与介绍绝不应上 Card |
| **Category Tags** | Detail「Tags」；Library `category_combo` | Detail Tags（可折叠）+ 筛选 | Card 上不展示标签全文 |
| **Relationships** | Card `relation_badge` 计数；Detail Relationships + 用户标记冲突列表 | Card 可选极简计数；Detail Relationships | 用户标记里的冲突列表与 Relationships Conflicts 功能重叠 |
| **Deploy path / time / type / error** | 仅 Detail「部署状态」 | Detail Status / Deploy 子区 | 正确；勿上 Card |

---

## 3. Card 重构方案

### 3.1 目标 IA（Must-have only）

```
ModCardWidget
├── Cover
│   ├── [State badge] 左上 — Conflict > Invalid > Disabled（互斥）
│   ├── [Platform badge] 右上 — Steam / Nexus / GitHub
│   └── （可选）Deploy 小点/角标 — 已部署绿 / 失败红；未部署不显示
├── Name（1–2 行，★ 可选）
└── Status strip（单行徽章流，高度固定）
    └── Deploy badge（若不用角标）+ Offline-missing（仅缺失时）
```

### 3.2 明确禁止上 Card

| 禁止字段 | 当前 widget | 处置 |
|---|---|---|
| URL / 来源链接 | （无直接控件，菜单有 Steam） | 仅 Detail / 菜单「原链接」 |
| Files / Mod Files | — | 仅 Detail Files |
| Version / Installed | — | 仅 Detail Version |
| Notes / 自定义介绍 | — | 仅 Detail / Edit |
| External ID / Workshop ID | `meta_label` | **删除该行** |
| Steam 原名固定行 | `steam_label` | **删除该行**；必要时 tooltip |
| 「离线页已同步」成功全文 | `offline_label` 成功态 | 改为仅缺失时显示「Offline」badge，或完全下沉 Detail |
| 关系全文 | `relation_badge` 可保留极简计数，或下沉 Detail | 若保留，不占卡片正文高度（仅 cover overlay） |

### 3.3 布局后果

- 去掉 `steam_label` + `meta_label` + 常驻 offline 行后，卡片高度显著下降，网格密度提升。
- `deploy_badge` 保留为正文唯一「状态条」或并入 cover 角标，避免与 State badge 语义打架：  
  **State（Conflict/Invalid/Disabled）优先级高于 Deploy 展示。**
- 选中态：停止在 `set_selected` 里 `setStyleSheet` 覆盖整卡（见主题审计）；改用 `QFrame#modCard[selected="true"]`（`styles.py` 已有规则）。

---

## 4. DetailPanel 重构方案

### 4.1 目标分区顺序

| Order | Section | 内容 | 默认 |
|---|---|---|---|
| 1 | **Header** | Cover、显示名、Platform badge、收藏 ★、更换封面 | 展开 |
| 2 | **Status**（合并现有两个「状态」+ 部署摘要） | 高优：Conflict / Invalid / Enabled / Deploy / Offline；摘要一行徽章 + 关键错误/审计 | 展开 |
| 3 | **Files** | Mod Files 列表与 enable | 有多文件则展开，否则折叠 |
| 4 | **Version** | Version / Installed / Status(update) | **默认折叠** |
| 5 | **Metadata** | External ID、来源 URL、游戏/AppID、自定义介绍、备注、Steam 原名（若不同） | **默认折叠** |
| 6 | **Tags & Relationships** | Category Tags；Dependencies / Conflicts / Addons / Patches | **默认折叠** |
| 7 | **Actions**（`#detailFooter` 固定底） | 精简主操作 | 固定 |

### 4.2 Status 区内优先级

1. Conflict（含文件冲突摘要 +「冲突详情」展开）
2. Invalid
3. Disabled / Enabled
4. Deploy（状态 + 失败原因；路径/时间/类型次级）
5. Offline（状态文案；下载/打开按钮在 Footer）

生命周期按钮（Enable / 标记失效 / 标记冲突…）保留在 Status，但分组为「快捷操作」子行，避免 6 按钮平铺。

### 4.3 合并 / 删除

- **删除重复 section 标题「状态」×2**：离线并入统一 Status。
- **「用户标记」与 lifecycle status 合并**：失效/冲突只保留一套写入路径（建议以 `mod_status` 为准；tag/relation 作为 Relationships 的数据入口）。
- **「基本信息」拆分**：名称上移 Header；Files → Files；ID/URL/Game → Metadata。
- **Footer 精简建议（2 行）**：  
  - 主：打开目录 · 打开来源 · 离线（保存/打开） · Deploy/Redeploy/Undeploy  
  - 次：编辑 · 复制信息 · Remove Mod  
  - 去掉与 Metadata 行内重复的「复制链接」双入口（保留一处即可）。

### 4.4 Edit 模式

保持非模态 stack；字段仅：显示名称、自定义介绍、备注、收藏。样式必须走全局 `QTextEdit` dark token（见 Phase 5）。

---

## 5. 全局主题问题列表

> 判定标准：未纳入 dark token、依赖 Qt 默认浅色底、或与 `styles.py` / 面板内颜色不一致，导致白底/灰底闪烁或语义色分裂。

### 5.1 `styles.py` 缺口（系统性）

```
文件: ui/styles.py
组件: QTextEdit / QTextBrowser
问题: APP_STYLE 未定义，Edit 页与其它对话框易出现系统默认白色背景
建议: background-color: #1a2330; color: #e8eef5; border: 1px solid #2c3a4d;
```

```
文件: ui/styles.py
组件: QListWidget（无 objectName）
问题: 仅 #gameList / #navList 有暗色规则；Detail 内关系列表、冲突列表、ModPicker 列表走系统默认（常见白底）
建议: 增加通用 QListWidget 规则，或 #detailList / #modPickerList { background-color: #1a2330; color: #c7d5e0; }
```

```
文件: ui/styles.py
组件: QCheckBox / QRadioButton
问题: 无样式；Import / Detail / Sync 勾选控件在 Windows 上常显浅色 indicator
建议: color: #e8eef5; spacing/indicator 使用 #2c3a4d / #66c0f4
```

```
文件: ui/styles.py
组件: QWidget（通用）
问题: 仅 QMainWindow/QDialog 设 background #121820；中间 library_host / QStackedWidget 页可能透出平台默认底
建议: QWidget { background-color: transparent; } 或给页面根设 #121820；卡片区明确 BACKGROUND_PRIMARY
```

```
文件: ui/styles.py
组件: QSplitter::handle
问题: 未定义，Library 三栏分隔条可能偏亮
建议: background-color: #243044;
```

```
文件: ui/styles.py
组件: Design Tokens
问题: 颜色硬编码散落；无 BACKGROUND_* / ACCENT_* 常量层（Python 或注释规范）
建议: 抽出 token 表并让 APP_STYLE / _PANEL_STYLE / badge 共用
```

### 5.2 Mod Card

```
文件: ui/mod_card.py
组件: QLabel (steam_label) ≈ L178
问题: 内联 color #6b7c8f，未用 objectName / token
建议: color: TEXT_SECONDARY (#8b9bb0 或 #6b7c8f 统一其一)
```

```
文件: ui/mod_card.py
组件: QLabel (meta_label) ≈ L192
问题: 同上内联样式；且内容为 Workshop ID（IA 也应移除）
建议: 删除控件或改为 token 化后仅 Detail 使用
```

```
文件: ui/mod_card.py
组件: QLabel (offline_label) ≈ L208 / L464 / L468
问题: 成功 #6b9e78、警告 #c9a227 与 Detail 的 #3fb950 / #d4a017 不一致
建议: 统一 ACCENT_SUCCESS / ACCENT_WARNING
```

```
文件: ui/mod_card.py
组件: QFrame#modCard.setStyleSheet ≈ L342–L347
问题: 选中时用内联 stylesheet 覆盖，清空时 setStyleSheet("") 可能干扰 APP_STYLE 中 QFrame#modCard[selected="true"]
建议: 仅 setProperty("selected", …) + polish；颜色用 styles.py
```

```
文件: ui/mod_card.py
组件: QLabel#deployBadge ≈ L499–L505
问题: 运行时拼装 bg/fg，未进设计系统
建议: badge token：DEPLOYED / NOT_DEPLOYED / FAILED
```

```
文件: ui/mod_card.py
组件: QLabel#modTagBadge ≈ L578–L584
问题: Conflict/Invalid/Disabled 色值内联
建议: STATE_* badge tokens
```

```
文件: ui/mod_card.py
组件: QLabel#modPlatformBadge ≈ L656–L662
问题: Steam/Nexus/GitHub 色值内联于 _apply_platform_badge
建议: PLATFORM_* badge tokens（与 Phase 6 一致）
```

### 5.3 Mod Detail Panel

```
文件: ui/mod_detail_panel.py
组件: QWidget#modDetailPanel / _PANEL_STYLE ≈ L2174+
问题: 面板样式游离于 styles.py，token 双源
建议: 合并进 APP_STYLE 或 styles.PANEL_STYLE 常量
```

```
文件: ui/mod_detail_panel.py
组件: QLabel (view_deploy_error) ≈ L456
问题: 内联 color #e07070
建议: color: ACCENT_ERROR (#e06c75 统一)
```

```
文件: ui/mod_detail_panel.py
组件: QLabel (view_deploy_audit / view_deploy_conflict / view_tag_deploy_hint) ≈ L462, L468, L679
问题: 内联 #c9a227，与 offline 警告 #d4a017 分裂
建议: ACCENT_WARNING 单一值
```

```
文件: ui/mod_detail_panel.py
组件: QLabel (status_conflict_detail) ≈ L572
问题: 内联 #e06c75
建议: ACCENT_ERROR
```

```
文件: ui/mod_detail_panel.py
组件: QPushButton (btn_remove_mod) ≈ L737
问题: 仅设 color #e06c75，背景仍走默认按钮蓝灰，危险操作不够明确
建议: objectName panelDangerButton { background #3d1a1a; color #e06c75; border #8b3a3a; }
```

```
文件: ui/mod_detail_panel.py
组件: QListWidget (_rel_lists / tag_conflict_list) ≈ L622, L664
问题: 无 objectName、无暗色 stylesheet → 默认白底高风险
建议: setObjectName("detailList"); styles 中定义暗色
```

```
文件: ui/mod_detail_panel.py
组件: QTextEdit (edit_custom_desc / edit_notes) ≈ L820, L827
问题: 无局部样式且全局无 QTextEdit 规则 → 编辑页白底
建议: background-color: #1a2330; color: #e8eef5;
```

```
文件: ui/mod_detail_panel.py
组件: QCheckBox (tag_* / mod file rows / edit_favorite)
问题: 无暗色样式
建议: 全局 QCheckBox token
```

```
文件: ui/mod_detail_panel.py
组件: QLabel (view_offline) 动态样式 ≈ L1604–L1639, L1797
问题: 成功用 #3fb950，Card 用 #6b9e78；警告 #d4a017 vs #c9a227
建议: 统一 ACCENT_SUCCESS / WARNING / ERROR
```

```
文件: ui/mod_detail_panel.py
组件: QLabel (mod file sub) ≈ L1056
问题: 内联 #8b9bb0
建议: TEXT_SECONDARY
```

### 5.4 Library View

```
文件: ui/library_view.py
组件: QLabel (title「Mod 库」) ≈ L98
问题: 仅 font-size/weight，未用 title 层级 token
建议: objectName pageTitle 或复用近似 titleLabel 的次级样式
```

```
文件: ui/library_view.py
组件: QLabel (count_label) ≈ L100
问题: 内联 #8b9bb0
建议: TEXT_SECONDARY / subtitleLabel
```

```
文件: ui/library_view.py
组件: QLabel (deploy_audit_banner) ≈ L117
问题: 内联 #c9a227；与 objectName subtitleLabel 的 #8b9bb0 冲突（后设 stylesheet 覆盖）
建议: objectName warningBanner { color: ACCENT_WARNING; }
```

```
文件: ui/library_view.py
组件: QLabel (game_label / tag_label / sort_label) ≈ L139, L207, L216
问题: 重复内联 caption 样式
建议: objectName fieldCaption → styles.py
```

```
文件: ui/library_view.py
组件: QWidget (library_host / center)
问题: 无显式背景；卡片间隙可能透出非 #121820
建议: background-color: BACKGROUND_PRIMARY (#121820)
```

### 5.5 Game Deploy / Import / Picker

```
文件: ui/game_deploy_view.py
组件: QLabel (heading 及多处 caption) ≈ L93, L108, L156, L185, L194
问题: 内联字体色，未 token 化
建议: pageTitle + fieldCaption
```

```
文件: ui/game_deploy_view.py
组件: QComboBox#deployGameCombo / #deployTypeCombo
问题: objectName 已设但 styles.py 无对应规则（继承通用 QComboBox，尚可；可与 librarySortCombo 对齐）
建议: 复用 INPUT 表面 token
```

```
文件: ui/mod_import_dialog.py
组件: QDialog#modImportDialog
问题: 无专用 stylesheet；依赖全局 QDialog 底
建议: 可接受；补充 QRadioButton/QCheckBox/Form 标签色
```

```
文件: ui/mod_import_dialog.py
组件: QRadioButton / QCheckBox (offline_clean_check) ≈ L90–L98, L338
问题: 无暗色样式
建议: 全局控件规则
```

```
文件: ui/mod_picker_dialog.py
组件: QListWidget (self.list) ≈ L86
问题: 无 objectName / 样式 → 白底列表高风险
建议: background-color: #1a2330; color: #c7d5e0; border: 1px solid #2c3a4d;
```

```
文件: ui/mod_edit_dialog.py / ui/mod_detail_dialog.py
组件: QTextEdit
问题: mod_edit_dialog 无样式；mod_detail_dialog 有局部 _DIALOG_STYLE（含 QTextEdit 暗色）— 不一致
建议: 全局 QTextEdit，删除重复局部定义
```

### 5.6 语义色不一致对照（必须收敛）

| 语义 | Card | Detail | 建议 Token |
|---|---|---|---|
| Success / Offline OK | `#6b9e78` | `#3fb950` | `ACCENT_SUCCESS = #3fb950`（或统一 Steam 绿 `#6b9e78`，二选一） |
| Warning | `#c9a227` | `#d4a017` | `ACCENT_WARNING = #d4a017` |
| Error | `#e07070` | `#e06c75` | `ACCENT_ERROR = #e06c75` |
| Accent brand | `#66c0f4` | `#66c0f4` | `ACCENT_PRIMARY`（已一致） |

---

## 6. Design Token 设计

### 6.1 Color Tokens（建议写入 `ui/styles.py` 顶部常量，再拼进 stylesheet）

| Token | Value | 用途 |
|---|---|---|
| `BACKGROUND_PRIMARY` | `#121820` | 主窗 / Dialog / Footer |
| `BACKGROUND_SIDEBAR` | `#171e28` | nav、controlPanel、card 默认 |
| `BACKGROUND_PANEL` | `#151c26` | Detail 面板 |
| `BACKGROUND_CARD` | `#171e28` | ModCard |
| `BACKGROUND_CARD_SELECTED` | `#1e2a3a` | 选中卡 |
| `BACKGROUND_SECTION` | `#1a2330` | detailSection、输入面 |
| `BACKGROUND_INPUT` | `#1a2330` | LineEdit / Combo / TextEdit / List |
| `BORDER_SUBTLE` | `#243044` | 卡片/分区边框 |
| `BORDER_DEFAULT` | `#2c3a4d` | 输入边框 |
| `BORDER_STRONG` | `#3d5a73` | hover |
| `BORDER_FOCUS` | `#66c0f4` | focus / selected |
| `TEXT_PRIMARY` | `#e8eef5` | 标题、主文 |
| `TEXT_SECONDARY` | `#8b9bb0` | caption、meta |
| `TEXT_BODY` | `#c7d5e0` | 正文 |
| `TEXT_MUTED` | `#6b7c8f` | empty / placeholder |
| `ACCENT_PRIMARY` | `#66c0f4` | 品牌、主按钮字底对比 |
| `ACCENT_PRIMARY_ON` | `#0b1520` | 主按钮文字 |
| `ACCENT_SUCCESS` | `#3fb950` | 成功 / 已部署 / 离线 OK |
| `ACCENT_WARNING` | `#d4a017` | 警告 / 离线缺失 / audit |
| `ACCENT_ERROR` | `#e06c75` | 失败 / 冲突详情 / Remove |
| `ACCENT_SUCCESS_BG` | `#1a3d2e` | success badge 底 |
| `ACCENT_WARNING_BG` | `#3a2410` | warning/invalid badge 底 |
| `ACCENT_ERROR_BG` | `#3a1418` | conflict/error badge 底 |
| `ACCENT_NEUTRAL_BG` | `#2a3038` | 未部署 / disabled 底 |

### 6.2 Badge System

#### Platform badges（identity，不参与状态优先级竞争）

| Platform | Text | BG | FG | Border |
|---|---|---|---|---|
| Steam | `Steam` | `#1b2838` | `#66c0f4` | `#2a475e` |
| Nexus | `Nexus` | `#2a1f14` | `#d4a017` | `#6b4f1d` |
| GitHub | `GitHub` | `#1c1c1c` | `#c9d1d9` | `#484f58` |

#### State badges（互斥，Card 左上只显示一个）

| Priority | State | Text | BG | FG | Border |
|---|---|---|---|---|---|
| 1 | Conflict | `Conflict` | `#3a1418` | `#ff6b6b` | `#8b2e2e` |
| 2 | Invalid | `Invalid` | `#3a2410` | `#f0a040` | `#8b5a20` |
| 3 | Disabled | `Disabled` | `#2a2a2a` | `#b0b0b0` | `#555555` |
| 4 | Deployed* | `Deployed` | `#1a3d2e` | `#6b9e78` | `#2d6b4f` |
| — | Offline missing* | `Offline` | warning tokens | | |
| — | Platform | （右上固定） | platform tokens | | |

\* Deploy / Offline / Platform **不覆盖** Conflict/Invalid/Disabled。  
**完整优先级（产品规则）**：`Conflict > Invalid > Disabled > Deploy > Platform`  
（Platform 始终可显示，但视觉权重最低；State 槽位被更高优先级占用时 Deploy 可降为小点或移到 status strip。）

### 6.3 组件 objectName 约定（新增）

| objectName | 用途 |
|---|---|
| `pageTitle` | 各页「Mod 库」「游戏部署设置」 |
| `fieldCaption` | 表单/筛选小标题 |
| `warningBanner` | deploy_audit_banner |
| `detailList` | Detail 内 QListWidget |
| `statusBadge` / `platformBadge` | 统一徽章（可替换现有 mod*Badge） |
| `panelDangerButton` | Remove Mod |
| `collapsibleSection` | 可折叠分区头 |

---

## 7. 默认应折叠的信息

采用 **分区头点击展开**（`QToolButton` checkable 或自绘 `#collapsibleSection`），默认：

| 默认折叠 | 内容 | 展开模式 |
|---|---|---|
| Version | Version / Installed / update Status | 点击「Version ▸」 |
| Metadata | External ID、来源 URL、游戏/AppID、Steam 原名、介绍、备注 | 「更多信息 ▸」 |
| Deploy 细节 | 目标路径、部署时间、部署类型（失败原因建议仍在 Status 摘要可见） | Status 内「部署详情 ▸」 |
| Offline 细节 | Provider、更新时间（状态徽章保留） | Status 内「离线详情 ▸」 |
| Tags | Category tags 编辑 | 「Tags ▸」 |
| Relationships | 四类关系列表 | 「Relationships ▸」 |
| 冲突详情 | `status_conflict_detail`（已有 checkable「冲突详情」） | 保持按需展开 |
| 用户标记（若短期保留） | 整段 legacy UI | 默认折叠并标注 Deprecated，最终删除 |
| Files | 单文件/未登记时 | 折叠或仅一行「（单文件）」；多文件默认展开 |

**默认展开**：Header、Status（高优摘要 + 关键按钮）、Actions footer。

---

## 8. 后续开发拆分 Phase

### Phase A — Design Tokens & 全局暗色补丁（低风险）

1. 在 `ui/styles.py` 抽出 token 常量。  
2. 补齐 `QTextEdit`、`QListWidget`、`QCheckBox`、`QRadioButton`、`QSplitter::handle`。  
3. 将 `_PANEL_STYLE` 并入或共享 tokens。  
4. 去掉 Card 选中态内联 `setStyleSheet`，改用 property。  
5. 统一 SUCCESS/WARNING/ERROR 色值。  
**验收**：Detail 关系列表、Edit QTextEdit、Import 单选、ModPicker 列表无白底。

### Phase B — Mod Card IA 瘦身

1. 移除 `steam_label`、`meta_label` 行；重算 `card_h`。  
2. Offline 改为缺失-only badge 或下沉。  
3. Deploy/State/Platform 按 badge 优先级渲染。  
4. Tooltip 保留 ID / Steam 原名供悬停。  
**验收**：卡片仅 Cover + Name + 核心徽章；网格更密。

### Phase C — Detail Panel 信息架构重组

1. 合并双「状态」→ 单一 Status。  
2. 抽出 Files / Version / Metadata 分区；低频率默认折叠。  
3. Footer 按钮减至 2 行。  
4. 规划废弃「用户标记」与 lifecycle 双轨（可先 UI 隐藏写入入口，数据迁移另开任务）。  
**验收**：首屏只见 Header + Status + Actions；无名称/平台/部署三重长文重复。

### Phase D — Library 壳层抛光

1. caption/banner 去内联样式，改 objectName。  
2. `library_host` 明确背景。  
3. 空态/加载态已有 objectName，核对 token。  
**验收**：Library 页无裸内联色（徽章动态色除外）。

### Phase E — Import / Deploy / Picker 对齐

1. Import / Deploy captions 与 checkbox/radio 走全局样式。  
2. ModPicker list 暗色。  
3. 与 Library 视觉语言一致（不改导入/部署业务逻辑）。  
**验收**：三页菜单式控件无浅色系统皮肤。

### Phase F — 冲突/失效单一数据源（可选，偏业务）

1. 产品确认：`mod_status` vs `mod_tags` vs `relationships` 的权威模型。  
2. UI 只暴露一套编辑面。  
**验收**：Card Conflict 与 Detail Status 始终一致。

---

## 附录 A — 关键真实文案 / objectName 索引

| 文案或 objectName | 位置 |
|---|---|
| `modCard`, `modTagBadge`, `modPlatformBadge`, `modRelationBadge`, `deployBadge` | `mod_card.py` |
| `modDetailPanel`, `detailPanelInner`, `detailSection`, `detailFooter` | `mod_detail_panel.py` |
| Section: 概览 / 基本信息 / 状态 / 部署状态 / 用户元数据 / 状态 / Tags / Relationships / 用户标记 / 操作 | `mod_detail_panel.py` |
| `librarySearchBox`, `libraryFilterChip`, `librarySortCombo`, `gameList`, `controlPanel` | `library_view.py` / `styles.py` |
| 筛选：全部、收藏、已部署、失效、冲突、已禁用、离线页面缺失 | `library_query.STATUS_FILTER_LABELS` |
| 平台：全部平台、Steam、Nexus、GitHub | `PLATFORM_FILTER_LABELS` |
| 导入对话框标题「导入 Mod」；目标游戏；Steam Workshop / Nexus Mods / GitHub | `mod_import_dialog.py` |
| 「游戏部署设置」；部署类型 folder_copy / palworld_pak | `game_deploy_view.py` |

---

## 附录 B — 审计结论（一句话）

当前 Library 是「胖卡片 + 超长详情 + 双状态轨 + 主题双源」；重构应把 **扫视信息收敛到 Card 徽章**，把 **决策与编辑收敛到 Detail 的 Status→Files→折叠 Metadata**，并把 **所有颜色收拢到 `styles.py` tokens**，以消除白底控件与语义色分裂。
