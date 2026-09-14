# Phase 12.1 — Asset zero sweep (final report)

Garbage cleanup only. No architecture, Store, manifest schema, Identity, Deployment, or Backup DB changes. No library-wide hash / materialize / rebuild.

Audits:

- `_tmp/phase12_1_live_asset_cleanup_audit.json`
- `_tmp/phase12_1_backup_asset_cleanup_audit.json`

## Before

Phase 12 inventory + this sweep’s first listing (Windows long-path revealed one extra file that a short-path walk had missed):

| Surface | Trees | Files | Bytes |
|---------|------:|------:|------:|
| LIVE `.info/**/assets` | 28 | 9 | 352273 |
| Backup `offline/assets` | 1 | 1 (0-byte) | 0 |

LIVE nonempty at start:

- `mod/Anno 1800/Combat Overhaul 01 Ships/.info/offline/assets` — 8 files, 348937 bytes
- `mod/博德之门Ⅲ/Early-Access-Dream-Visitor-Restored-at-Baldur-s-Gate-3-Nexus-Mods-and-community-www.nexusmods.com/.info/offline/assets` — 1 file, 3336 bytes (long path; previously looked empty)

Backup:

- `data/mod_backup/2567/offline/assets` — `bf7acb7becdc7091.png`, 0 bytes

## What was deleted

| Class | Action |
|-------|--------|
| 26 empty LIVE `assets/` dirs | Deleted (section 3). `.info`, `metadata.json`, `manifest.json`, `internal_id`, `index.html` kept. |
| Backup `2567/offline/assets` | Deleted. Manifest (344 objects) + Store complete. `offline/index.html` and `offline/manifest.json` kept. Empty `assets/` not left behind. |

No migrate, copy, or `offline_view` materialize. Store objects were checked with `exists` + size vs manifest (no leftover-file hash, no full-library hash).

## After

| Surface | Trees | Files | Bytes |
|---------|------:|------:|------:|
| LIVE `.info/**/assets` | **2** | **9** | **352273** |
| Backup `offline/assets` | **0** | **0** | **0** |

LIVE is **not** zero. Those two trees were **not** deleted.

## STOP — not auto-fixed

Both remaining trees have a usable LIVE `manifest.json` and every **manifest** hash exists in Asset Store (`open_rebuildable: true`). Leftover files on disk are **not listed in the manifest**.

Per this phase: do not migrate, do not hash leftover bytes into Store, do not delete unindexed files.

| Path | Files | Manifest assets | Store | Leftover vs manifest |
|------|------:|----------------:|-------|----------------------|
| `mod/Anno 1800/Combat Overhaul 01 Ships/.info/offline/assets` | 8 | 32 | 32 present | 8 files not in manifest (2 of those names still appear in `index.html`) |
| `mod/博德之门Ⅲ/Early-Access-Dream-Visitor-…/.info/offline/assets` | 1 | 79 | 79 present | 1 file not in manifest |

OPEN for these Mods can still rebuild **manifest** assets: Asset Store → `cache/offline_view`. The leftover files are unused by OPEN. They were left on disk because they are not indexed.

## Grep — `.info/assets` / `offline/assets`

Allowed (docs / tests / cleanup / migration): `docs/**`, `tests/**`, `tools/**`, `services/legacy_*`, `tools/archive/legacy_asset_tools/fast_info_asset_purge.py`, `services/info_asset_migration.py`, `services/backup_asset_migration.py` (leftover ingest / GC comments), `services/dir_size.py` (skip leftover size).

Production OPEN / resolver / browser:

- `ensure_live_offline_openable` / `prepare_offline_open` → `cache/offline_view` only
- `ModMetadataResolver.resolve_offline_page` → `prepare_offline_open`
- `ensure_backup_offline_openable` → `cache/offline_view` only
- UI size helper skips leftover `offline/assets` (does not OPEN them)

No production OPEN / resolver / browser-launch path reads leftover physical `assets/`.

## Legacy tools

Not deleted (Phase 13):

- `legacy_info_asset_cleanup`
- `fast_info_asset_purge`
- `legacy_backup_asset_cleanup`
- rebuild / migrate operator tools

## Acceptance

| Criterion | Result |
|-----------|--------|
| LIVE `.info/assets` = 0 | **FAIL** — 2 unindexed leftover trees (9 files). Not deleted. |
| Backup `offline/assets` = 0 | **PASS** |
| OPEN does not read physical leftover assets | **PASS** |
| Asset Store + manifest can restore OPEN (indexed assets) | **PASS** for remaining Mods’ manifests |
| No business DB / Identity / Deployment changes | **PASS** |
| No architecture change | **PASS** |

Next operator choice (not this phase): ingest those 9 files into Store + manifest, or explicitly discard them as unreferenced. This phase does neither.
