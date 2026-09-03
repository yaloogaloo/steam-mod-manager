# P0-2 Archive Asset Pipeline Forensic

**Mode:** code + git history + readonly `data/asset_cache` listing. **No code changes. No cache mutation. No Proxy Resolver / Backup / Identity / Conflict / Data Cleanup.**

Live evidence (user Runtime Verification, Workshop `3786388428`):

| Stage | Result |
|---|---|
| Network / proxy | **PASS** — `proxy_source=windows_lan` `http://127.0.0.1:12450` |
| Steam HTML GET | **PASS** — HTTP body 66846 bytes in ~0.86s |
| Asset pipeline | **FAIL / REGRESSION** — `assets=419.01s` |
| Backup after archive | 7ms (`copy_ms=5.2`) — not this bottleneck |
| Overall P0-2 | **BLOCKED** — not `RUNTIME_VERIFIED` |

```text
HTTP response received
        ↓
419.01s  _rewrite_and_download_assets  (ASSET PIPELINE)
        ↓
write started
        ↓
[ARCHIVE_SUCCESS] elapsed_ms=419922.0
```

---

## 1. Timing field source

Log line:

```text
Archived live Workshop page -> ... (html=%.2fs assets=%.2fs unique_assets=%s
top_ok=%s top_fail=%s workers=%s cache=%s) result=SUCCESS
```

| Field | Generator | Meaning |
|---|---|---|
| `html=` | `OfflinePageArchiver.archive` | `time.monotonic()` from just before `_fetch_main_html` until after `classify_steam_workshop_html`. **Does not include** BeautifulSoup parse / `_strip_noise`. |
| `assets=` | same | `t_assets0` immediately before `_rewrite_and_download_assets`; `assets_elapsed` immediately after it returns. **This is the 419.01s.** |
| `unique_assets=` | `stats["unique"] = len(seen)` | Unique **successfully rewritten** absolute URLs in `seen`. Failures are omitted. |
| `top_ok=` / `top_fail=` | `_tracked_download` only | Counts **top-level** CSS/font/img **jobs** (img candidates). Nested `url()` inside CSS / inline styles do **not** increment these. |
| `workers=` | constant `GLOBAL_ASSET_WORKERS` | Always `6`. Not measured concurrency. |
| `cache=` | `get_asset_cache_stats()` | Process-wide `{hit, miss}` counters. **Never reset** at archive start (tests call `reset_asset_cache_stats()`). |

**File:** `services/archive.py`  
**Function:** `OfflinePageArchiver.archive` (~1464–1522)  
**Caller:** `ensure_offline_page` → `archive`; also `ModSyncService` / UI archive action.

Clock:

```text
t_html0
  _fetch_main_html          → 0.86s (user)
  classify HTML
html_elapsed
parse + _strip_noise        → NOT in html= or assets=
t_assets0
  _rewrite_and_download_assets  → assets=419.01s
t_assets1
write index.html
```

---

## 2. EXACT_CALL_CHAIN

From `HTTP response received` (same function, next statements):

```text
OfflinePageArchiver.archive
  classify_steam_workshop_html
  _parse_workshop_html
  _strip_noise                          # scripts/iframes only; keeps CSS
  _rewrite_and_download_assets          # BLOCKING_STAGE, 419s
      discover <link stylesheet|font>, <img src/srcset>
      ThreadPoolExecutor(max_workers=GLOBAL_ASSET_WORKERS=6)
        submit _tracked_download / _download_first
        as_completed(css) → fut.result()
        as_completed(font) → fut.result()
        as_completed(img) → fut.result()
      soup.find_all(style=True)         # SERIAL on archive thread
        _rewrite_css_url_refs(download=True)
          _download_asset               # nested, not pooled
    _download_asset
      seen short-circuit
      sha1 dest name + sha256 cache key
      with _cache_lock_for(cache_key):
        _find_cached_asset              # data/asset_cache/{sha256}{ext}
        HIT: maybe _copy_file_atomic to mod .info/offline/assets/
             hit += 1
             NO HTTP
        MISS: _http_get_asset
             _GLOBAL_ASSET_SEMAPHORE.acquire()
             _perform_asset_get
               curl_requests.get(..., proxies=) timeout=15
               on proxy transport error: curl_requests.get direct timeout=15
             stream to dest + copy into asset_cache
             miss += 1  ONLY if write succeeds
             HTTP fail → return None, miss NOT incremented
        if ext == .css:
          _localize_css_file            # ALWAYS, including HIT
            _rewrite_css_url_refs(download=True)
              _download_asset           # nested, same thread, still holding CSS cache lock
  _inject_offline_banner
  _write_atomic(index.html)             # "write started"
  log_archive_success
```

