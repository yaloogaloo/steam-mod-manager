# Archive Proxy Flow Diagnosis

**Date:** 2026-08-05  
**Mode:** read-only (no production code changes)  
**Question:** Why does Steam archive still go direct when the Sync Center has a proxy field?

---

## Runtime snapshot (this machine)

```
QSettings:
  org/app = SteamModManager / WorkshopLibrary
  key     = network/proxy_url
  file    = HKEY_CURRENT_USER\Software\SteamModManager\WorkshopLibrary
  proxy   = ''                    ← EMPTY

sync (if started now):
  proxy_url     = ''
  proxies_dict  = None

OfflinePageArchiver:
  _proxies      = None

_http_get / curl_cffi:
  proxies kwarg = (omitted) → DIRECT CONNECT
```

Simulated with `socks5://127.0.0.1:7897` in `SyncOptions.proxy_url` (code path intact):

```
QSettings:              (would save same string)
sync.proxy_url:         socks5://127.0.0.1:7897
OfflinePageArchiver:    {'http': 'socks5://…7897', 'https': 'socks5://…7897'}
_http_get:              same dict
curl_cffi proxies:      same dict
```

**Conclusion of runtime check:**  
Wire from `SyncOptions.proxy_url` → Archiver → `_http_get` → `curl_cffi` **works when non-empty**.  
Current failure is because **persisted / used proxy value is empty**, so archive intentionally omits `proxies`.

---

## 1. Current proxy configuration source (layer by layer)

| # | Layer | File | Function / site | Variable | Type | Current value |
|---|-------|------|-----------------|----------|------|---------------|
| 1 | UI input | `ui/sync_view.py` | `SyncCenterView._build_ui` | `self.proxy_edit` | `QLineEdit` | (UI text; persisted value below) |
| 2 | Read helper | `ui/sync_view.py` | `proxy_url()` | return of `proxy_edit.text().strip()` | `str` | `''` if empty |
| 3 | Save | `ui/sync_view.py` | `_save_proxy_setting` | `_SETTING_PROXY` → QSettings | `str` | written on `editingFinished` **and** `start_sync` |
| 4 | Restore | `ui/sync_view.py` | `_restore_proxy_setting` | `QSettings.value(...)` | `str` | only fills UI if truthy |
| 5 | Storage | Windows Registry via Qt | `QSettings("SteamModManager","WorkshopLibrary")` | key `network/proxy_url` | `str` | **`''`** |
| 6 | Sync start | `ui/sync_view.py` | `start_sync` | `SyncOptions(proxy_url=self.proxy_url())` | `SyncOptions` | `proxy_url=''` |
| 7 | Worker | `ui/sync_thread.py` | `SyncWorker.__init__` / `run` | `self.options` | `SyncOptions` | passed through unchanged |
| 8 | Service | `services/sync.py` | `ModSyncService.sync(options)` | `opts` | `SyncOptions` | `opts.proxy_url` |
| 9 | Convert | `services/sync.py` | `SyncOptions.proxies_dict()` | return | `dict[str,str] \| None` | **`None`** when empty |
| 10 | Enrich | `services/sync.py` | `_network_enrich` | `proxies = opts.proxies_dict()` | same | **`None`** |
| 11 | Archiver ctor | `services/sync.py` L506–509 | `OfflinePageArchiver(proxies=proxies)` | `proxies` kwarg | `dict \| None` | **`None`** |
| 12 | Stored | `services/archive.py` `__init__` | `self._proxies` | `dict \| None` | **`None`** |
| 13 | HTTP | `services/archive.py` `_http_get` | `kwargs["proxies"]` only if `self._proxies` | — | **omitted** |
| 14 | Client | `curl_cffi.requests.get` | `proxies=` | — | **direct** |

`archive()` itself does **not** take a proxy argument; it uses instance `self._proxies` set at construction.

---

## 2. QSettings key / save / load

| Item | Value |
|------|--------|
| Org | `SteamModManager` |
| App | `WorkshopLibrary` |
| Key | **`network/proxy_url`** |
| Storage (Windows) | `HKEY_CURRENT_USER\Software\SteamModManager\WorkshopLibrary` |
| Save triggers | `proxy_edit.editingFinished` → `_save_proxy_setting`; also `start_sync` → `_save_proxy_setting` |
| Load trigger | `__init__` → `_restore_proxy_setting` (only if saved non-empty) |

There is **one** proxy setting in the app for Sync Center → Archive. No second Steam-only proxy store.

---

## 3. Sync read / Archiver receive / `_http_get` use

| Question | Answer |
|----------|--------|
| Does `sync()` read QSettings itself? | **No.** It only uses `SyncOptions` from the worker. |
| Who reads QSettings? | UI only (`_restore` / `_save`). Sync gets the value via `start_sync` → `proxy_url=self.proxy_url()`. |
| Does `_network_enrich` pass proxy? | **Yes**, when `opts.proxies_dict()` is non-`None`: `OfflinePageArchiver(..., proxies=proxies)`. |
| Does `archive()` receive proxy? | Indirectly via ctor `self._proxies`. |
| Does `_http_get` use it? | **Yes**, `if self._proxies: kwargs["proxies"] = self._proxies`. |
| Format conversion? | **None** beyond wrapping: `{"http": url, "https": url}` — no `socks5`→`http` rewrite. |

---

## 4. Breakpoint classification (A–H)

