# Archive Proxy + UI Blocking Diagnosis

**Date:** 2026-08-05  
**Scope:** Read-only runtime audit for Mod `3761838546`  
**Constraint:** No production code changes in this phase  

**Environment proven at runtime:**
- `curl_cffi` **0.15.0**
- QSettings org/app: `SteamModManager` / `WorkshopLibrary`
- Key: `network/proxy_url`
- Local SOCKS that works: `socks5://127.0.0.1:7897`
- Mod folder: `mod/Palworld/[Palworld 1.0] 4x Storage - Bigger Chests and Containers`
- Current `index.html`: **stub** (`is_stub_offline_page=True`, size ~1412)

---

## A. 实际代理传递链（运行时探针）

### A.1 当前机器 QSettings（真实值，非推测）

| 项 | 值 |
|---|---|
| QSettings file | `HKEY_CURRENT_USER\Software\SteamModManager\WorkshopLibrary` |
| `network/proxy_url` | `''`（空字符串） |

### A.2 路径 1：同步中心 Sync（代码接线正确，但输入为空）

当 UI 把空字符串塞进 `SyncOptions` 时，实测层值：

| 层 | proxy 实际值 |
|---|---|
| QSettings `network/proxy_url` | `''` |
| `sync` / `SyncOptions.proxy_url` | `''` |
| `SyncOptions.proxies_dict()` | `None` |
| `OfflinePageArchiver(proxies=…)` | `None` → `self._proxies = None` |
| `archive` | `self_proxies=None` |
| `_fetch_main_html` | `self_proxies=None` |
| `_http_get` | `will_pass_proxies=False` → **不传** `proxies` |
| `curl_cffi.requests.get` | `proxies=<<MISSING>>`, `proxy=<<MISSING>>` → **直连** |

**代理在哪一层“丢失”？**

不是中间层把非空代理弄丢了。  
**丢失点在源头：QSettings / Sync UI 当前存的是空代理。**

空值从第一层起就是直连：

```
QSettings '' → SyncOptions '' → proxies_dict None → Archiver None → _http_get 省略 proxies → 直连
```

### A.3 路径 2：查看 Mod 信息（永远不读代理）

`ModDetailDialog._populate()` → `_ensure_offline_page()`：

```python
with OfflinePageArchiver() as archiver:  # 无 proxies 参数
    path = archiver.ensure_offline_page(...)
```

实测：

| 层 | proxy 实际值 |
|---|---|
| QSettings | （未读取） |
| OfflinePageArchiver | `proxies=None` |
| archive / `_fetch_main_html` / `_http_get` | `None` / 直连 |
| `curl_cffi` | `proxies=<<MISSING>>` |

**第二条丢失链：** 即使以后 QSettings 填了代理，详情页这条路径也**永远直连**，因为它根本不注入 `proxies`。

### A.4 反证：代理存在时整条链能传到 curl_cffi

`SyncOptions(proxy_url='socks5://127.0.0.1:7897')` → `OfflinePageArchiver(proxies=opts.proxies_dict())` 实测：

```
OfflinePageArchiver.__init__ proxies=
  {'http': 'socks5://127.0.0.1:7897', 'https': 'socks5://127.0.0.1:7897'}
_fetch_main_html self_proxies=同上
_http_get will_pass_proxies=True
CURL_CFFI_CALL proxies=
  {'http': 'socks5://127.0.0.1:7897', 'https': 'socks5://127.0.0.1:7897'}
→ HTTP 成功, ~0.93s, ~82KB
```

**结论：** Sync 代码路径在代理非空时**会**把 SOCKS5 传到 `curl_cffi`。当前失败是因为运行时代理为空 + 详情页从不传代理。

---

## B. curl_cffi 最终参数

### B.1 参数名

`curl_cffi` 0.15.0 `Session.request` 同时支持：

- `proxies: Optional[ProxySpec]`
- `proxy: Optional[str]`

