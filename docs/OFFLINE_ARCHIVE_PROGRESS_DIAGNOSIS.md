# Offline Archive Queue Progress Stuck at 0% — Diagnosis

**Mode:** read-only analysis (no code changes applied).  
**Date:** 2026-08-05  
**Symptom:** Click「同步 Steam 离线网页」→ UI progress bar stays at **0%**, while `services.archive` logs show proxy OK, `last_status=200`, and `request_count` climbing (24 → 25 → 26…).

---

## 1. Current flow

### Entry points

| Step | File | Function / symbol |
|------|------|-------------------|
| Button | `ui/sync_view.py` | `SyncCenterView.start_offline_pages_sync` |
| Worker thread | `ui/sync_thread.py` | `OfflinePagesSyncWorker.run` |
| Service queue | `services/sync.py` | `ModSyncService.sync_offline_pages_only` |
| Per-mod archive | `services/archive.py` | `OfflinePageArchiver.ensure_offline_page` → `archive` → `_http_get` |

### Flowchart

```text
UI: 「同步 Steam 离线网页」 clicked
        │
        ▼
SyncCenterView.start_offline_pages_sync()
        │  progress_bar.setValue(0)
        │  OfflinePagesSyncWorker(...)
        │  progress_changed ──connect──► _on_sync_progress
        │  sync_finished    ──connect──► _on_offline_sync_finished
        ▼
OfflinePagesSyncWorker.start()  (QThread)
        │
        ▼
OfflinePagesSyncWorker.run()
        │
        ▼
ModSyncService.sync_offline_pages_only(on_progress=worker._on_progress)
        │
        │  for each mod in library (serial):
        │     progress("offline", current, total, message)
        │            │
        │            ▼
        │     OfflinePagesSyncWorker._on_progress
        │            │
        │            ▼
        │     percent = _phase_to_percent("offline", current, total)
        │            │
        │            ▼
        │     progress_changed.emit(percent, message)   ← Queued to UI thread
        │            │
        │            ▼
        │     SyncCenterView._on_sync_progress
        │            │
        │            ▼
        │     progress_bar.setValue(percent)
        │     status_label.setText(message)
        │
        │     ensure_offline_page() / archive()
        │            │
        │            ▼
        │     many Steam GETs (HTML + CSS + images)
        │     request_count += 1 per GET   ← NOT reflected in UI percent
        │
        ▼
progress("done", ...) → percent=100
sync_finished.emit(result)
        │
        ▼
_on_offline_sync_finished → progress_bar.setValue(100) + summary dialog
```

There is **no separate Qt “archive queue” object**. The “queue” is the serial `for` loop inside `sync_offline_pages_only`.

---

## 2. Check results

### Check 1 — Entry

Confirmed:

```text
UI button
  → start_offline_pages_sync()
  → OfflinePagesSyncWorker (QThread)
  → sync_offline_pages_only()
  → ensure_offline_page() / archive()
```

### Check 2 — Progress events

Backend **does** send progress.

`sync_offline_pages_only` calls `on_progress(phase, current, total, message)` with:

| phase | when | typical `current` |
|-------|------|-------------------|
| `"offline"` | start / per mod / downloading | often `index - 1` |
| `"offline"` | after skip / success / fail / 429 | `index` |
| `"done"` | finished | `total` |

Worker maps that to a percentage:

```python
# ui/sync_thread.py — _phase_to_percent
"offline": (0, 95)
percent = int(0 + 95 * (current / total))
```

**Important:** progress is **per Mod**, not per HTTP request.  
`request_count` in archive logs counts HTML/CSS/image GETs inside one mod; UI `current` only advances when that mod finishes (or is skipped).

While downloading mod `index`, the service repeatedly reports:

```text
progress("offline", index - 1, total, "正在下载离线页面: …")
```

So for the **first** mod that needs a network archive:

```text
current = 0  →  percent = int(95 * 0 / total) = 0
```

All asset GETs (`request_count` 1…N) happen in this window → bar stays at **0%**.

### Check 3 — UI listener

`SyncCenterView` uses **one** `QProgressBar` for both full sync and offline sync.

Offline worker is wired correctly:

```python
worker.progress_changed.connect(self._on_sync_progress)
worker.sync_finished.connect(self._on_offline_sync_finished)
```

`_on_sync_progress` updates `progress_bar` and `status_label`.  
This is **not** “listening only to full sync.” Offline progress is connected.

### Check 4 — Finish without UI refresh?

