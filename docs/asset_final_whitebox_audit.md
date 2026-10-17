# Asset Lifecycle Final White-box Audit (Phase 3–10)

**Superseded for current lifecycle** by `docs/asset_lifecycle_final_status.md` (Phase 12). Keep this file as the Phase 3–10 white-box record.

Audit only. Conclusions from source call graphs, not prior STATUS markdown.

Gate: `cas_only_info_asset_runtime()` default **True**.

Related layout notes: `docs/asset_architecture_audit.md`.

---

# A. 当前架构是否正确

**PASS** (durable pipeline) **with FAIL sub-criterion** on leftover `.info/assets` retirement.

| Criterion | Verdict |
|-----------|---------|
| Asset Store is durable offline-asset SoT | PASS |
| LIVE/Backup `manifest.json` is the index | PASS |
| `cache/` is deletable and rebuildable | PASS |
| Capture → staging → finalize → Store | PASS (finalize is unique; **soft-fail**) |
| OPEN → manifest → Store → `cache/offline_view` → browser | PASS on CAS hit/miss **when usable manifest exists** |
| Repair (gate ON) does not write durable `.info/assets` | PASS |
| `.info/assets` fully out of production | **FAIL** — staging + leftover OPEN fallback + Backup in-place |

The intended CAS-only model **is wired and is the default**. It is not yet a closed leftover-free lifecycle.

---

# B. 已确认风险

## P0 — 数据损坏风险

**None confirmed** on the default gate.

OPEN / Repair (gate ON) do not rewrite Asset Store objects or `mod_manager.db`. Repair writes LIVE `manifest.json` after Store verify; it does not put new hashes. Capture finalize `put`s new objects; that is ingest, not corruption.

Split-brain (leftover `.info/assets` vs Store) can serve **stale bytes** on the leftover OPEN path. That is a consistency bug, not Store wipe.

## P1 — 功能风险

1. **UI-thread OPEN miss hashes the full CAS closure**  
   `ModDetailPanel._open_offline` / `ModDetailDialog._open_offline` call `resolve_offline_page` on the Qt thread. Cache miss: `verify_manifest_against_store` → `AssetStore.verify` (content SHA-256 per object) then `materialize_manifest_to_dir` verifies **again** and hashes each copy. Large pages freeze the UI. `BackgroundAssetWorker` exists but has **zero production callers**.

2. **`safe_finalize_live_offline` swallows errors**  
   Capture can report success while `assets/` remains on disk. Staging becomes a durable-looking tree.

3. **LIVE OPEN disk fallback when CAS is unusable**  
   `ensure_live_offline_openable`: if gate ON but no usable manifest/Store, still opens LIVE `index.html` in place when `live_assets_need_materialize` is false. Browser then reads `.info/assets` (or sibling `assets/`). Violates “no `.info/assets` → browser”.

4. **Backup OPEN in-place leftover `offline/assets`**  
   `ensure_backup_offline_openable` returns Backup index without Store if physical files already satisfy the HTML. Mixes leftover Backup tree with CAS-only Backup snapshots.

5. **Latent UI helper `offline_page_exists`**  
   `ui/library_query.py` calls full `resolve_offline_page` (can materialize). Production UI does not call it (tests only). Wiring it into list/filter would become HIGH UI block.

## P2 — 维护风险

- Dual Repair APIs (`repair_live_from_cas` vs `repair_info_assets_from_backup_store`); gate OFF still writes LIVE `assets/`.
- `restore_backup_assets_from_store` docstring still says it materializes Backup `offline/assets`; production OPEN passes `cache/offline_view`. Tests/tools still pass Backup dest (writes Backup assets — **test/tool path**, not UI OPEN).
- `asset_manifest.py` module comment still frames Phase 1 as unwired.
- Duplicate CAS verify on every OPEN miss (manifest verify + materialize verify).
- Crash log still conceptually `data/import_crash_trace.log` vs `logs/` (file-governance leftover; not CAS).
- Diagram name `data/backup/` vs real `data/mod_backup/`.