项目代码使用：

```python
kwargs["proxies"] = self._proxies   # 仅当 self._proxies 非空
return curl_requests.get(url, **kwargs)
```

这是正确用法（`proxies=`，不是错误的唯一依赖 `proxy=`）。

### B.2 格式实测（同一 URL）

| 格式 | 结果 |
|---|---|
| `socks5://127.0.0.1:7897`（http+https 都设） | **200**, ~0.86s, ~84KB |
| `socks5h://127.0.0.1:7897` | **SSLError / curl (35)** TLS connect error |
| 不传 proxies（直连） | **Timeout curl (28)** ~15s/次 |

**对本机 Clash/本代理：应使用 `socks5://`，不要改成 `socks5h://`。**

### B.3 直连时实际 kwargs（探针）

```
url=https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546
proxies=<<MISSING>>
proxy=<<MISSING>>
timeout=15
impersonate=chrome131
```

### B.4 有代理时实际 kwargs（探针）

```
proxies={'http': 'socks5://127.0.0.1:7897', 'https': 'socks5://127.0.0.1:7897'}
proxy=<<MISSING>>   # 未使用单字符串 proxy=，不影响成功
timeout=15
impersonate=chrome131
```

---

## C. 3761838546 A/B/C 网络测试

目标 URL：

`https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546`

| 测试 | 方式 | proxy | status | elapsed | size | 异常 |
|---|---|---|---|---|---|---|
| **A** | 当前 Archive 实际方式（详情页 / 空 QSettings）：`OfflinePageArchiver()` | 无 / MISSING | fail→stub | **30.62s** | stub ~1.3KB | `curl:(28)` ×2（`MAIN_PAGE_RETRIES=2`） |
| **B** | 同 `curl_cffi` + 明确 SOCKS5 | `socks5://127.0.0.1:7897` | **200** | **0.86s** | **83813** | 无 |
| **B2** | `socks5h://` | `socks5h://127.0.0.1:7897` | fail | 5.02s | — | TLS / curl (35) |
| **C** | 项目配置注入：`SyncOptions(proxy_url=QSettings)` | `''` → `None` | fail | **30.61s** | — | 同 A，直连 timeout |
| **C2** | 若配置为 SOCKS5（等价于 B 的注入路径） | dict 含 socks5 | **ok** | **0.93s** | **81975** | 无 |

**证明：**

1. **B 能成功。**
2. **C（当前 QSettings）≠ B** — 当前配置注入等价于直连失败。
3. **C2 ≡ B** — 一旦 `proxy_url` 非空并经 `proxies_dict()` 注入，与手写 SOCKS5 等价且成功。

---

## D. 查看 Mod 信息调用链

```
用户点击「查看详情」(ui/mod_card.py::_open_detail)
  → ModDetailDialog(...)          # 构造函数在 UI 线程
      → _build_ui()
      → _populate()               # 仍在 UI 线程
          → 读本地 mod.json / 封面 / 描述（快）
          → _ensure_offline_page()   # ★ 同步阻塞点
              → OfflinePageArchiver()          # proxies=None，永不读 QSettings
              → ensure_offline_page(...)
                  → 若 index 缺失 / 空 / is_stub_offline_page:
                        archive()
                          → _fetch_main_html
                            → _http_get × MAIN_PAGE_RETRIES(2)
                              → curl_cffi 直连 Steam，timeout=15s 每次
                          → 失败则 write_fallback_page(stub)
  → dialog.exec()                 # 对话框此时才真正显示/交互
```

**结论：用户请求线程（GUI 主线程）同步等待 Steam。**

这不是后台 QThread；`SyncWorker` 不参与详情打开。

---

## E. 一次「查看 Mod 信息」期间的 Steam HTTP 次数

对 **当前 stub 状态** 的 `3761838546`，一次打开详情：

