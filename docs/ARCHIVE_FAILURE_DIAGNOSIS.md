# Archive Failure Diagnosis Report

**Mod:** Pal Analyzer  
**Mod ID:** `3677771546`  
**Observed error:** `curl: (28) Connection timed out after 15003 milliseconds`  
**Date:** 2026-08-05  
**Mode:** read-only (no business-logic changes)

---

## 1. 调用链

```
ModSyncService._network_enrich_and_save()     # services/sync.py ~L440
  └─ ModSyncService._network_enrich()         # services/sync.py ~L449
        └─ OfflinePageArchiver(..., proxies=opts.proxies_dict())
              └─ OfflinePageArchiver.archive()          # services/archive.py ~L174
                    └─ _fetch_main_html(page_url)       # services/archive.py
                          └─ _http_get(page_url)        # services/archive.py ~L151
                                └─ curl_cffi.requests.get(...)   # NOT std requests / NOT subprocess
                    └─ on success: BeautifulSoup → assets → _write_atomic(index.html)
                    └─ on failure: write_fallback_page(...) → stub

Hard guarantee (if still no valid page):
  OfflinePageArchiver.ensure_offline_page()   # services/sync.py ~L520–521
    └─ archive() again (same HTTP path)
```

**HTTP 客户端：** `from curl_cffi import requests as curl_requests` → `curl_requests.get`  
**不是：** 原生 `requests.Session`、也不是 `subprocess`/`curl.exe`。

---

## 2. 网络请求配置

| 项 | 当前值 |
|----|--------|
| URL | `https://steamcommunity.com/sharedfiles/filedetails/?id=3677771546` |
| timeout | **15s**（`DEFAULT_TIMEOUT` / 同步传入的 client timeout） |
| impersonate | `"chrome131"` |
| User-Agent | Chrome 131 Windows UA |
| headers | Accept / Accept-Language=`zh-CN…` / Referer / Sec-Fetch-* / Connection |
| cookies | **无**（不携带登录 Cookie） |
| proxy | 仅当 `SyncOptions.proxy_url` 非空；**QSettings `network/proxy_url` 当前 = `''`** → `proxies=None` → **直连** |
| follow redirect | `allow_redirects=True` |
| Cloudflare / Steam challenge | **无专门处理**（CAPTCHA/challenge 未实现） |

---

## 3. 实际响应（Mod `3677771546`）

### A. 直连（与当前 GUI 同步配置一致：代理为空）

| 项 | 结果 |
|----|------|
| HTTP 状态码 | *(无响应)* |
| 响应时间 | ~15.02s |
| Content-Length | n/a |
| Content-Type | n/a |
| Steam HTML | **否** |
| 错误 | `Timeout` / `curl: (28) Connection timed out after 15014 ms` |

与 stub 内嵌错误一致。

### B. 当前 archive 同款客户端 + `socks5://127.0.0.1:7897`

| 项 | 结果 |
|----|------|
| HTTP 状态码 | **200** |
| 响应时间 | **~1.18s** |
| Content-Type | `text/html; charset=UTF-8` |
| Content-Length (hdr) | 18548（压缩前/传输；body 解码后 **89158**） |
| Steam HTML | **是**（标题含 `Pal Analyzer` / Workshop 结构） |
| Cloudflare / 403 / 空 HTML | **否** |

### 补充对照

| 通道 | 结果 |
|------|------|
| 浏览器经系统代理/梯子 | 通常可开（区域网络下直连常不通） |
| archive 直连 `curl_cffi` | **超时 (28)** |
| archive + SOCKS5 `curl_cffi` | **200 正常 HTML** |
| 空 HTML / 403 / challenge | **本次未出现** |

---

## 4. 根因判断

**E. 网络代理问题**（叠加 **A. 直连无法访问 Steam**）

- 失败点：`_http_get()` 在 **15s 内未完成 TCP/TLS 到 `steamcommunity.com`**，尚未进入 HTML 解析或保存成功页。
- **不是** timeout「略短」为主因：有代理时 ~1.2s 成功；直连 15s 仍无任何字节。
- **不是** challenge / 403 / 空 HTML / 重定向死循环。
- **不是** stub/同步判定逻辑（本次已排除）。

配置侧直接证据：`SAVED_PROXY == ''` → 同步不传代理 → 与错误复现一致。

---

## 5. 最小修复建议（仅建议，未改代码）

1. **立即操作：** 在同步中心填写并保存代理  
   `socks5://127.0.0.1:7897`  
   （本机 7897 已开；同客户端实测可拉取 Pal Analyzer 页面）
2. 再同步该 Mod（或勾选强制覆盖）。stub 识别已允许重试，关键是请求必须走代理。
3. 可选产品增强（后续）：代理为空时同步前探测 Steam，失败则明确提示「离线页将超时」；区分 `http://` 与 `socks5://`（此前 http→7897 曾 TLS 失败）。

**不要优先：** 加长 timeout、改 stub 判定、加 Cookie、改 UA——对当前 `curl:(28)` 直连超时无效。
