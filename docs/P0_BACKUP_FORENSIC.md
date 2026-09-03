# P0 Backup / Startup Sync Forensic

Instrumentation and static call-graph only.
**No production 5-minute startup log was captured this round.**
Do not treat the numbers below as that 5min+ run.

Two different “backup” systems must not be mixed:

| System | Path | What it copies | Startup? |
|---|---|---|---|
| **Metadata backup** (`backup synced` log) | `data/mod_backup/<mod_id>/` | `.info` metadata.json + cover + `offline/` snapshot | **Yes** — every reconcile |
| **Deploy overwrite backup** (`BackupManager`) | `<mod>/.info/backups/` | Game-dir files about to be overwritten | Deploy only |
| Historical **full-library robocopy** | identity-repair backup | Entire `mod/` tree | One-off repair; **~698s previously**; **not** this startup path |

There is **no `robocopy` invocation** in the metadata-backup / reconcile code (repo grep). The 698s figure is a different job.

---

## 1. Startup chain (code)

```
MainWindow restore
  QTimer.singleShot(0):
    refresh_system_proxy          (P0-2, network, not disk)
    start_reconcile_library_async
        ↓
    daemon thread name=library-reconcile   (ONE thread)
        ↓
    reconcile_library()
        for folder in list_managed_mods():          # serial
            ensure_mod_identity / maybe create
            folder.resolve()
            sync_after_metadata_change(..., "restore"|"import")
                sync_metadata_backup
                    snapshot_from_mod_folder
                        write metadata.json if text differs
                        cover: size+sha256 then copy2 or skip
                        offline/: sha256 of index.html then copytree or skip
                    update_mod_backup_snapshot (DB)
                validate_backup (JSON read, existence checks — not full tree hash)
                update_mod_backup_status (DB)
        then: backup rows without disk; orphan backup dirs
  [BACKUP_START] reason=reconcile
  [BACKUP_RESULT] after the whole pass
```

UI: `library_view.refresh` may **queue another** reconcile if one is already running (coalesced follow-up). CoverLoaderManager runs on library cards (decode/scale) **in parallel with** the daemon — possible disk contention, **not proven** as the 5min source.

ConflictDetector post-deploy scan is **not** on this startup path unless a deploy happens.

---

## 2. Serial: where, and is it “the” cause?

**Where:** `reconcile_library`’s `for folder in list_managed_mods()` and nested `sync_after_metadata_change` are synchronous on **one** daemon thread. Not a thread pool. Design of “one library pass”; historically not a performance experiment.

**Is serial the 5min root cause?** **Unknown.** Serial only says work is not overlapped across Mods. If each Mod is 50ms of hash, 1766 Mods ≈ 88s; if each hashes a large `offline/` tree, minutes are plausible. **Need `[BACKUP_RESULT] total_elapsed_ms` + per-mod `copy_ms` from a real startup.**

---

## 3. Layer answers (static)

| Question | Answer | Evidence class |
|---|---|---|
| Every Mod fully scanned? | Every managed folder with `.info/metadata.json` is visited every reconcile | code |
| Repeat scan same directory? | `list_managed_mods` once per reconcile; `library_view` may enqueue a **second** pass | code |
| Unchanged files still hashed? | **Yes.** `_same_file_content`: size equal → **SHA256 both files**. Cover and offline index. | code |
| Manifest recomputed every start? | **No.** `deploy_manifest.json` is not rebuilt here. Metadata backup snapshots `.info`. | code |
| Lots of `Path.resolve()`? | Yes: every folder, cover dest, last_known_path | code |
| Repeat walk? | Game dirs iterated once per list_managed_mods call | code |
| Per-Mod robocopy? | **No** on this path | code |
| Copy unchanged? | Skip `copy2`/`copytree` when hash matches; **hash still runs** | code |
| Duplicate backup? | Second reconcile pass would sync again (hash/compare again) | code |
| Concurrent I/O? | Cover loader + possible second reconcile + user actions | code, unmeasured |
| DB/UI block? | Reconcile is off UI thread; DB writes per Mod on that thread | code |
| Serial required? | Convenience / simplicity, not a documented invariant | code |

`validate_backup` after each sync reads JSON and checks file existence; it does **not** SHA256 the whole offline tree again.

---

## 4. Stage timing — what exists vs what is missing

**Implemented:**

- Per Mod: `backup synced ... elapsed_ms= copy_ms= persist_ms=`
- Reconcile aggregate: `[BACKUP_START] reason=reconcile` / `[BACKUP_STAGE] stage=sync` / `[BACKUP_RESULT]`
- Missing-backup rebuild: `[BACKUP_*]` on that narrower path

**Not split in logs (yet):** discover vs scan vs compare vs hash vs copy vs persist as separate production numbers for a 5min run.

**Production 5min+:** **no timed log this round.** Do not invent stage ms.

---

## 5. Most likely bottleneck (hypothesis, not proven)

Given static structure, the expensive work **inside** `copy_ms` is most likely:

1. SHA256 of `.info/offline/index.html` (and cover) **even when unchanged**
2. Occasional `shutil.copytree` of a large offline snapshot when hash mismatches
3. × number of managed Mods (order of ~1700 in last identity scan)
4. Optional second reconcile pass
5. Disk contention with CoverLoader

**Not** deploy `BackupManager`, **not** robocopy, **not** “copy a few KB of zip for minutes” (that was a previous false story about deploy).

---

## 6. Is this enough to locate 5min+?

**No.** Enough to know **which function** and **which I/O class**. Not enough to know hash vs copy vs DB vs contention **percentages**.

Next measurement (no code redesign): restart once, collect:

- `[BACKUP_START] reason=reconcile`
- `[BACKUP_RESULT] total_elapsed_ms mods=`
- sample of `backup synced ... copy_ms= persist_ms=` (min/median/max)

Then decide: skip hash when mtime/size match (incremental), or leave serial, or reduce double reconcile. **Do not multithread first.**

---

## 7. Hygiene

`ConflictDetector._TYPE_TO_CLASS` is unused (REVIEW, not deleted).
Metadata backup has no robocopy helper to delete.