| 请求 | 来源 | 次数 |
|---|---|---|
| Workshop 主 HTML | `ensure_offline_page` → `archive` → `_fetch_main_html` | **2**（retry） |
| CSS / 图片 / 字体 | 仅主 HTML 成功后才会下 | **0**（主页失败） |

实测探针：

```
ensure_elapsed 30.62
http_calls 2
first_call_proxies <<MISSING>>
out_stub True
```

若用户再点「打开离线页面」：再次调用 `_ensure_offline_page()` → **再 2 次** 直连 Steam（又 ~30s）。

`library_view.refresh()` 的 `backfill_offline_pages()`：**已有 index.html 就 skip**（含 stub），不会在刷新时重抓 stub；卡顿主因是详情对话框，不是 refresh backfill。

---

## F. timeout 是否阻塞用户请求

| 项 | 值 |
|---|---|
| 单次 timeout | 15s |
| 主页重试 | 2 |
| 一次失败归档 | ≈ **30s**（+少量 sleep） |
| 执行线程 | **GUI 主线程** |

日志形态（已复现）：

```
Failed to perform, curl: (28) Connection timed out after 1501x milliseconds
Live Workshop archive failed for 3761838546 (...); writing minimal stub
```

**页面卡顿 = 同步 Steam 网络请求阻塞用户请求（打开详情）。**

卡顿时长与 curl timeout × retries 一致，不是“界面渲染慢”。

---

## G. stub 是否导致重复重抓

### G.1 机制（已确认存在）

```
archive 失败
  → 写入 stub index.html
  → is_stub_offline_page(index) == True
  → 下次 ensure_offline_page 不把 stub 当有效页
  → 再次 archive()
  → 再次直连 timeout ~30s
  → 再写 stub
```

对详情页：**每次打开 stub Mod 都会重新 archive。**

这与 stub 判断方向本身方向正确（避免永久卡在坏 stub），但**缺少失败冷却**，叠加**无代理直连**，造成“每次查看都卡 ~30 秒”。

### G.2 失败后是否写入 retry 状态？

审计 `mod.json`（3761838546）字段：

- 有：`offline_page_path`, `fetch_error`（当前为 `null`）
- **无：** `last_archive_attempt` / `last_archive_failure` / `next_retry_at`

`archive` timeout 后：只写 stub HTML + 日志；**不写**可驱动 cooldown 的同步状态。

因此系统无法知道“刚刚失败过”，只能在下次打开时再打 Steam。

---

## H. 根因排序

1. **P0 — 查看详情在 UI 线程同步 `ensure_offline_page` → `archive`，且永不注入代理**  
   直接解释“页面变卡”与每次打开 ~30s。

2. **P0 — 运行时 QSettings `network/proxy_url` 为空**  
   Sync 路径虽接线正确，但当前实际仍是直连 → `curl:(28)`。  
   （用户手动 SOCKS5 成功 ≠ 应用配置已生效。）

3. **P1 — stub + `is_stub_offline_page` 无 cooldown**  
   失败 stub 导致每次查看都重试；在直连失败环境下放大卡顿。

4. **P2 — `MAIN_PAGE_RETRIES=2` × 15s**  
   单次用户操作阻塞约 30s，体感更差。

5. **非根因（已排除）**
   - Steam 本身不可达（B 证明可达）
   - `proxies=` 参数名错误（0.15.0 支持且 C2 成功）
   - 需要改成 `socks5h://`（本机实测更差）
   - Sync 中途丢弃非空代理（反证 C2：非空时完整传到 curl_cffi）

---

## I. 最小修复方案（本阶段只设计，不改代码）

### I.1 代理（让真实请求等于 B）

1. 确认同步中心代理输入框保存到 QSettings 的值确实是  
   `socks5://127.0.0.1:7897`（当前为空是事实根因之一）。
2. 详情 / backfill 路径复用同一代理来源：  
   `OfflinePageArchiver(proxies=SyncOptions(proxy_url=QSettings…).proxies_dict())`  
   禁止裸 `OfflinePageArchiver()` 打 Steam。