## P3 — 代码清理

One-shot Phase 3–10 services/tools, unused `BackgroundAssetWorker`, dead `LibraryView._build_filter_index` (contains `resolve_offline_page`). See section C.

---

# C. 可删除代码候选

List only. Do not delete in this audit.

### C1. One-shot / leftover GC (keep until leftovers = 0)

| 文件 | 函数 / 入口 | 作用 | 当前引用 | 删除风险 | 建议 |
|------|-------------|------|----------|----------|------|
| `tools/archive/legacy_asset_tools/legacy_info_asset_cleanup.py` | `cleanup_mod_info_assets`, `cleanup_all_safe_info_assets` | Remove leftover LIVE `.info/assets` after CAS | `tools/cleanup_legacy_info_assets.py`, `tools/audit_legacy_info_assets.py`, tests | Medium — leftovers still exist on real libraries | Keep until leftover audit is zero; then move to `tools/` only |
| `tools/archive/legacy_asset_tools/fast_info_asset_purge.py` | purge helpers | Faster leftover GC | `tools/purge_legacy_info_assets.py`, tests | Medium | Same |
| `tools/archive/legacy_asset_tools/legacy_info_manifest_rebuild.py` | rebuild LIVE manifest from leftover files | Repair index without re-capture | `tools/rebuild_legacy_info_manifests.py`, tests | Medium | Keep while leftover OPEN fallback exists |
| `tools/archive/legacy_asset_tools/legacy_backup_asset_cleanup.py` | Backup `offline/assets` GC | Phase 5 leftover Backup trees | tools + tests | Medium | Keep until Backup in-place OPEN is retired |
| `services/legacy_backup_finalize.py` | Backup finalize debt | One-shot Backup CAS | tools | Medium | Same |
| `tools/archive/legacy_asset_tools/backup_manifest_debt.py` | Manifest debt | One-shot | tools | Medium | Same |
| `tools/phase6_validate_info_cas_runtime.py` | CLI validate | Gate check | operator | Low | Keep as ops tool |
| `tools/migrate_backup_assets_to_store.py` | CLI migrate | Historical Backup → Store | operator | Medium | Keep until no Backup `offline/assets` |

### C2. Production — do **not** delete

| 文件 | 函数 | 作用 | 引用 | 删除风险 | 建议 |
|------|------|------|------|----------|------|
| `services/info_asset_migration.py` | `migrate_info_assets` | Capture finalize ingest | `finalize_live_offline_to_cas` | **High** | Keep |
| `services/info_asset_runtime.py` | `finalize_*`, `ensure_*`, `repair_live_from_cas` | LIVE CAS runtime | resolver, archive, offline providers | **High** | Keep |
| `services/asset_store.py` | Store | Durable objects | all CAS | **High** | Keep |
| `services/asset_manifest.py` | Manifest model | Index | all CAS | **High** | Keep; only refresh stale docstring |
| `services/offline/backup_closure.py` | `snapshot_offline_closure` | Backup CAS snapshot | `metadata_backup._copy_offline_index` | **High** | Keep |
| `services/offline_view_cache.py` | view cache | OPEN materialize target | info_asset_runtime, backup_closure | **High** | Keep |

### C3. Dead / duplicate / alias

| 文件 | 函数 | 作用 | 当前引用 | 删除风险 | 建议 |
|------|------|------|----------|----------|------|
| `ui/library_view.py` | `_build_filter_index` | Would call `resolve_offline_page` per folder | **no callers** | Low | Safe delete after confirm no dynamic getattr |
| `ui/library_query.py` | `offline_page_exists` | Exists check via full OPEN | tests only | Medium | Replace with `probe_live_offline_available` then delete OPEN path; update tests |
| `ui/background_asset_thread.py` | `BackgroundAssetWorker` | QThread wrapper | **no callers** | Low | Wire to OPEN miss **or** delete with `BackgroundAssetTask` kept for tests |
| `services/backup_asset_migration.py` | `repair_info_assets_from_backup_store` | Alias; gate OFF writes LIVE assets | tests, tools, cleanup | Medium | Keep until gate OFF is deleted; then inline to `repair_live_from_cas` |
| `services/background_asset_task.py` | `BackgroundAssetTask` | Batch IO | tests + unused UI wrapper | Low | Keep if OPEN is moved off UI; else tests-only |