---

## 3. ASSET_CACHE_SEMANTICS

**Directory:** `data/asset_cache/` via `core.paths.asset_cache_dir()`. Readonly listing this forensic: **6748 files**, **54 `.css`**.

**Key:** `sha256(absolute_url)` (`_asset_cache_key`). Dest filename in the Mod folder is `sha1(url)[:16]+ext` (different hash).

**Hit definition (code, not “skip all work”):**

`_find_cached_asset`: `data/asset_cache/{key}{preferred_ext}` exists and size>0; else `glob("{key}.*")`; else bare `{key}`.

On hit:

| Action | Happens? |
|---|---|
| Skip all processing | **No** |
| Network GET/HEAD | **No** on that URL |
| Hash of file bytes | **No** (existence + size>0 only) |
| `stat` | **Yes** |
| Copy into this Mod `offline/assets/` | **Yes** if dest missing (`_copy_file_atomic`) |
| Enter ThreadPool worker | **Yes** for top-level jobs; nested CSS `url()` is **sync on that worker** |
| Wait for worker | **Yes** (`as_completed` + `fut.result()` + pool shutdown) |
| Timeout/retry | **Not for the hit URL itself** |
| `_localize_css_file` if `.css` | **Yes — still downloads nested `url()`** |

`hit=38, miss=0` means **38 successful cache copies incremented the process counter**. It does **not** mean:

- 38 assets were the only work
- nested CSS/inline URLs were skipped
- failed GETs did not run

Failed GET: `except _CURL_HTTP_ERRORS: return None` **before** `miss += 1`. Those URLs are invisible in `cache=`.

`unique_assets=38` = `len(seen)` successes only.

`top_ok=49` vs `unique=38`: 49 successful **top-level tracked** downloads; 11 were `seen` duplicates. Nested CSS/inline successes increment `seen`/`hit` but **not** `top_ok`. Nested failures increment **nothing**.

**Are the 38 hits a complete fast path?** **No.** CSS hits still recurse. Inline `style` rewrite still runs after the pool with `download=True`.

---

## 4. WORKER_WAIT_SEMANTICS

`workers=6` is **`ThreadPoolExecutor(max_workers=6)`** plus **`threading.Semaphore(6)`** on **HTTP GET only** (`_http_get_asset`). Not QThreadPool.

| Primitive | Role |
|---|---|
| `ThreadPoolExecutor` | Schedule top-level CSS/font/img jobs |
| `as_completed` + `fut.result()` | Archive thread waits for each wave (CSS, then fonts, then images). Fonts/images were already submitted, so they run overlapping CSS. |
| `with ThreadPoolExecutor` exit | `shutdown(wait=True)` |
| `_GLOBAL_ASSET_SEMAPHORE` | Caps concurrent **asset HTTP**, not cache copies |
| `_cache_lock_for(url)` | One download/copy per cache key; **CSS localization runs inside this lock** |
| `seen_lock` | Dedup map |
| Nested `_download_asset` from CSS/inline | **Not submitted to the pool** — sync `for url() in css` on that thread |

