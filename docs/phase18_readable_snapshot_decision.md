# Phase 18 — `readable_snapshot.py` decision

File: `services/offline/readable_snapshot.py` (~1128 physical lines).

## Conclusion

**KEEP**

Not ARCHIVE. Not REMOVE. No code change in this module (or `github.py` `readable_provider=`).

## Evidence

### Runtime caller

**None.**

| Location | Result |
|----------|--------|
| `ui/` | no import |
| `core/` | no import |
| `main.py` | no import |
| `services/offline/__init__.py` | not exported |
| Production GitHub OPEN | `GitHubBrowserSnapshot` / Playwright |

`GithubOfflineProvider.__init__` still accepts `readable_provider=` then `del`s it. That is not a call into this module.

### Test caller

`tests/test_readable_snapshot.py` (9 tests): parsers, CSS write, no image download, Cloudflare degrade, `NexusManualOfflineProvider` overlap, render sections.

`tests/contract/test_asset_lifecycle_contract.py` `ALLOWED_OTHER` lists the path as a string only.

### Provider contract

`ReadableSnapshotProvider` is a full unused pipeline: fetch HTML → extract main content → write `index.html` + `style.css`, no JS/image mirror.

It is **not** on the OPEN contract (`manifest → Asset Store → cache/offline_view`).

Live Nexus/GitHub offline uses layout snapshot / browser snapshot / manual HTML import instead.

## Why not ARCHIVE or REMOVE

| Option | Blocker this phase |
|--------|-------------------|
| **ARCHIVE** | Needs a product “never an OPEN backend” call, then a move of module + tests. Instruction: if not REMOVE, do not change this code. |
| **REMOVE** | Would drop ~307 test LOC and the only lock of Cloudflare degrade / no-image-download, unless those tests are retargeted first. No product kill. |

KEEP cost: ~1.1k production LOC + 9 tests, zero OPEN risk.

Next change allowed only after an explicit product ARCHIVE/REMOVE.
