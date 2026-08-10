# Phase E+ — Source-aware Mod File Management (Read-only Audit)

**Date:** 2026-08-08  
**Scope:** Audit only. No schema, deploy strategy, offline, or product-code changes in this phase.  
**Allowed deliverable:** this document.

---

## Executive answers (mandatory)

### 1. Does current `mod_files` support multi-file / type / source / user selection?

| Capability | Supported today? | Evidence |
|---|---|---|
| **Multi-file** | **Yes** | One Mod row owns a JSON list `{"files":[...]}` in `mods.mod_files`. Model: `ModFilesBundle.files: list[ModFileEntry]` in `core/mod_platform.py`. Persistence: `DatabaseManager.get_mod_files` / `set_mod_files`. |
| **File type** | **Partial / coarse** | Per-entry `type` with values `main` \| `optional` \| `patch` (`FILE_TYPE_*` in `core/mod_platform.py`). **Not** Nexus roles (`main_file` / `optional_file` / …) or GitHub roles (`release_asset` / …). |
| **Source (per file)** | **No** | No `source_type` (or similar) on `ModFileEntry`. Platform source lives on the **Mod** row: `mods.platform`, `mods.source_url`, `mods.external_id`. |
| **User selection state** | **Yes (as `enabled`)** | `ModFileEntry.enabled: bool` (default `True`). UI checkboxes call `ModFileManager.set_file_enabled`. Deploy uses `bundle.enabled_files()`. There is **no** field named `selected` / `selected_for_deploy`. |

**Current entry fields (JSON keys via `ModFileEntry.to_dict`):**

```text
id, name, filename, path, type, enabled
```

**SQL storage:** single column `mods.mod_files TEXT NOT NULL DEFAULT '{}'` (create + `_MODS_MIGRATIONS`), not a separate `mod_files` table.

---

### 2. What does deploy logic currently read?

| Signal | Used? | Where |
|---|---|---|
| **File path** | **Yes** | `services/deploy.py` → `resolve_deploy_sources`: builds allow-list from `entry.path` (fallback `entry.filename`), plus basename / resolved relative path. |
| **`enabled` (per file)** | **Yes** | Only `bundle.enabled_files()` contribute when the bundle is non-empty. |
| **`selected` / `selected_for_deploy`** | **No** | Identifier does not exist in codebase. |
| **Whole Mod (legacy)** | **Yes** | If `mod_files` parses to an empty `files` list → `resolve_deploy_sources` returns `None` → strategies treat as full-folder / full-scan deploy (`DeployContext.allowed_rel_paths is None`). |
| **Mod-level `mods.enabled`** | **Yes (gate)** | `ModDeployer.deploy_mod` refuses with `"Mod disabled"` if `DatabaseManager.is_mod_enabled(mid)` is false — orthogonal to per-file `enabled`. |

**Call chain:**

1. `ModDeployer._resolve_context` → `allowed_rel_paths=resolve_deploy_sources(mid, source, db=db)` (`services/deploy.py`).
2. Strategies (`FolderCopyStrategy` in `services/deploy_rules/generic.py`, Palworld pak path in `services/deploy_rules/palworld.py`) filter via `is_rel_path_allowed(..., ctx.allowed_rel_paths)` (`services/deploy_rules/base.py`).
3. When allow-list is `None`, `FolderCopyStrategy.deploy` uses `shutil.copytree` (skips `.info` / `info` only). When set, only planned allowed files are `copy2`'d.

**Proven by tests:** `tests/test_deploy_mod_files.py` — disabled optional path is not deployed.

---

### 3. Minimum change vs need migration?

**Recommendation: minimum-change JSON evolution inside existing `mods.mod_files` — no new SQL table; no hard `ALTER` required for Phase E+ core fields.**

| Approach | Verdict |
|---|---|
| **New SQLite table for files** | Not required; multi-file already lives in JSON. |
| **Add SQL columns for each file field** | Wrong granularity (files are array elements). |
| **Additive JSON keys on `ModFileEntry`** | Preferred: keep `enabled`; optionally add aliases / new keys (`source_type`, `file_role`, `display_name`, `selected_for_deploy`, `metadata`) with **read-time defaults** for old blobs. |
| **Data migration (soft)** | Needed for *semantic* upgrade of existing rows: fill `source_type` from `mods.platform`, map `enabled` → `selected_for_deploy` (or treat them as synonyms), map coarse `type` → `file_role=legacy` (or keep `type` as deploy-facing role until importers emit richer roles). Can be lazy (`from_dict`) + optional one-shot cleanup, similar to `cleanup_image_entries_in_mod_files`. |

**Compatibility with proposed unified FileEntry (do not implement in this audit):**