Cache hits do **not** take the HTTP semaphore. Nested misses **do**, one at a time per worker.

There is **no** per-asset INFO log (`[ARCHIVE ASSET]` is `logger.debug`). This run cannot show per-URL elapsed without DEBUG.

---

## 5. TIMEOUT_RETRY_ANALYSIS

| Knob | Value | Applies to |
|---|---|---|
| `DEFAULT_TIMEOUT` | **15** | HTML and asset `kwargs["timeout"]` |
| HTML limiter interval | 3s | HTML only — **not assets** |
| HTML 429 retry | 3 attempts, 8s backoff | HTML only |
| `MAIN_PAGE_RETRIES` | 2 | HTML `_fetch_main_html` |
| Asset retry loop | **None** (no for-attempt around asset GET) | |
| Asset proxy fallback | **Yes** | `_perform_asset_get`: proxy GET, on `_PROXY_TRANSPORT_ERRORS` (includes timeout / curl 28) → **second GET with no proxy**, same 15s timeout |

So a **hanging CDN URL** can cost **up to ~30s** (15 proxy + 15 direct), logged only at DEBUG as `[ARCHIVE ASSET] proxy failed, fallback direct`.

`419.01 / 38 ≈ 11.03` is **not** a proven per-success-asset timeout. The 38 are **hits** (no HTTP). The 419s is wall time of `_rewrite_and_download_assets`, which includes **uncounted failed nested GETs**.

Arithmetic **consistent with code**, not proven by this run’s INFO logs:

- ~28 serial 15s timeouts → ~420s
- ~14 serial 15+15 proxy-then-direct timeouts → ~420s

Need DEBUG `[ARCHIVE ASSET] elapsed=` or INFO per nested URL to **prove** which. **Do not treat 11.03s as the mechanism.**

Largest files in `data/asset_cache` `.css` (readonly): ~8 remote `url()` each — not enough alone. Steam HTML **inline `style=url(...)`** (serial, post-pool) plus **several stylesheets × nested fonts** can still produce dozens of extra GETs. This forensic did not parse the live 3786388428 HTML (not required to establish the hole in cache stats).

---

## 6. Cache location (unchanged, not deleted)

- Path: `data/asset_cache/`
- Hit: file exists size>0
- Hit does not fetch **that** URL
- Hit **does** still copy + CSS recurse
- 38 hits this process; **cannot** assert all 38 were this page’s only work

---

## 7. RECENT_REGRESSION_DIFF

Git: coarse squash (`2fd2138` → `894d068` “multi-platform” → `1b4a623` “更新一次dev分支稳定代码”). Current cache-hit CSS path is in `1b4a623`.

| Era | Asset pipeline |
|---|---|
| `2fd2138` first commit | `_rewrite_and_download_assets` **serial** `for link in soup`; `_download_asset` already exists. **Already materializes assets**, not HTML-only. |
| `894d068`+ | `ThreadPoolExecutor` + `GLOBAL_ASSET_WORKERS=6` + `data/asset_cache` + `_perform_asset_get` proxy→direct + CSS localize **after hit or miss** |

This 419s is **not** explained by “HTML-only archive recently started downloading assets.” Materialize was always there.

**Why it feels like a regression now:** previous Runtime forensic for `3786388428` failed at **HTML curl (28)** (~15s) and never entered `_rewrite_and_download_assets`. Proxy fix made HTML succeed in 0.86s, **exposing** the asset stage that used to be unreachable.

Semantic traps introduced with cache/workers (still present):

1. Cache **hit still `_localize_css_file`**.
2. Asset **proxy timeout then full direct timeout**.
3. Failed asset GET **omitted from `miss`**.
4. `workers=6` logged as a constant; nested work is often **serial on one worker / the archive thread**.

---

## 8. WHY_419_SECONDS / ROOT_CAUSE_CONFIDENCE