| Code | Meaning | Verdict for GUI sync path |
|------|---------|---------------------------|
| **A** | UI shows proxy but QSettings empty | **PRIMARY (current machine):** QSettings `network/proxy_url` is `''`. Placeholder text (`http://127.0.0.1:7890`) can look like a filled value; empty field → empty save. |
| B | QSettings OK, worker not reading | **No** — worker receives `SyncOptions.proxy_url` from UI at start; does not re-read QSettings. |
| C | Worker has proxy, not passed to Archiver | **No** for `_network_enrich` main archive path (`proxies=proxies`). |
| D | Archiver ctor missing proxy on `archive()` call | **N/A** — `archive()` uses instance state; ctor is where proxy is set. |
| E | Lost in `_http_get` | **No** — guarded pass-through is correct. |
| F | curl_cffi ignores proxies | **No** — diagnosed earlier: same client + proxies works. |
| G | Bad format rewrite | **No** on happy path; risk if user types `http://127.0.0.1:7897` (TLS fail on that port) instead of `socks5://…`. |
| **H** | Side paths not on Sync Center proxy | **Yes, secondary:** `backfill_offline_pages()`, `mod_detail_dialog`, CLI `sync`/`backfill` construct `OfflinePageArchiver()` **without** proxies. Library refresh backfill can rewrite stubs **without** proxy even if Sync Center has one. |

**Why “Sync Center has proxy” but Steam archive still goes direct?**

On this install, **the value that actually reaches sync is empty** (`QSettings` / `proxy_url` = `''`).  
The pipeline then correctly treats “no proxy” as direct connect → `curl:(28)`.

If the user *believes* the field shows `socks5://127.0.0.1:7897` but registry is empty: that is **A** (not saved / not restored / empty line vs placeholder).  
If they sync from CLI / library backfill / detail dialog: that is **H**.

The Sync Center → `_network_enrich` → Archiver chain is **not broken in code**; it is **unfed** (empty string) or **bypassed** (side entrypoints).

---

## 5. Where to fix (minimal; design only)

### Prefer (existing unified path)

Keep single source: Sync Center `network/proxy_url` → `SyncOptions.proxy_url` → `proxies_dict()` → `OfflinePageArchiver(proxies=…)`.

**Do not** hardcode `socks5://127.0.0.1:7897`.

### Minimal product fixes (choose as needed)

1. **Ops / UX (zero code):**  
   Type and leave focus / click Sync so `_save_proxy_setting` runs; confirm registry value is non-empty. Use **`socks5://127.0.0.1:7897`** (not bare `http://` on this port).

2. **Smallest code hardening for “empty save” UX** (if UI appears filled but isn’t):  
   - File: `ui/sync_view.py`  
   - Clearer placeholder vs value; optional status line showing “当前代理: (未设置)” / actual URL before sync.  
   - Ensure `start_sync` already saves (it does); maybe also save on `textChanged` debounce.

3. **Close side-path gap H** (same proxy, no second system):  
   - Files: `services/archive.py` `backfill_offline_pages`, `ui/library_view.py`, `ui/mod_detail_dialog.py`, `main.py` CLI  
   - Pass `proxies=` from the same QSettings key / `SyncOptions.proxies_dict()` into every `OfflinePageArchiver(...)`.

4. **Minimal sync-path change if somehow Options lose proxy:**  
   - File: `services/sync.py` `_network_enrich`  
   - Already passes `proxies=opts.proxies_dict()` — **no change needed** if UI supplies `proxy_url`.

### Answer: “修改哪一个最小位置，就能让 OfflinePageArchiver 使用同步中心代理？”

**For the normal “开始同步” path:**  
No Archiver change required. Ensure **`SyncOptions.proxy_url` is non-empty at `ui/sync_view.py` `start_sync`** (i.e. `self.proxy_url()` / QSettings `network/proxy_url`). That single value already flows to `_http_get`.

**If the goal is “even library refresh / detail re-archive use the same proxy”:**  
Minimum code touch: pass QSettings `network/proxy_url` into `OfflinePageArchiver(proxies=…)` in `backfill_offline_pages` / `mod_detail_dialog` (side path **H**).

---

## 6. Checklist answers (required)

1. **代理配置来源:** Sync Center `QLineEdit` → QSettings → `SyncOptions.proxy_url`  
2. **QSettings key:** `network/proxy_url`  
3. **保存位置:** `HKEY_CURRENT_USER\Software\SteamModManager\WorkshopLibrary`  
4. **sync 读取位置:** does not read QSettings; receives `options.proxy_url` from `SyncWorker`  
5. **OfflinePageArchiver 接收位置:** `_network_enrich` ctor `proxies=opts.proxies_dict()`  
6. **`_http_get` 使用位置:** `if self._proxies: kwargs["proxies"]=self._proxies`  
7. **curl_cffi 最终 proxies:** currently **omitted** (`None`); with configured URL → `{"http": "<url>", "https": "<url>"}`  
8. **断点位置:** **A** (empty persisted/used value) + secondary **H** (backfill/detail/CLI without proxy)  
9. **最小修改文件:** ops: none; UX/side-path: `ui/sync_view.py` and/or `services/archive.py` `backfill_*` + callers  
10. **最小修改方案:** feed non-empty `proxy_url` through existing `SyncOptions`; optionally wire same dict into side entrypoints — **do not** invent a second proxy system or hardcode 7897
