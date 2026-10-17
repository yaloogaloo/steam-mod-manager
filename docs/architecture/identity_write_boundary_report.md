# Identity Write Boundary Report

**Date:** 2026-09-07  
**Task:** Phase 2 Task 2.5 — Identity Write Boundary Audit  
**Policy:** No database schema changes

> **P2-2 (2026-09-10) contract correction:** the table below is a historical
> write-boundary audit. Frozen Entity Identity is TEXT `mods.internal_id`,
> not `mods.mod_id`. `mods.mod_id` is the SQLite PK / FK target. Business
> callers resolve `internal_id → resolve_mod_pk() → mod_id`. This report
> is not rewritten; do not treat its “Internal entity PK” wording as the
> Frozen identity law. See `docs/architecture/IDENTITY_LIFECYCLE_CONTRACT.md`.

---

---

## Contract

| Field | Role | Write rule |
|-------|------|------------|
| `mods.mod_id` | Internal entity PK | Sole identity key for UPDATE / entity match |
| `published_file_id` | Steam Workshop ID | API fetch + source association only — **never** INSERT as `mods.mod_id` |
| `workspace_id` / `external_id` | Platform registration | Derived from Workshop ID; not entity PK |

**Allowed Steam catalog path:**

```
Steam metadata fetch
        ↓
resolve existing entity (Workshop → mods.mod_id)
        ↓
UPDATE existing mod_id
```

**Forbidden:** auto-create Internal entity from Workshop ID via catalog / API upsert.

**Allowed create:** Sync / Import via `IdentityService.create_mod_identity` (`allow_insert=True` + create scope). Historical Steam scheme may still use Workshop digits as PK on intentional create — digits coincidence ≠ semantic merge.

---

## Audit results

### `database.upsert_mod` / `upsert_mods` — **FIXED**

| Before | After |
|--------|-------|
| `mid = int(meta.published_file_id)` used as INSERT/UPDATE PK | Resolve Workshop → entity via `resolve_steam_entity_mod_id` |
| Empty lifecycle auto-allowed INSERT | INSERT only when `allow_insert=True` or Identity create scope (Import/Sync) — **not** empty lifecycle |
| `upsert_mods` `WHERE mod_id = workshop` | `WHERE mod_id = resolved_entity` |

New helper: `DatabaseManager.resolve_steam_entity_mod_id(workshop_id, *, app_id, internal_id)`.

### `SteamWorkshopClient` — **FIXED**

| Method | Status |
|--------|--------|
| `refresh_details` | Already provider-only (no DB write) — OK |
| `get_details_batch` | Was cache/upsert by Workshop-as-PK — now resolves Workshop → Internal PK; `upsert_mods` UPDATE-only; unknown Workshop IDs are **not** inserted |

### `scanner` (`core/scanner.py`) — **OK (no DB write)**

Filesystem Workshop folder scan only. Emits `published_file_id` as scan token for Sync. Does not call `upsert_mod`.

### `sync` (`services/sync.py`) — **OK (create via IdentityService)**

- Metadata batch: `get_details_batch` (now UPDATE-only)
- Entity mint: `create_mod_identity(..., operation=LIFECYCLE_SYNC)` only
- Does not pass Workshop ID as `mods.mod_id` outside IdentityService

### `reconcile` (`services/library_reconcile.py`) — **OK**

Documented: must not call `create_mod_identity` / allocate. No `upsert_mod` path found that mints from Workshop.

### Sidecar loader (`services/info_sidecar.py`) — **FIXED**

`apply_sidecar_to_db` previously fell back to `sidecar.published_file_id` as `mod_id`. Now requires caller `mod_id` or `sidecar.internal_id` only; refuses create for missing entity.

### `create_mod_identity` (Steam) — **ADJUSTED**

Still the sole intentional Steam create path (`allow_insert=True`). Resolves created PK after insert; platform bind uses resolved Internal ID.

---

## Tests

**New:** `tests/test_identity_write_boundary.py`

1. `mod_id=465` + `published_file_id=872296228` → upsert keeps PK **465**, no Workshop PK row  
2. No entity → `upsert_mod` refuses auto-create  
3. Steam API `get_details_batch` → UPDATE existing only; orphan Workshop ID not INSERTed  
4. `upsert_mods` resolves Workshop → Internal PK  
5. `resolve_steam_entity_mod_id` coverage  

**Regression (requested):**

```text
pytest tests/test_steam_refresh_identity.py -q   → passed
pytest tests/test_identity_authority_final.py -q → passed
pytest tests/test_identity_write_boundary.py -q  → passed
```

---

## Residual / follow-up

| Item | Notes |
|------|-------|
| Test seeds using `upsert_mod(published_file_id=…)` without create scope | Will no longer INSERT — migrate seeds to `identity_create_scope()` + `allow_insert=True`, or `create_mod_identity` |
| `_ensure_mod_stub` | Still inserts under Identity create guard; Steam-range stub still treats argument as Workshop-shaped — guarded by `refuse_unauthorized_mod_insert` |
| Historical Steam PK == Workshop | Remains valid for IdentityService **create** only; catalog refresh must not rely on it |

---

## Verdict

**Write boundary closed** for Steam catalog / API paths: Workshop ID can no longer silently become a new `mods.mod_id`. Refresh and batch fetch update the resolved Internal PK only.
