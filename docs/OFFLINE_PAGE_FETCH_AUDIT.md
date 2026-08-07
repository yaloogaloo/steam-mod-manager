# Offline Page Fetch Audit (Read-Only)

**Date:** 2026-08-05  
**Scope:** Why `archive` cannot obtain the live Steam Workshop HTML  
**Out of scope:** stub validity / skip logic (already fixed)

---

## 1. Archive main call chain

```
ensure_offline_page(info_dir, published_file_id)
  └─ if index exists, size>0, not stub → return
  └─ else archive(...)

archive(published_file_id, info_dir, overwrite=True, metadata=…)
  └─ page_url = WORKSHOP_PAGE_URL.format(id=…)
  └─ _fetch_main_html(page_url)
        └─ _http_get(page_url)          # curl_cffi, impersonate=chrome131
              └─ response.raise_for_status()
              └─ return response.text
  └─ BeautifulSoup + asset rewrite
  └─ _write_atomic(info_dir/index.html, html)
  └─ on any exception → write_fallback_page(...)  # stub
```

| 步骤 | 函数 | 文件 | 关键参数 |
|------|------|------|----------|
| 入口（硬保底） | `OfflinePageArchiver.ensure_offline_page` | `services/archive.py` ~L220 | `info_dir`, `published_file_id` |
| 主抓取 | `OfflinePageArchiver.archive` | `services/archive.py` ~L174 | `overwrite=True`（同步主路径） |
| HTTP | `OfflinePageArchiver._http_get` | `services/archive.py` ~L151 | `impersonate="chrome131"`, `timeout`, `headers=_BROWSER_HEADERS`, optional `proxies` |
| 客户端 | `curl_cffi.requests.get` | 非原生 `requests.Session` | 同上 |
| 保存成功页 | `_write_atomic` | `services/archive.py` | `.info/index.html` |
| 保存失败页 | `write_fallback_page` | `services/archive.py` | stub HTML + `error=str(exc)` |

同步侧调用（参考，非本次改动点）：

- `ModSyncService._network_enrich` → `OfflinePageArchiver(..., proxies=opts.proxies_dict()).archive(...)`
- `proxies_dict()` 来自 UI `proxy_url`；**空字符串 → `proxies=None` → 直连**

---

## 2. Live request test (failed Mod)

**Sample Mod ID:** `3751485906` (Always Win Fishing)  
**URL:** `https://steamcommunity.com/sharedfiles/filedetails/?id=3751485906`

### Direct (no proxy) — current sync config (`SAVED_PROXY == ''`)

| 项 | 结果 |
|----|------|
| HTTP status | *(no response)* |
| Error | `curl: (28) Connection timed out after ~15–20s` |
| response headers | n/a |
| content-type | n/a |
| response size | 0 |
| 前 500 字符 | n/a |

**判断：D / E — 连接错误；被网络层阻断（超时），未拿到任何 Steam HTML。**

### Via `socks5://127.0.0.1:7897` (port open)

| 项 | 结果 |
|----|------|
| HTTP status | **200** |
| content-type | `text/html; charset=UTF-8` |
| server | `nginx` |
| response size | **~88141** |
| Cloudflare / captcha | **否** |
| 前 500 字符 | `<!DOCTYPE html>… Steam 创意工坊::Always Win Fishing … motiva_sans.css …` |

**判断：A — Steam 返回正常 HTML。**

---

## 3. Saved offline file

**Path:**  
`mod/Palworld/Always Win Fishing/.info/index.html`

| 项 | 值 |
|----|-----|
| 文件大小 | **1336** bytes |
| 类型 | **stub**（`Offline (stub)`） |
| 内嵌错误 | `curl: (28) Connection timed out after 15011 milliseconds` |
| Cloudflare | 否 |
| Steam 正常页 | 否（无 `workshopItemTitle` / `smm-offline-banner`） |

Library scan at audit time: **70 stubs / 0 live pages**.

---

## 4. Downloader configuration audit

| 配置 | 当前值 |
|------|--------|
| HTTP 库 | `curl_cffi.requests`（**不是** std `requests`） |
| Session | 无持久 Session；每次 `_http_get` 独立 `get` |
| impersonate | `"chrome131"`（硬编码） |
| User-Agent | Chrome 131 Windows UA |
| Accept / Accept-Language | 浏览器风格；`zh-CN,zh;q=0.9,en;q=0.8` |
| Cookie | **不发送**（无登录 Cookie） |
| timeout | 默认 **15s**（同步侧可用 client.timeout） |
| proxy | **仅当** `SyncOptions.proxy_url` 非空；QSettings `network/proxy_url` **当前为空** |
| retry | 主 HTML：`MAIN_PAGE_RETRIES = 2` |

**Steam 对 UA / Cookie / Accept-Language：**  
带代理时，现有 UA + chrome131 impersonate + **无 Cookie** 即可拿到完整 Workshop HTML → 说明当前失败**不是** Cookie/验证码/Cloudflare 拦截，而是**无代理时根本连不上**。

---

## 5. Browser vs curl vs Python

| 通道 | 同一 URL | 结果 |
|------|----------|------|
| 浏览器（用户侧，经系统代理/梯子时） | 通常可开 | （本审计未自动化浏览器；与此前成功镜像一致） |
| `curl.exe` 直连 | 超时 `curl: (28)` | 失败 |
| Python `curl_cffi` 直连 | 超时 `curl: (28)` | 失败 |
| Python `curl_cffi` + `socks5://127.0.0.1:7897` | 200 / ~88KB HTML | **成功** |

**差异：** 成败唯一稳定变量是 **是否走本地 SOCKS5 代理**，不是 UA/impersonate/HTML 解析。

---

## 6. Root cause

**C. 代理/网络问题**

真实失败位置：

```
_http_get(Steam Workshop URL)
  → 直连 steamcommunity.com
  → TCP/TLS 超时 curl:(28) 或历史上 reset curl:(35)
  → 从未进入「正常 HTML 解析 / 资源下载 / 成功保存」
  → write_fallback_page → stub
```

**不是：** stub 判定、保存逻辑写错、Steam 返回 Cloudflare/验证码、archive 解析损坏。

证据链：

1. QSettings 代理为空 → 同步传 `proxies=None`  
2. stub 内嵌错误为 **timeout (28)**，与直连探测一致  
3. 同一 URL + 同一 downloader + SOCKS5 → **A 正常 HTML**

---

## 下一步最小修复方向（建议，本次未改代码）

1. **操作层（立即）**  
   - 同步中心填写并保存：`socks5://127.0.0.1:7897`  
   - 再同步（stub 已可重试）；必要时勾选强制覆盖  

2. **产品层（可选、最小）**  
   - 同步开始时若 `proxy_url` 为空且探测 Steam 超时，UI 明确警告「离线页将失败」  
   - 或默认记住上次可用代理；区分 `http://` vs `socks5://`（本机 7897 上 http 曾 TLS 失败）  

3. **不要优先做的事**  
   - 改 stub 判定、改 BeautifulSoup、加 Cookie、换 UA —— 对当前根因无效