**BLOCKING_STAGE:** `OfflinePageArchiver._rewrite_and_download_assets` (`services/archive.py`).

**ROOT_CAUSE_CONFIDENCE:**

| Claim | Confidence |
|---|---|
| 419s is this function, not Steam HTML / proxy discovery / backup | **High** (user clocks + `t_assets0` wrapping) |
| `cache hit=38` does not mean “38 assets, zero network, skip wait” | **High** (code) |
| CSS hit still recursively `_download_asset` for `url()` | **High** (code) |
| Inline `style` rewrite is serial `download=True` after the pool | **High** (code) |
| Failed nested GET does not increment `miss` | **High** (code) |
| Asset timeout=15; proxy failure retries direct another 15s | **High** (code) |
| 419s **is** N serial (or proxy+direct) nested timeouts | **Medium** — matches 15/30s arithmetic; **not** logged at INFO this run |
| Disk copy of 38 hits took 419s | **Low** — 7k cache files, copies are small CSS/images; glob is not 11s×38 on NTFS without extra stalls |
| Need more threads | **Not supported** — nested path ignores the pool; adding workers does not fix CSS/inline serial GET |

**WHY_419_SECONDS (best supported story):** HTML succeeded; asset stage always copies/localizes even on cache hits; nested/inline URLs **not** in the 38-hit set still `curl_cffi` GET with 15s timeout (and possibly a second 15s direct fallback); those failures are **silent at INFO** and **absent from `cache={'hit':38,'miss':0'}`**. Wall clock ~419s is that wait, not Steam HTML and not backup.

---

## 9. MINIMAL_FIX_DIRECTION (not implemented)

Do **not** multithread reconcile. Do **not** raise `GLOBAL_ASSET_WORKERS` first.

Smallest likely fixes (later P0-2 round, needs new Runtime Verification):

1. **Cache hit + CSS:** do not re-fetch nested `url()` that are already in `asset_cache` / `seen`; optionally skip re-localizing unchanged CSS.
2. **Do not proxy→direct fallback on asset timeout** (or use a short connect timeout); HTML already proved the proxy.
3. **Count failures** (`miss` vs `fail`) and INFO-log `[ARCHIVE ASSET] url= elapsed_ms= cache=hit|miss|fail`.
4. **Do not treat `hit==unique` as fast path** in product logs.

---

Not `RUNTIME_VERIFIED`. Network success ≠ acceptable Archive performance.

---

## 10. FIX ROUND (2026-09-03) — implemented + live re-verify

Minimal changes in `services/archive.py` only (plus tests). `GLOBAL_ASSET_WORKERS` still 6. HTML `_perform_get` proxy→direct unchanged.

| Item | Result |
|---|---|
| Targeted tests | **94 passed** (`test_archive_asset_pipeline`, `test_asset_cache`, proxy/rate-limit/html/governance/offline/modio archive tests) |
| Live mod | `3786388428` Duck Tracks, existing `.info`, overwrite. Identity unchanged (1766 rows). |
| HTML | HTTP 200, proxy `http://127.0.0.1:12450` `windows_lan` |
| `.info/index.html` | written, `is_valid_steam_workshop_page` **True** |
| Run 1 assets | **93.64s** (was **419.01s**): −325.37s, **4.47×** |
| Run 1 stats | hit=38 miss=0 fail=257 top_ok=49 top_fail=0 nested_ok=0 nested_fail=257 unique=38 workers=6 |
| Run 1 fail type | nested CSS `url()` **HTTP 404** (~0.7–3s), **not** 15s timeout, **no** direct retry |
| Run 2 assets | **0.16s** hit=38 fail=0 nested_fail=0 (localized CSS skipped) |
| New identity | **No** |

P0-2 asset-pipeline 419s blocker is **RUNTIME_VERIFIED closed**. Residual first-localize Steam CSS-relative 404s are a separate optional follow-up; do not raise worker count for them.