---

# D. 架构违背点

### D1. `.info/assets` out of lifecycle

- **设计目标:** `.info/` holds `metadata.json`, `manifest.json`, `internal_id` only. Bytes live in Asset Store. Browser never reads `.info/assets`.
- **当前实现:** Capture still writes `assets/` staging. Finalize usually deletes it. OPEN still has disk fallback. Backup OPEN still serves leftover `offline/assets`.
- **问题:** Leftover trees remain a second unofficial SoT. Tests even document “legacy OPEN without manifest”.

### D2. OPEN = Store → cache → browser

- **设计目标:** `manifest → Asset Store → cache/offline_view → browser`. Never `.info/assets → browser`.
- **当前实现:** Cache hit matches the design. Cache miss matches the design **if** CAS is usable, but runs on the **UI thread** with full SHA-256. If CAS is not usable, LIVE index is opened in place.
- **问题:** Two OPEN implementations (CAS view vs leftover disk). Miss path is a UI freeze risk.

### D3. Repair = Store → cache, never reverse-write SoT

- **设计目标:** Repair only materializes Store → cache. Never cache / `.info/assets` → Store.
- **当前实现 (gate ON):** Verify Store, temp-dir materialize then **delete**, write LIVE manifest. Does not write `.info/assets`. Does **not** fill `cache/offline_view` (OPEN does). No reverse `put` from cache.
- **当前实现 (gate OFF):** Writes durable LIVE `assets/` — reverse of the target.
- **问题:** Default path is safe. Alias + gate OFF still encode the old Repair. Repair does not match the “→ cache/live” diagram literally.

### D4. Backup vs LIVE mix

- **设计目标:** Backup snapshot is index + Store hashes, not a second asset tree. OPEN MISS uses Backup manifest + Store → `cache/offline_view`.
- **当前实现:** Snapshot path does **not** persist `dest/assets/*`. OPEN still short-circuits if leftover Backup `offline/assets` exist.
- **问题:** Libraries mid-migration have two MISS behaviors.

### D5. `asset_cache` vs Asset Store

- **设计目标:** `cache/asset_cache` is regenerable HTTP cache, not SoT.
- **当前实现:** `services/archive.py` uses it as download accelerator; leftover ingest still goes through finalize. OPEN does not read `asset_cache`.
- **问题:** None for SoT. Naming collision with “asset” is a maintenance hazard only.

---

# E. 后续整改建议（优先级）

1. **P1 — Move OPEN miss off the Qt thread.** Reuse `BackgroundAssetWorker` (currently dead). Keep probe/`has_offline` on UI; only the click path may wait. Deduplicate `verify` inside `materialize_manifest_to_dir` when the caller already verified.

2. **P1 — Close leftover OPEN.** When gate ON, `ensure_live_offline_openable` must not return LIVE index in place. Fail OPEN (or Repair prompt) if CAS is unusable. Same for Backup: never open leftover `offline/assets` in place.

3. **P1 — Harden finalize.** `safe_finalize_live_offline` should fail the capture (or retry/clear) instead of leaving `assets/` as a silent second tree. Staging dir should be under `cache/temp`, not `.info/assets`, once leftover OPEN is gone.

4. **P2 — Delete gate-OFF Repair write of LIVE `assets/`.** Then collapse `repair_info_assets_from_backup_store` to `repair_live_from_cas`.

5. **P3 — After leftovers = 0,** relocate one-shot cleanup modules out of `services/` (or delete). Remove dead `_build_filter_index`. Change `offline_page_exists` to probe-only.

