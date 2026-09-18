# Phase 17 — `readable_snapshot.py` final review (audit only)

File: `services/offline/readable_snapshot.py` (~1128 physical lines).

This phase does **not** delete, archive, or rewire the module.

## 1. Tests that use it

All live imports are in `tests/test_readable_snapshot.py` (9 tests):

| Test | What it locks |
|------|----------------|
| `test_nexus_html_parsing_extracts_main_fields` | Nexus readable parser fields |
| `test_github_html_parsing_readme_and_releases` | GitHub readable parser |
| `test_layout_css_generated_and_main_content_saved` | Writes `index.html` + local CSS |
| `test_images_not_downloaded` | Images are not fetched |
| `test_cloudflare_failure_degrades_to_fallback_page` | Cloudflare → fallback page via `run_readable_offline_snapshot` |
| `test_cloudflare_detection_helpers` | `_is_cloudflare_challenge` |
| `test_nexus_provider_is_manual_import` | Also covers `NexusManualOfflineProvider` (retarget if this module is archived) |
| `test_nexus_update_offline_does_not_scrape` | Nexus update path does not scrape |
| `test_render_includes_sections` | `render_readable_html` / `FileEntry` / `ReadablePage` |

Other mention (path string only, not an import):

| Location | Role |
|----------|------|
| `tests/contract/test_asset_lifecycle_contract.py` `ALLOWED_OTHER` | Classifies the file if it mentions `.info/assets` writes |

Estimated test LOC if the module is removed without retargeting: **~307** lines in `tests/test_readable_snapshot.py`.

## 2. Runtime import?

**None.**

| Location | Result |
|----------|--------|
| `ui/` | no references |
| `core/` | no references |
| `main.py` | no references |
| `services/offline/__init__.py` | not exported |
| Production GitHub OPEN | `GitHubBrowserSnapshot` / Playwright, not `ReadableSnapshotProvider` |

The only production-looking entry that constructs the provider is `run_readable_offline_snapshot` in the same file, called from tests.

## 3. `github.py` `readable_provider`

`GithubOfflineProvider.__init__` still accepts `readable_provider=` then immediately `del`s it (with unused `downloader`).

That kwarg is **not** a runtime call into `readable_snapshot.py`.

`tests/test_readable_snapshot.py` passes `readable_provider=` to **`run_readable_offline_snapshot`**, not to `GithubOfflineProvider`.

Leave the discarded kwarg until a product ARCHIVE/REMOVE of readable snapshot (removing it now is an API-shape change with no caller benefit).

## Decision for Phase 17

**KEEP.** No delete.

| Option | When |
|--------|------|
| **KEEP** (current) | Default. Cost is ~1.1k LOC + 9 tests. No production OPEN risk. |
| **ARCHIVE** | Product confirms readable HTML will never be an OPEN backend. Move module + test to `tools/archive/` (or `tests/archive/` for the test), drop `readable_provider` from `github.py`, drop the path from `ALLOWED_OTHER`. |
| **REMOVE** | Same confirmation plus retarget `test_nexus_provider_is_manual_import`. Do not remove while this file is the only lock of Cloudflare degrade / no-image-download. |

Class **C** in `_tmp/phase17_symbol_audit.json` — needs a product call, not a scanner delete.