| Proposed | Current | Compatibility note |
|---|---|---|
| `source_type` (`steam`/`nexus`/`github`) | *(missing on entry)*; Mod has `platform` | New JSON key; default from `mods.platform` on read/migrate. |
| `file_role` (platform-specific enums) | `type` ∈ {`main`,`optional`,`patch`} | Overlap but different vocabulary. Prefer keep writing `type` until UI/deploy migrate, or dual-write; map old → `legacy` / `workshop_content` / `main`→`main_file`. |
| `display_name` | `name` | Alias: `display_name` ↔ `name`. |
| `selected_for_deploy` | `enabled` | **Must not break `enabled`.** Deploy today reads `enabled`. Either synonym both fields in `from_dict`/`to_dict`, or teach deploy to accept either while UI writes both. |
| `metadata` JSON | *(missing)* | New nested object; ignore-unknown on old rows. |
| `path` / `filename` / `id` | Present | Keep as deploy identity keys. |

**If schema fields are “missing” relative to the proposal:** they are missing as **JSON keys on the entry**, not as SQL columns. Migration outline (later phase):

1. Extend `ModFileEntry` serialize/deserialize with backward-compatible defaults.
2. Optionally walk all mods once: enrich entries from `mods.platform`; preserve `enabled`.
3. Point deploy/UI at a single getter (e.g. `selected_for_deploy if present else enabled`).
4. Update importers / import dialog to emit richer roles for Nexus/GitHub only; Steam stays one workshop entry / empty→legacy whole-mod behavior.

---

## Current data model (detail)

### SQL (`core/db_manager.py`)

- Table `mods` column: `mod_files TEXT NOT NULL DEFAULT '{}'`
- Migration tuple `_MODS_MIGRATIONS`: `("mod_files", "TEXT NOT NULL DEFAULT '{}'")`
- Constant: `DEFAULT_MOD_FILES_JSON = "{}"` (`core/mod_platform.py`)
- APIs: `get_mod_files(mod_id) -> ModFilesBundle`, `set_mod_files(mod_id, bundle)`, `register_external_mod(..., mod_files=...)`
- `ModDisplayInfo.mod_files_json` + property `mod_files -> ModFilesBundle.from_json(...)`

There is **no** relational child table and **no** Alembic-style versioned migration for file entries — only additive column ensure-on-startup.

### Python model (`core/mod_platform.py`)

```python
@dataclass
class ModFileEntry:
    id: str = ""
    name: str = ""
    filename: str = ""
    path: str = ""
    type: str = FILE_TYPE_MAIN  # main | optional | patch
    enabled: bool = True

@dataclass
class ModFilesBundle:
    files: list[ModFileEntry] = field(default_factory=list)
    def enabled_files(self) -> list[ModFileEntry]: ...
```

Helpers: `normalize_file_type`, `new_file_id`, `SUPPORTED_FILE_TYPES`.

### Manager facade (`services/mod_files.py`)

`ModFileManager`: `get_files`, `get_enabled_files`, `add_file`, `remove_file`, `toggle_file`, `set_file_enabled`, `replace_all`.  
Comment in module: UI/deploy must not touch `mod_files` JSON directly — go through this / DB APIs.

---

## ImportContext & ModImporter

### `ImportContext` (`services/importers/importer_base.py`)

Fields only:

- `game_id: int`
- `game_name: str`
- `offline_html_path: str | None`

**No file-selection payload.** Completeness = valid game id/name (`is_complete` / `require_import_context`). Steam may soft-resolve without full context (`resolve_game_for_import(..., require_context=False)`).

### `ModImporter` ABC

- `platform: str`
- `detect(value) -> bool`
- `import_mod(**kwargs) -> ImportResult`

`ImportResult` exposes `files_count` (len of scanned bundle) but not file selection details.

### Registry (`services/importers/__init__.py`)

`detect_importer` tries `SteamImporter` → `NexusImporter` → `GithubImporter`.  
`ArchiveImporter` extracts then delegates to platform importers; still uses `scan_mod_directory` for root validation.

---

## Current import flows regarding files

All three platform importers (and archive prep) ultimately build `ModFilesBundle` via **the same** scanner:

`services/importers/local_scanner.py` → `scan_mod_directory` / alias `scan_folder_to_mod_files`.

**Scanner rules (platform-agnostic):**

- Recurse folder; skip images, `.info`/`info`, VCS, hidden files.
- Rank candidates (stem “main”, then `.pak`/`.zip`/`.7z`/`.rar`/…).
- First primary → `type=main`, `enabled=True` (display name often `"Main File"`).
- Others → `type=optional`, `enabled=False` (with a few stem heuristics that can mark additional mains, then demote duplicates).
- Stores relative `path`, `filename`, UUID `id`.

### Steam — `services/importers/steam.py`

