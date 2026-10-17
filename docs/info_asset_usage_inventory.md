# Phase 6 — `.info` asset usage inventory (historical; updated Phase 12)

Current lifecycle: `docs/asset_lifecycle_final_status.md`.

`.info` holds metadata, `manifest.json`, `internal_id`, and `index.html`. Offline **bytes** live in Asset Store. Capture staging is `cache/temp/offline_staging`. OPEN uses `cache/offline_view`.

Do not treat leftover `.info/assets` directories as Source of Truth.

## Durable vs materialized (current)

| Role | Location |
|------|----------|
| Durable content | `data/asset_store/` (SHA-256) |
| LIVE description | `.info[/offline]/manifest.json` + `index.html` + metadata / `internal_id` |
| Ephemeral OPEN | `cache/offline_view/` |
| Capture staging | `cache/temp/offline_staging/` |
| Historical leftover (not SoT) | `.info/assets`, `.info/offline/assets`, Backup `offline/assets` |

---

## Write paths

Production capture writers write **staging**, then finalize. They must not create durable `.info/assets`.

Repair is `repair_live_from_cas` (LIVE manifest + CAS verify). It does not materialize into `.info/assets`.

Migration / leftover GC still **read** leftover trees (class C). See `docs/phase12_deleted_code_report.md`.

---

## Read / OPEN

OPEN: `manifest → Asset Store → cache/offline_view → browser`.

Detail presence probe may note Backup `offline/index.html` exists; it does not open leftover `offline/assets`.

---

## Feature gate

`CAS_ONLY_INFO_ASSET_RUNTIME` default **ON**. Gate-OFF Repair that wrote LIVE `assets/` was removed in Phase 12.
