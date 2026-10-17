# Mod Entity Identity Lifecycle Contract

**Status:** FROZEN — Minimal Model (Identity Convergence)  
**Audience:** humans and AI agents modifying this repository  
**Authority:** this document is the long-term identity architecture law

---

## Final Identity Model

Only **two** ID concepts:

| Concept | Role |
|---------|------|
| `internal_id` | Sole **entity** identity (DB PK / `.info` / Reconcile / Backup / Deploy assoc / Projection assoc) |
| `workspace_id` | External platform **registration** number + UI display |

```
Steam Workshop Sync / User Import
    → (platform, app_id, workspace_id) registration
    → create_mod_identity()
    → internal_id entity
    → .info proof
    → Reconcile binds path
    → Library displays
```

Create entries: **Steam Workshop Sync** and **Import** only.

Stable shape:

```
DB Entity:     internal_id
                   |
                   +---- workspace_id (display / registration number)

Registration:  platform + app_id + workspace_id
```

There is **no** third Mod identity.

### SQLite implementation mapping

Frozen `internal_id` is **not** INTEGER `mods.mod_id`.

| Frozen concept | SQLite |
|----------------|--------|
| Entity Identity (`internal_id`) | `mods.internal_id` TEXT |
| Platform / display (`workspace_id`) | `mods.workspace_id` |
| Implementation PK / FK target | `mods.mod_id` INTEGER |

Business callers resolve `internal_id → find_mod_by_internal_id / resolve_mod_pk → mods.mod_id → DB/FK`.
`.info/entity_key` is filesystem registration proof of the same TEXT identity
(value equals Entity `internal_id` — not a third Mod ID).

---

## ID Boundary

### `internal_id`

Unique Mod **entity** identity.

Authority: highest.

Used for: DB entity, `.info`, Reconcile bind, Backup restore proof, Deploy association, Library Projection association.

Any question “is this the same Mod?” → **only** `internal_id`.

### `workspace_id`

External **platform identifier** (Steam Workshop ID, Nexus Mod ID, etc.).

Allowed: Sync / Import **registration** rematch via `find_mod_for_registration`.

Forbidden: global entity query; Library / Reconcile / Backup / Deploy / UI entity logic; merge key.

`find_mod_by_workspace_id` is a permanent no-op.

### `app_id`

**Game scope** — not a Mod identity, not a third Mod ID.

Allowed: registration matching as part of `(platform, app_id, workspace_id)` with `app_id > 0`.

Forbidden:

- stored as Mod identity
- substitute for `internal_id`
- standalone Mod query (“find mod by app_id”)

### `external_id`

**Legacy metadata only** (historical audit column). Retained; not dropped in hygiene phase.

Forbidden:

- Identity lookup
- Duplicate detection (except deprecated alias that delegates to registration)
- Reconcile binding
- Backup restore identity

Do **not** restore the old multi-identity model:

`internal_id + workspace_id + external_id + app_id` as four Mod identities.

---

## 1. `internal_id`

- Answers: “is this the same Mod?”
- Create **only** via `create_mod_identity()` from Steam Sync or User Import
- Never mint from path / folder / workspace reverse lookup

## 2. `workspace_id`

- Registration key scoped as `(platform, app_id, workspace_id)` with `app_id > 0`
- Lookup API: `find_mod_for_registration` — **Sync / Import / create gate only**
- `find_mod_by_workspace_id` is a **permanent no-op**
- Forbidden for: Library query, Reconcile bind, Backup restore, Deploy locate, UI entity logic, merge

Same `workspace_id` on different `app_id` ⇒ **two** Mods (e.g. Nexus BG3 1333 ≠ Stardew 1333).

Same `(platform, app_id, workspace_id)` ⇒ **one** Mod (duplicate registration must rematch / refuse a second entity).

---

## 3. Deleted identity concepts

Must not participate in identity:

- `external_id` (legacy; registration uses `workspace_id`)
- `published_file_id`
- `workshop_id`
- `sidecar_published_file_id`
- path / folder name as identity
- `source_url` as entity key (metadata / optional Sync assist only)

Do **not** add `platform_id` / `external_key` / `registration_id` or any third identity field.

---

## 4. `.info` rules

Must contain: `internal_id`, `workspace_id`  
Must not use for identity: `external_id`, `published_file_id`, `workshop_id`

Match: read `.info/entity_key` (legacy `.info` key `internal_id` accepted) → DB.
Missing / forged ⇒ ignore (never create / merge). `entity_key` value equals
Entity `internal_id` — it is not a third Mod ID.

---

## 5. Reconcile

`.info` → `internal_id` → DB → update `last_known_path`  
Forbidden: create, mint UUID, workspace match, external match, folder digits.

---

## 6. Backup

Restore `.info` only when `backup.internal_id == DB.internal_id`.  
Forbidden: create Mod; infer from workspace / external / folder name.

---

## 6b. Orphan auto

Orphan auto-import is bind-only: never create entities; never invent identity from folder digits.

---

## 7. Data hygiene (non-lifecycle)

Hygiene may **plan** `workspace_id` corrections only after manual confirm.

Forbidden in hygiene: create / delete / merge; modify `internal_id` or `app_id`; auto-apply.

See: `tools/identity_data_hygiene_audit.py`, `tools/identity_data_hygiene_plan.py`.

---

## 8. AI / contributor protection

Forbidden patterns:

- Effective `find_mod_by_workspace_id` entity lookup
- `find_mod_by_external` as a separate identity axis (use registration API)
- Path / folder-name identity
- Orphan auto / backup auto-create
- Reconcile `create_mod_identity`
- Treating `app_id` / `external_id` / `published_file_id` / `workshop_id` as a third Mod identity

If a module still needs a third persisted identity key: **stop** and prove necessity.

See also: `.cursor/rules/id-architecture.mdc`, `tests/test_identity_minimal_model.py`, `tests/test_identity_boundary_contract.py`.
