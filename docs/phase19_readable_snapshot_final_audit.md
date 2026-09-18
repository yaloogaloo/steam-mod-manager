# Phase 19 — `readable_snapshot.py` final audit

File: `services/offline/readable_snapshot.py` (~1128 physical lines).

This phase does **not** delete, archive, or rewire the module.

## 1. Runtime callers

| Location | Import / call |
|----------|----------------|
| `main.py` | none |
| `ui/` | none |
| `core/` | none |
| OPEN (`ensure_live_offline_openable` / `cache/offline_view`) | none |
| Production services (GitHub / Nexus / Steam / mod.io offline) | none |
| `services/offline/__init__.py` | not exported |

Production GitHub OPEN uses `GitHubBrowserSnapshot` (Playwright `page.content()`), not `ReadableSnapshotProvider`.

`GithubOfflineProvider.__init__` still accepts `readable_provider=` then immediately `del`s it. That is **not** a call into this module.

## 2. Current callers

**Tests only.**

| Location | Role |
|----------|------|
| `tests/test_readable_snapshot.py` | 9 tests: parsers, CSS write, no image download, Cloudflare degrade, `NexusManualOfflineProvider` overlap, render sections |
| `tests/contract/test_asset_lifecycle_contract.py` `ALLOWED_OTHER` | Path **string** only (write-path classification) |

No docs/tools import the module as a library. Mentions exist only in Phase 15–18 governance docs.

## 3. Provider contract

`ReadableSnapshotProvider` is an unused pipeline: fetch HTML → extract main content → write `index.html` + `style.css`, no JS/image mirror.

It is **not** on the frozen OPEN path (`manifest → Asset Store → cache/offline_view`).

## 4. Decision this phase

Confirmed tests-only → **do not delete**.

Recorded **ARCHIVE candidate** (not executed):

| Item | Action when product confirms “never an OPEN backend” |
|------|------------------------------------------------------|
| `services/offline/readable_snapshot.py` | Move to `tools/archive/` |
| `tests/test_readable_snapshot.py` | Move with it; retarget `test_nexus_provider_is_manual_import` if needed |
| `services/offline/github.py` `readable_provider=` | Drop discarded kwarg (and unused `downloader=` if still `del`’d) |
| `ALLOWED_OTHER` path string | Remove |

Until that product call: **KEEP** in `services/offline/`.