On success, `_on_offline_sync_finished` forces `progress_bar.setValue(100)` and shows a summary dialog.  
Completion path is wired. The stuck-at-0% symptom matches **long first-mod download**, not a missing finish handler (unless the run never returns).

Task is **not blocked**: archive logs with `last_status=200` and rising `request_count` prove GETs are progressing (single-flight + ≥3s interval makes one mod slow).

---

## 3. Root cause

**选择：D. 其他**

更精确：

> 后台有发 progress，UI 也有监听；但百分比按「已完成 Mod 数 / 总数」计算，下载进行中故意报 `current = index - 1`。第一个需要抓取的 Mod 整段网络阶段（含大量 CSS/图片请求）`current` 一直为 0 → `_phase_to_percent` 一直算成 0%。`request_count` 上涨与 UI 百分比无关。

排除：

| 选项 | 为何不是 |
|------|----------|
| A. 后台没有发送 progress | `sync_offline_pages_only` 多次调用 `on_progress`；worker 会 `emit` |
| B. 后台发送但是 UI 未监听 | `progress_changed → _on_sync_progress` 已连接 |
| C. 任务实际上阻塞 | archive 日志 `200` + `request_count` 递增，说明在跑 |

### Why it looks “stuck” especially now

After the architecture change, offline sync is a dedicated low-frequency job:

- global single-flight
- ≥3s between Steam GETs
- one Workshop page ⇒ HTML + many assets ⇒ dozens of requests

So the first real archive can sit in `current=0` for a long time while the bar shows **0%**. Status text *may* still update to「正在下载离线页面: …」; the percentage digit does not.

Example:

```text
total = 5 mods needing pages
index = 1, downloading → current=0 → 0%
  request_count=1..40 (all inside mod 1) → still 0%
index = 1 done → current=1 → 19%
```

If only **one** mod needs archive: bar stays **0%** until `"done"` / finish handler jumps to **100%**.

---

## 4. Minimal fix (do not touch archive / Limiter / Steam GET)

**Constraint:** do not modify `services/archive.py`, Steam request logic, or `SteamArchiveLimiter`.

### Recommended (smallest, UI/sync mapping only)

**Option 1 — Credit “in progress” for the active mod** (`services/sync.py` only)

When starting / during download of mod `index`, report:

```text
current = index          # or index - 0.5 if you switch to float later
```

instead of `index - 1`, so the first mod immediately maps to:

```text
percent ≈ int(95 * index / total)   # e.g. 1 mod → ~95%, 10 mods → ~9%
```

Still no per-asset granularity, but the bar leaves 0% as soon as work starts.

**Option 2 — Offline-specific percent helper** (`ui/sync_thread.py` only)

In `_phase_to_percent` (or an offline-only helper):

```text
# treat current as completed; add a small “working” floor when phase==offline and current < total
if phase == "offline" and 0 <= current < total:
    percent = max(1, int(95 * current / total))   # never show 0 while offline phase active
```

Or:

```text
ratio = (current + 0.5) / total   # half-credit for the mod currently running
```

**Option 3 — Status clarity (optional, UI only)**

Keep bar formula, but make `status_label` always show `已完成 current/total · 当前：name` (already mostly present). Users then see activity even at 0%. Does not fix the percent digit alone.

### Not required

- No change to archive HTTP / proxy / cookie / limiter
- No new queue infrastructure
- No need to emit progress per `request_count` unless you later want asset-level progress (would need a callback from archive — out of scope / forbidden by “don’t modify archive.py”)

### Suggested verify after fix

1. Library with 1 mod missing offline page → bar leaves 0% shortly after click (e.g. ~95% or ≥1%).  
2. Library with N mods → bar steps as each mod starts/finishes.  
3. Finish still jumps to 100% and summary dialog.  
4. Confirm `request_count` growth unchanged (archive untouched).

---

## 5. Summary

| Item | Finding |
|------|---------|
| Entry | `sync_view.start_offline_pages_sync` → `OfflinePagesSyncWorker` → `sync_offline_pages_only` → `ensure_offline_page` |
| Progress emitted? | Yes (`phase/current/total/message` → `%` via `_phase_to_percent`) |
| UI listens? | Yes (`progress_changed` → shared `progress_bar`) |
| Finish refresh? | Yes (`_on_offline_sync_finished` → 100%) |
| Root cause class | **D. 其他** — percent stuck at 0 because `current=0` for entire first-mod download while asset GETs inflate `request_count` |
| Minimal fix | Adjust offline `current` / `_phase_to_percent` mapping in `sync.py` and/or `sync_thread.py` only |
