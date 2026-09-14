# Project layout

Canonical trees. Do not put regenerable or log files in `data/`.

```
steam-mod-manager/
├── README.md              # project entry (root markdown)
├── main.py                # application entry
├── pytest.ini
├── requirements.txt
├── data/                  # durable business data only
├── cache/                 # deletable performance cache
├── logs/                  # runtime logs (deletable)
├── docs/                  # long-term documentation
├── config/                # local configuration
├── mod/                   # Mod library
├── core/ services/ ui/    # application code
└── tests/ tools/ scripts/
```

There is currently no `LICENSE` file at the repository root.

## data/

Permanent business state. Never wipe this tree as a cache.

| Entry | Role |
|-------|------|
| `mod_manager.db` (+ `-wal` / `-shm` while SQLite WAL is active) | Library / Identity / Deployment rows |
| `asset_store/` | Unique durable asset bytes |
| `mod_backup/` | Backup protocol |
| `deploy_backup/` | Deployment backup |
| `collection_covers/` | Collection covers |
| `identity_repair_quarantine/` | Identity repair quarantine |
| `mod_types.json` | Type Definition catalog (in use) |
| `mod_types.legacy_migrated` | Migration sidecar (still referenced; do not delete) |
| `app_instance.lock` | Single-instance `QLockFile` (runtime; auto-unlock on clean exit) |

SQLite uses `journal_mode=WAL`. `-wal` / `-shm` must not be deleted while the GUI holds the database.

`import_crash_trace.log` may still appear here while a GUI process has faulthandler attached. Designated home is `logs/`. Move or delete it only after `main.py` has exited.

## cache/

Safe to delete entirely. Rebuilt from `data/` + `mod/` + Asset Store.

See `docs/cache_layout.md`.

```
cache/
├── offline_view/
├── asset_cache/
├── import_cache/
├── headers/
└── temp/
```

## logs/

Runtime logs only. Safe to delete. Not Source of Truth.

After a clean GUI shutdown, crash traces should live at `logs/import_crash_trace.log` rather than under `data/`.

## docs/

Long-term documentation. Architecture contracts live under `docs/architecture/`. Current asset lifecycle: `docs/asset_lifecycle_final_status.md`. One-shot audits belong under `_tmp/`, never at the repository root (except `README.md`).

## Root markdown rule

Only `README.md` stays at the repository root. Other `*.md` files are either long-term docs (`docs/`) or audited temporary records (deleted only after classification).
