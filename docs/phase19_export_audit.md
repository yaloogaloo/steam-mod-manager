# Phase 19 — `__init__.py` re-export audit

Scan of every `from … import …` in package `__init__.py` files. **No automatic deletes.**

Protected (not modified): Identity aliases, `services/offline/__init__.py`, `services/importers/__init__.py`.

## Classification

### A — Production depends (KEEP)

| Package | Why |
|---------|-----|
| `services/offline/__init__.py` | Offline provider barrel; OPEN/UI import providers from here. **Frozen export surface.** |
| `services/importers/__init__.py` | Importer barrel + `detect_importer`. **Frozen export surface.** |
| `services/deploy_rules/__init__.py` | Live deploy strategy registry (`get_strategy`, `resolve_deploy_type`, WH3). Deploy freeze. |
| `services/offline/browser_snapshot/__init__.py` | Playwright snapshot API used by GitHub/Nexus capture. |
| `services/offline/nexus_cleaner/__init__.py` | `clean_mhtml_to_offline` used by `manual_import.py`. |
| `services/update_sources/__init__.py` | `get_update_source` used by `services/mod_update.py`. |
| `ui/__init__.py` | `MainWindow` is the GUI package entry. |

### B — Tests / tools also import the package (KEEP)

In-repo callers often use **submodules** (`from services.deploy_rules import load_manifest`, `from services import collection`) rather than the barrel names. Tests still import:

- `from services.importers import …` (`test_importers.py`, `test_mod_import_ui.py`, …)
- `from services.offline.nexus_cleaner import …`
- `from services.deploy_rules import …`

No test-only re-export was safe to strip without also proving zero production `from package import Name`.

### C — Pure historical re-export

**None confirmed.**

| Candidate | Why not C |
|-----------|-----------|
| `core/__init__.py` (`DatabaseManager`, `get_db`, `project_root`, …) | Intended public package API. In-repo code prefers `core.db_manager` / `core.paths`, but this is the documented facade, not a leftover alias. |
| `services/__init__.py` (`ModFileManager`, `ModSyncService`, `OfflinePageArchiver`, …) | Same: package facade. Callers use `from services import archive` (submodule) more than barrel names; dropping the barrel would be an API shrink without a product call. |
| Empty `__init__.py` (`tools/`, `tests/`, `services/runtime/`, `tests/helpers/`, `tests/offline/`) | No re-exports. |
| `tools/archive/__init__.py` | Docstring only. |
| `scripts/__init__.py` | Comment only. |

## Deletes this phase

**None.** No Class C item was clear enough to remove without touching Identity / offline / importer / deploy surfaces.

## Note

Phase 17 scanner “unused imports” inside `__init__.py` files are **re-exports**, not dead code. They stay Class B.