1. Resolve Workshop ID; soft game context.
2. `upsert_mod` + `update_mod_platform_info(platform=steam, source_url=workshop URL, external_id=mid)`.
3. If `source_folder` is a directory → `bundle = scan_mod_directory(folder)`; else **empty** `ModFilesBundle()`.
4. `db.set_mod_files(mid, bundle)`.
5. Optional `materialize_imported_mod` copy into library.

**Implication for Phase E+:** empty `mod_files` ⇒ deploy whole Mod folder (legacy). Steam “unchanged” should preserve: single workshop content folder semantics; avoid forcing multi-choice UI.

### Nexus — `services/importers/nexus.py`

1. `require_import_context` (game required).
2. Require existing `source_folder` directory (manual / extracted pack).
3. `bundle = scan_mod_directory(folder)`.
4. `db.register_external_mod(..., mod_files=bundle)`.
5. Materialize copy into library.

**Gap vs Phase E+ multi-zip:** importer does **not** accept a list of separate Nexus download zips with roles. One folder scan only; zip files inside the folder become optional/main by ranking, not by Nexus “Main Files / Optional / Misc / Old” taxonomy. No import-dialog multi-add UI wired to `mod_files`.

### GitHub — `services/importers/github.py`

1. `require_import_context`.
2. Parse `owner/repo` as `external_id`.
3. Same: `scan_mod_directory(folder)` → `register_external_mod(..., mod_files=bundle)`.

**Gap vs Phase E+ multi-asset:** no GitHub Releases API / asset list; no `release_asset` vs `source_archive` vs `developer_build`. Same local-folder heuristics as Nexus/Steam.

### Materialize (`services/importers/materialize.py`)

Copies **entire** `source_folder` tree into managed library (`ModFileManager.copy_mod`). Does **not** filter by `enabled`. Selection only affects **deploy**, not what is stored on disk.

### Offline / archive (out of Phase E+ change scope — note only)

- Offline generators may *display* file lists (`services/offline/...` has a separate `FileEntry` for readable pages — **not** `ModFileEntry`).
- Must not be modified for this feature per product constraints.

---

## Detail Panel Files UI

**File:** `ui/mod_detail_panel.py`

- Section builder: `_build_files_section` → host `mod_files_host` / layout `mod_files_layout`, section title `"Files"`.
- Populate: `_fill_view` → `files_bundle = info.mod_files` → `_fill_mod_files_list(files_bundle)`.

**Behavior:**

| Condition | UI |
|---|---|
| `len(files) <= 1` | Compact label `"0 files"` / `"1 file"`; tooltip shows `filename|path [type]`. **No checkbox** for single-file. |
| `len(files) > 1` | Summary `"{n} files · enabled {k}/{n}"` + per-entry `QCheckBox` bound to `entry.enabled`, property `file_id`, subtitle = `filename`/`path`. |

**Persist:** `_on_mod_file_toggled` → `ModFilesJsonManager(get_db()).set_file_enabled(mid, file_id, checked)` → reloads `get_mod_display_info`, emits `tags_saved` for library refresh.

**Gaps vs Phase E+ Detail design:**

- No platform header (`Platform: Nexus`).
- No role labels (`Main File` / `Release Assets` groups) beyond raw `type` in tooltip.
- No Select All / Clear Optional / Reset Default actions.
- Steam single-file path never shows a checkbox (acceptable for “✓ Workshop Content” later, but different copy).
- Copy helpers use `filename`/`name` only (`_file_names_for_copy`).

---

## Gaps vs Phase E+ requirements

| Requirement | Current state |
|---|---|
| Unified FileEntry with `source_type`, `file_role`, `display_name`, `selected_for_deploy`, `metadata` | Only `ModFileEntry` with `id/name/filename/path/type/enabled`. |
| Nexus multi zip import + typed defaults (main checked, optional/misc unchecked) | Single folder scan; coarse `main`/`optional`; no multi-zip import UI. |
| GitHub multi asset import + first asset selected | Same scanner; no release assets model. |
| Steam unchanged (one mod / one directory; simple Files) | Mostly true; empty bundle ⇒ whole-folder deploy; with scan, multi-file checkboxes can appear if folder has many files — **risk** if Steam packs are scanned aggressively. |
| Deploy reads selection only (platform-agnostic) | Already platform-agnostic via `enabled` + paths; rename/synonym needed for `selected_for_deploy`. |
| Do not change deploy strategy core / offline Steam-Nexus-GitHub | Compatible with JSON-only + `resolve_deploy_sources` synonym change. |
| Import dialog platform-specific file UI | `ui/mod_import_dialog.py` has source-type radio rows; no multi-file role editor yet. |
| Card must not show file counts | Out of this audit’s UI redesign scope; verify separately when implementing. |

---

## Risk list (must not break)

