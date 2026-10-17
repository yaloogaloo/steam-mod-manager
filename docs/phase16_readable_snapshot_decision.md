# Phase 16 — `readable_snapshot.py` status (no deletion)

File: `services/offline/readable_snapshot.py` (~1128 physical lines).

This phase does **not** delete, archive, or wire the module.

## 1. Production caller?

**No live caller.**

| Location | Role |
|----------|------|
| `tests/test_readable_snapshot.py` | Direct import and behavior tests |
| `services/offline/github.py` | Constructor kwarg `readable_provider` is accepted then `del`’d — discarded injectable, not a call |
| `services/offline/__init__.py` | Not exported |
| `ui/`, `core/`, `main.py` | No references |
| `tests/contract/test_asset_lifecycle_contract.py` `ALLOWED_OTHER` | Path string so the file is classified if it mentions `.info/assets` writes |

Production GitHub offline uses `GitHubBrowserSnapshot` / layout snapshot, not `ReadableSnapshotProvider.snapshot`.

## 2. Test helper only?

**It is a full unused provider, not a test double.**

The module implements a real pipeline: fetch HTML → strip chrome → write `index.html` + `style.css` (no image downloads). Tests drive that pipeline with fixture HTML. Production never constructs `ReadableSnapshotProvider` except via the test-only `run_readable_offline_snapshot` wrapper.

So: **shipped-looking code with tests-only entry**, not a pytest fixture module.

## 3. If deleted — which tests?

| Impact | Detail |
|--------|--------|
| `tests/test_readable_snapshot.py` | Entire file (9 tests) would go or be rewritten |
| `tests/test_readable_snapshot.py::test_nexus_provider_is_manual_import` | Also covers `NexusManualOfflineProvider` — that assertion would need a new home |
| `tests/contract/test_asset_lifecycle_contract.py` | Drop `services/offline/readable_snapshot.py` from `ALLOWED_OTHER` |
| GitHub constructor tests | None import `readable_snapshot`; `readable_provider=` kw on `GithubOfflineProvider` can stay as a no-op or be removed separately |

Estimated test LOC lost if the module is removed without retargeting: **~307 lines** in `tests/test_readable_snapshot.py`.

## 4. If kept — what capability is it?

**Unshipped “readable HTML” offline snapshot:** main-content extract + local CSS, no JS/image mirror. Distinct from:

- Layout snapshot (`layout_snapshot.py`) — DOM + primary CSS via Playwright/HTTP (live Nexus/GitHub path)
- Browser snapshot — full rendered page
- Manual Nexus HTML import
- Steam/Mod.io archive rewrite

Product has not chosen to expose this as an OPEN backend.

## Candidate (not executed)

**KEEP** in `services/offline/` until a product decision.

| Option | When |
|--------|------|
| **KEEP** (current) | Default. Cost is ~1.1k LOC + 9 tests. No production risk. |
| **ARCHIVE** | After product confirms readable HTML will never be an OPEN backend. Move to `tools/archive/` with its test, drop `readable_provider` from `github.py`. |
| **REMOVE** | Same confirmation plus tests retargeted or deleted. Do not remove while `test_readable_snapshot.py` is the only documentation of Cloudflare degrade / no-image-download behavior. |

Phase 16 choice: **KEEP**. Not ARCHIVE/REMOVE this pass.