6. **Tests:** Add UI-thread / worker OPEN miss; `safe_finalize` failure leaves no durable `assets/`; gate-ON OPEN refuses leftover disk; Asset Store object missing on Open click; do not treat constructing `.info/assets` fixtures as proof of `archive.py` HTTP capture.

---

## 1. Production-path scan (classification)

Search tokens: `.info/assets`, `offline/assets`, `asset_cache`, `offline_view`, `materialize`, `repair`, `snapshot`, `finalize`, `legacy`, `migration`, `fallback`, `ensure_`.

### CURRENT_PRODUCTION (keep)

| Location | Role |
|----------|------|
| `services/info_asset_runtime.py` | finalize, OPEN, Repair, probe |
| `services/info_asset_migration.py` | ingest staging → Store |
| `services/asset_store.py` / `asset_manifest.py` | SoT + index |
| `services/archive.py` | Steam capture + `safe_finalize` + `asset_cache` fetch |
| `services/offline/{provider_snapshot,github,nexus_manual,manual_import}.py` | capture + finalize |
| HTML/MHTML/snapshot writers | **staging** `assets/` only |
| `services/mod_metadata_resolver.py` `resolve_offline_page` | OPEN entry |
| `ui/mod_detail_panel.py` / `mod_detail_dialog.py` `_open_offline` | UI OPEN |
| `services/offline/backup_closure.py` `snapshot_offline_closure` | Backup CAS snapshot + MISS view |
| `services/metadata_backup.py` `_copy_offline_index` | Backup owner |
| `services/offline_view_cache.py` | OPEN cache |
| `core/paths.py` cache helpers | Phase 10 layout |
| `core/cas_runtime.py` | gate |
| `services/cover_loader.py` / cover projection | covers (not CAS) |
| `services/dir_size.py` skip `.info/assets` | size exclusion |

### LEGACY_COMPAT (can delete only after leftover + fallback removed)

| Location | Notes |
|----------|-------|
| `ensure_live_offline_openable` disk fallback | leftover OPEN |
| `ensure_backup_offline_openable` in-place assets | leftover Backup OPEN |
| `repair_info_assets_from_backup_store` gate OFF branch | writes LIVE assets |
| `services/legacy_info_*` / `legacy_backup_*` / `fast_info_asset_purge.py` | GC |
| `services/backup_asset_migration.py` restore-to-Backup dest | tests/tools |
| Nexus gallery readers of `offline/assets` | leftover display |

### DEAD_CODE (no production callers)

| Location | Notes |
|----------|-------|
| `LibraryView._build_filter_index` | contains OPEN |
| `BackgroundAssetWorker` | unused QThread |
| `offline_page_exists` | tests only |

### TEST_ONLY

`tests/test_phase*.py`, `test_cache*`, `test_cas*`, `test_info*`, `test_asset_*`, `test_backup_offline_*`, `test_legacy_*`. Fixtures often **create** `.info/assets` then finalize — valid capture simulation, not HTTP proof.

### Bypass checklist

| Question | Finding |
|----------|---------|
| Read bytes skipping Asset Store? | Yes: leftover LIVE OPEN; leftover Backup OPEN; `asset_cache` (download only, not OPEN) |
| Regenerate `.info/assets`? | Capture staging yes; gate OFF Repair yes; gate ON Repair **no**; OPEN **no** |
| Backup mixed with LIVE? | Snapshot is CAS-only; OPEN MISS can still use leftover Backup files |
| Old-path fallback? | Yes (OPEN disk + Backup in-place) |

---

## 2. Asset flows (truth)

### Capture

```
capture → staging (.info/.../assets) → finalize → Asset Store → LIVE manifest
```

- Staging is intended temporary; directory is still named `assets/` under `.info`.
- Finalize wrapper `safe_finalize_live_offline` is the production unique entry; it is **not** fail-closed.

### OPEN

Allowed path when CAS usable:

```
manifest → Asset Store → cache/offline_view → browser
```

Forbidden path **still reachable**:

```
.info/assets (or Backup offline/assets) → browser
```

