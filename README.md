# Steam Mod Manager

一个面向 Steam 游戏玩家的本地 Mod 管理工具。

支持：

- 自动识别 Steam Workshop / Mod.io / Nexus Mods Mod
- Mod 信息整理与本地归档
- Mod 元数据管理
- 离线网页保存
- Mod 部署与撤销
- 多游戏 Mod 库管理
- 部署配置与游戏目录管理
- Mod 部署记录与环境恢复

目标：

> 将散落在 Steam Workshop、Nexus、Mod.io 等平台的 Mod 转化为可管理、可备份、可部署的本地 Mod 资产库。


---

# ✨ 功能特性

## 1. Mod 本地化管理

支持将 Mod 自动整理为：

```
mod/
 └── 游戏名称/
      └── Mod名称/
           ├── mod文件
           ├── .info/
           │    ├── metadata.json
           │    └── offline/
           │         └── index.html
           └── backup/
```

解决：

- Steam Workshop 数字 ID 难以管理
- Mod 名称变化
- 多版本混乱
- Mod 丢失


---

# 2. 多来源 Mod 支持

目前支持：

## Steam Workshop

支持：

- Workshop ID
- HTML 信息归档
- 本地 Mod 管理


## Mod.io

支持：

- Mod.io 页面解析
- 元数据同步
- 离线网页保存


## Nexus Mods

支持：

- Nexus Mod 导入
- Archive 自动识别
- 部署解压


---

# 3. 离线网页归档

支持保存 Mod 页面：

```
.info/offline/index.html
```

特点：

- Playwright 浏览器快照
- 本地资源保存
- 相对路径修复
- 支持离线查看


生成结构：

```
.info/
 └── offline/
      ├── index.html
      └── assets/
```


---

# 4. Mod 部署系统

支持：

- 自动识别 Mod 类型
- 自动解压 Archive
- 文件验证
- 部署到游戏目录


支持格式：

```
.zip
.rar
.7z
```

RAR 解压策略：

```
RAR
 |
 +-- bundled UnRAR
 |
 +-- system unrar
 |
 +-- 7-Zip fallback
```


内置工具：

```
bin/
 └── tools/
      └── UnRAR.exe
```


---

# 5. Path Lifecycle 路径生命周期管理

解决 Mod 路径漂移问题。


系统统一管理：

```
文件系统路径

        ↓

SQLite

        ↓

sidecar metadata

        ↓

backup record
```


支持：

- 自动恢复移动后的 Mod
- rename 后路径同步
- stale path 修复


---

# 6. 部署记录系统

支持保存 Mod 部署组合：

例如：

```
星露谷宝可梦周边 Mod 一周目
```

记录：

```
Game
 |
 +-- Mod A
 |
 +-- Mod B
 |
 +-- Mod C
```


用途：

- 不同存档使用不同 Mod 环境
- 快速恢复 Mod 配置
- 管理多个游戏体验环境


---

# 技术栈

## Backend

```
Python 3.11
SQLite
PySide6
```


主要依赖：

```
beautifulsoup4
curl_cffi
rarfile
py7zr
playwright
```


## GUI

```
PySide6
Qt
```


## Browser Automation

```
Playwright Chromium
```


---

# 项目结构

```
steam-mod-manager/

├── main.py

├── services/

│   ├── deploy.py
│   ├── path_lifecycle.py
│   ├── backup.py
│   ├── manifest.py
│   │
│   ├── offline/
│   │    ├── manager.py
│   │    ├── modio.py
│   │    └── modio_browser_snapshot.py
│   │
│   └── importers/
│        └── archive.py


├── ui/

│   ├── library_view.py
│   ├── mod_detail_panel.py
│   └── offline_archive_thread.py


├── tests/

├── data/

├── mod/

└── bin/

    └── tools/

        └── UnRAR.exe
```


---

# 安装

## 1. 创建虚拟环境


Windows:

```powershell
python -m venv .venv
```


激活：

```powershell
.\.venv\Scripts\activate
```


---

## 2. 安装依赖


```powershell
pip install -r requirements.txt
```


---

## 3. 安装 Playwright 浏览器


```powershell
playwright install chromium
```


验证：

```powershell
playwright install --list
```


---

# requirements.txt

项目所有 Python 依赖统一维护：

```
PySide6
beautifulsoup4
curl_cffi
rarfile
py7zr
playwright
requests
```

安装：

```powershell
pip install -r requirements.txt
```


---

# 运行


开发模式：

```powershell
python main.py
```


---

# 测试


运行全部测试：

```powershell
pytest
```


当前覆盖：

## Offline Archive

```
21 passed
```


包括：

- Playwright runtime
- Chromium 检测
- 页面快照
- 错误分类
- 实机 E2E


## RAR Archive

```
8 passed
```


包括：

- bundled UnRAR
- system unrar
- 7z fallback
- 错误分类


---

# 配置

## 游戏部署配置


每个游戏支持：

```
游戏目录

Mod安装目录

部署策略
```


例如：

```
Baldur's Gate 3

Game:

D:\SteamLibrary\steamapps\common\
Baldurs Gate 3


Mods:

...\Baldurs Gate 3\Mods
```


---

# 常见问题


## 1. RAR提示缺少解压组件


检查：

```
bin/tools/UnRAR.exe
```


确认：

```powershell
python -c "import rarfile;print(rarfile.__version__)"
```


如果缺少：

```powershell
pip install rarfile
```


---

## 2. 离线网页保存失败


检查：

```powershell
playwright install --list
```


确保存在：

```
chromium
```


---

## 3. Git无法提交数据库文件


SQLite运行时文件：

```
*.db-shm
*.db-wal
```


不要提交。


添加：

```
*.db-shm
*.db-wal
*.db-journal
```


到：

```
.gitignore
```


---

# 开发原则

## 1. 不允许隐藏真实错误


错误必须分类：

例如：

```
PLAYWRIGHT_BROWSER_MISSING

PLAYWRIGHT_LAUNCH_FAILED

RAR_BAD_ARCHIVE

RAR_TOOL_MISSING
```


禁止：

```
except Exception:
    返回通用错误
```


---

## 2. 所有路径必须经过 Path Lifecycle


禁止：

- Worker 保存旧路径
- UI 缓存路径
- 手动拼接路径


---

## 3. 所有依赖必须进入 requirements.txt


禁止：

- 本地环境能运行
- 新环境无法启动


每次新增第三方库：

必须同步：

```
requirements.txt
```


---

# Roadmap

## 已完成

- [x] Mod 本地管理
- [x] Steam Workshop 支持
- [x] Mod.io 支持
- [x] Offline Archive
- [x] Path Lifecycle
- [x] Archive 自动解压
- [x] Deploy 系统


## 进行中

- [ ] 自动更新检测
- [ ] Mod 冲突分析
- [ ] Mod 加载顺序优化
- [ ] 云端备份


---

# License

Private Project