1. **Legacy empty `mod_files` (`{}` / no files)** — must keep meaning “deploy whole Mod” (`allowed_rel_paths is None`).
2. **Existing `enabled` semantics** — users already toggle Detail checkboxes; deploy tests assert disabled files stay out.
3. **Mod-level `mods.enabled`** — do not conflate with per-file selection.
4. **Steam Workshop ID identity** — `mod_id` = Workshop ID; do not force non-Steam id allocation or multi-choice UX.
5. **Nexus/GitHub identity** — `(platform, external_id)` uniqueness via `register_external_mod`.
6. **Materialize full-folder copy** — selection must not silently delete unselected files from library unless product explicitly changes that (Phase E+ says deploy selection, not library prune).
7. **Deploy strategies** — only feed allow-list; do not add platform branches inside `generic.py` / `palworld.py`.
8. **Offline snapshot / archive / Nexus manual offline** — out of scope; do not couple FileEntry redesign to offline HTML generators.
9. **Image exclusion** — images must stay out of `mod_files` (scanner + `cleanup_image_entries_in_mod_files`).
10. **Single-file Detail UI** — currently no checkbox when `len <= 1`; changing Steam to always show “Workshop Content” must remain non-breaking.
11. **Path matching fragility** — allow-list matches rel path, basename, and loose suffix rules (`is_rel_path_allowed`); new nested zip layouts may need careful `path` values.
12. **Dual field `enabled` vs `selected_for_deploy`** — risk of desync if both written inconsistently.

---

## Recommended implementation order (later — do not implement now)

1. **Model compatibility layer** in `core/mod_platform.py`: extend `ModFileEntry` JSON with optional new keys; keep `enabled`; read `selected_for_deploy` as alias; ignore unknown keys; default `source_type` from caller/`mods.platform`.
2. **Soft migration helper** (lazy in `from_dict` + optional DB walk): old entries remain deployable; set `file_role`/`source_type` defaults (`legacy` / platform).
3. **Deploy synonym** in `resolve_deploy_sources` only: treat selected = `selected_for_deploy if key present else enabled` — **no** strategy rewrites.
4. **`services/mod_files.py`**: `set_file_enabled` updates both flags; add helpers for role/display if needed.
5. **Steam importer**: explicitly ensure single workshop entry or keep empty→legacy; **no** multi-choice import UI.
6. **Nexus import path + dialog**: allow multiple archives → multiple entries with Nexus `file_role` defaults (main checked, others unchecked); still one Mod row.
7. **GitHub import path + dialog**: multiple assets with GitHub roles; default first release asset selected.
8. **Detail Panel Files**: platform-aware grouping/labels + bulk actions; keep checkbox → selection persistence; Steam simple row.
9. **Tests:** `tests/test_source_file_model.py` (as specified) + extend `test_deploy_mod_files.py` for alias field; regression Steam empty-bundle whole deploy.
10. **Docs / acceptance:** Steam UX unchanged; Nexus/GitHub multi-file selectable; offline/deploy strategies untouched.

---

## Key file index

| Path | Role |
|---|---|
| `core/mod_platform.py` | `ModFileEntry`, `ModFilesBundle`, file type constants |
| `core/db_manager.py` | `mods.mod_files` column, get/set/register |
| `services/mod_files.py` | Mutation facade for UI |
| `services/importers/importer_base.py` | `ImportContext`, `ModImporter`, `ImportResult` |
| `services/importers/local_scanner.py` | Shared folder → bundle |
| `services/importers/steam.py` | Steam import + `set_mod_files` |
| `services/importers/nexus.py` | Nexus import + `mod_files=bundle` |
| `services/importers/github.py` | GitHub import + `mod_files=bundle` |
| `services/importers/materialize.py` | Library folder materialization |
| `services/deploy.py` | `resolve_deploy_sources`, `ModDeployer` |
| `services/deploy_rules/base.py` | `DeployContext.allowed_rel_paths`, `is_rel_path_allowed` |
| `services/deploy_rules/generic.py` | folder_copy allow-list vs copytree |
| `services/deploy_rules/palworld.py` | pak deploy respect allow-list |
| `ui/mod_detail_panel.py` | Files section + checkboxes |
| `tests/test_deploy_mod_files.py` | Deploy enabled-only contract |
| `tests/test_mod_files.py` | Manager + scanner defaults |

---

## Audit conclusion

The repo **already has** a multi-file JSON model with coarse types and checkbox-driven **`enabled`** selection that deploy honors via path allow-lists — plus a legacy empty-bundle whole-Mod path. What Phase E+ still needs is **source-aware roles/metadata**, **richer Nexus/GitHub import UX**, and a **backward-compatible alias** for `selected_for_deploy` / `display_name` / `source_type` **inside the existing JSON column**, not a new SQL schema or deploy-strategy fork.
)