### Repair

Gate ON:

```
Asset Store verify → temp cache/temp/repair_check_* → delete → LIVE manifest
```

Does not write SoT from cache. Does not fill `offline_view`.

---

## 3. UI_BLOCKING_RISK

UI modules scanned: `ui/mod_detail_panel.py`, `ui/mod_detail_dialog.py`, `ui/library_view.py`, `ui/library_query.py`. No direct `hashlib` / `os.walk` / `shutil.copy2` in those files. Blocking is **indirect** via resolver.

| Severity | Location | Why |
|----------|----------|-----|
| **HIGH** | `ModDetailPanel._open_offline`, `ModDetailDialog._open_offline` | Qt slot → `resolve_offline_page` → full Store verify + copy on cache miss |
| **MEDIUM** | `ui/library_query.offline_page_exists` | Full OPEN; **not** called from production UI (tests only) |
| **MEDIUM** | `LibraryView._build_filter_index` | Would OPEN per card; **dead method** |
| **LOW** | `_has_offline_page` / `probe_live_offline_available` | Manifest existence, no hash |
| **LOW** | Library list `has_offline` | DB column via `mod_library_cache`, not OPEN |
| **LOW** | OPEN cache fingerprint hit | Skip verify |
| **LOW** | Cover load | No CAS hash in `cover_loader.py` |
| n/a (worker) | `OfflineArchiveWorker` / archive capture | Off UI; hash/copy OK |
| n/a (worker) | Backup snapshot hashing | Backup path, not paint |

Capture `os.walk` / sha256 in archive, snapshot, migration: not UI thread.

---

## 4. Test authenticity

### Covers a real path (isolated tmp Store)

| Test | Real? | Gap |
|------|-------|-----|
| `test_phase6_cas_only_info.py` | finalize clear, OPEN view, Repair no assets, MISS Backup, missing Store fail, snapshot without LIVE assets | Documents **legacy OPEN without manifest** (locks in fallback) |
| `test_cas_only_offline_capture_open.py` | fixture capture → finalize → OPEN view | Not `archive.py` HTTP |
| `test_cache_directory_contract.py` / `test_phase10_cache_contract.py` | wipe cache, OPEN rebuild; Store/DB isolated | Not full GUI startup |
| `test_phase9_asset_lifecycle.py` | static finalize-owner scan; OPEN/Repair no `.info/assets` | `test_production_write_need_fix_is_zero` is an empty-list assert |
| `test_phase5_cas_only_backup.py` | snapshot no Backup `offline/assets`; restore from Store | Restore helper can write Backup dest in tests |
| `test_phase7_*` / `test_fast_info_asset_purge.py` | leftover GC gates | Operator tools |
| `test_no_data_offline_view_regression.py` | retired `data/offline_view` | Layout only |
| `test_cas_only_stabilization.py` | threading/cleanup | Not UI OPEN |

### Not covered / weak

- OPEN **on the Qt thread** (freeze)
- `safe_finalize` failure leaving `.info/assets`
- Gate-ON OPEN **must not** use leftover disk
- Live Open click with missing Store object (Repair fail is tested in isolation)
- Manifest vs HTML ref mismatch as user-visible OPEN
- `offline_page_exists` must not materialize
- Full-library OPEN (correctly out of scope)

Many tests mock/construct `.info/assets` then call finalize — they exercise the ingest function, not the Steam HTTP archiver.

---

## Evidence index (source)

- OPEN UI: `ui/mod_detail_panel.py` `_open_offline`; `ui/mod_detail_dialog.py` `_open_offline`
- OPEN impl: `services/info_asset_runtime.py` `ensure_live_offline_openable`, `_materialize_live_view`
- Capture finalize: `safe_finalize_live_offline` callers listed in architecture JSON
- Repair: `repair_live_from_cas`; UI callers = none
- Backup snapshot: `services/metadata_backup.py` → `snapshot_offline_closure`
- Worker unused: `ui/background_asset_thread.py` `BackgroundAssetWorker`