3. 可选：启动或首次网络失败时提示“代理未配置且 Steam 直连超时”。

### I.2 UI 阻塞（让查看详情不再等 Steam）

目标架构：**B — 先返回已有 Mod 信息，后台异步 enrich/archive**

最小改动方向：

1. `_populate` **不要**同步调用会触发网络的 `ensure_offline_page`。
2. 已有任意 index（含 stub）或仅有 mod.json：对话框立即显示。
3. 需要补档时：`QThread` / worker 后台 archive；UI 显示“正在同步离线页”。
4. 「打开离线页面」：有有效页直接开；否则提示同步中/失败，勿在 UI 线程堵 30s。

### I.3 stub 重复重抓（保留 stub 判断，加 cooldown）

不要删除 `is_stub_offline_page`。增加例如：

- `last_archive_attempt`
- `last_archive_failure`
- `next_retry_at`（或固定 cooldown，如 15–60 分钟）

规则：

- stub + 未到 retry → **不**打 Steam，直接展示已有信息 / stub
- stub + 已到 retry → 后台重试（仍用代理）

### I.4 建议实施顺序

1. 详情页：去掉同步网络（立刻消卡）  
2. 全路径注入同一代理（消 timeout）  
3. stub cooldown（防反复重试）  
4. （可选）降低详情场景的同步重试次数

---

## 十、五个问题的明确答案

### 1. 为什么手动用代理可以成功，但实际同步仍然直连？

因为**应用运行时读到的代理是空的**（QSettings `network/proxy_url == ''`），`proxies_dict()` 返回 `None`，`_http_get` 不传 `proxies`，curl_cffi 直连。  
手动测试写死 `socks5://127.0.0.1:7897` 成功，不能证明 GUI/Sync 已带上该代理。  
Sync 代码在代理非空时是通的（C2 已证）；当前是**配置为空**，不是“中间层偷偷丢掉非空代理”。

### 2. 3761838546 当前实际 curl_cffi 请求有没有使用 `socks5://127.0.0.1:7897`？

**没有。**  
探针显示：`proxies=<<MISSING>>`，结果 `curl:(28)` ~15s。  
只有显式注入或 `SyncOptions(proxy_url=socks5…)` 时才会出现该 SOCKS5 dict。

### 3. 查看 Mod 信息为什么变卡？

`ModDetailDialog._populate` 在 **UI 线程**调用 `_ensure_offline_page` → 对 stub 同步 `archive()` → 直连 Steam timeout×2 ≈ **30 秒**，对话框/主界面卡住。

### 4. 一次查看 Mod 信息是否同步等待 Steam timeout？

**是。** 同步等待，且约 2×15s；失败后写 stub 才继续显示对话框内容流程。

### 5. stub 失败后是否导致每次查看都重新等待 15 秒？

**是（实际约 30 秒）。**  
stub 不被当作有效离线页 → 每次打开再 archive → 再 timeout → 再写 stub；且无 `next_retry_at` 一类冷却状态。

---

## 附录：关键代码位置（只读索引）

| 行为 | 位置 |
|---|---|
| 详情同步 ensure | `ui/mod_detail_dialog.py` `_populate` / `_ensure_offline_page` |
| 打开详情入口 | `ui/mod_card.py` `_open_detail` → `dialog.exec()` |
| stub 触发重抓 | `services/archive.py` `ensure_offline_page` + `is_stub_offline_page` |
| curl_cffi 入口 | `services/archive.py` `_http_get` |
| Sync 代理注入 | `services/sync.py` `_network_enrich` / `proxies_dict` |
| UI 代理读写 | `ui/sync_view.py` `network/proxy_url` |
| 库刷新 backfill | `ui/library_view.py` `refresh` → `backfill_offline_pages`（有文件则跳过，含 stub） |
