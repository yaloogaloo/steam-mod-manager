# Mod Entity Identity Lifecycle Contract

**Status:** FROZEN  
**Audience:** humans and AI agents modifying this repository  
**Authority:** this document is the long-term identity architecture law

---

## Chain of truth

```
DB internal_id  →  .info registration proof  →  Projection  →  UI
```

Anything outside this chain is not an identity authority.

---

## 1. Mod entity definition

A **Mod Entity** is a database row whose durable primary key is:

| Field | Role |
|-------|------|
| `internal_id` (`mods.mod_id` / `mods.internal_id`) | **Only** entity identity |

### Forbidden identity keys

These must never be treated as the Mod entity key:

- `workspace_id`
- filesystem `path` / `last_known_path`
- folder / directory name
- `external_id` alone (without `platform` + `app_id`)

### Allowed non-identity fields

| Field | Role |
|-------|------|
| `workspace_id` | Display / user-facing label only |
| `external_id` | Platform-native id; match only as `(platform, app_id, external_id)` |
| path / folder | Storage carrier for files |

Changing path or folder name must not create, delete, or merge Mod entities.

See also: `.cursor/rules/id-architecture.mdc`.

---

## 2. Registration entry points

### Only allowed creators

1. **Steam Workshop Sync**
2. **User-initiated Import**

Both must go through the Identity Creation Gate (`create_mod_identity` / IdentityService create path).

### Forbidden creators

- Reconcile
- Backup restore
- Orphan auto-import / auto-register
- Deploy / Status / Projection
- Folder scan that invents UUIDs
- Reading forged `.info` and inserting a DB row

If a folder has no DB entity: **ignore or surface for human Import** — never mint.

---

## 3. `.info` rules

`.info` is **registration proof**, not an authority that can invent entities.

### Valid proof

```
DB.internal_id == .info.internal_id
```

Only then may the sidecar participate in path binding / projection.

### Otherwise

Ignore the `.info` (and its folder for identity purposes).

### Forbidden

- Creating a DB Mod from `.info` that does not already exist in DB
- Trusting `.info` when `internal_id` is missing, forged, or not in DB
- Using `.info.workspace_id` to look up or create entities

---

## 4. Directory rules

Directories are **file carriers only**.

### Allowed

- Rename the folder
- Move the folder within / across library roots
- Re-bind `last_known_path` for an existing DB entity when `.info` proves the same `internal_id`

### Forbidden effects

- Rename / move must **not** create a new Mod
- Rename / move must **not** delete a Mod
- Rename / move must **not** change `internal_id` or mint a new `workspace_id` as identity

---

## 5. Reconcile duties

Reconcile may only:

```
existing DB entity  +  valid .info (same internal_id)
        → bind / refresh path relationship
```

### Forbidden

- Mint `internal_id` / UUID
- Call `create_mod_identity`
- Create Mods from orphans
- Resolve entity by `workspace_id`, path, or folder digits
- Auto-fix historical pollution

No `.info` or forged `.info` → ignore (optional: queue human Import). Never create.

---

## 6. Backup duties

Backup may only **restore `.info` for an already-existing DB entity** when identity axes match:

- `internal_id`
- `platform`
- `app_id`
- `external_id`

### Forbidden

- Creating a Mod from backup metadata
- Using backup to invent `internal_id`
- Merging two DB entities because backup looks similar

If ownership is not 100% confirmed: **skip**. Prefer leftover orphan data over wrong entity binding.

---

## 7. `workspace_id` rules

`workspace_id` is a **display field**.

### Forbidden

- `find_mod_by_workspace_id` as a real entity lookup (API must remain a no-op / always `None`)
- UI / Sync / Import / Reconcile using workspace id as primary key
- Showing Internal ID / Steam Workshop ID / Nexus ID as the ordinary user Mod id beside Workspace ID (debug UI excepted)

Derivation (never invert):

```
Steam Workshop ID → external_id → workspace_id
Nexus Mod ID      → external_id → workspace_id
Other             → system-generated workspace_id (never from Internal ID)
```

---

## 8. AI modification protection

When changing code, **do not reintroduce** these patterns:

| Forbidden pattern | Why |
|-------------------|-----|
| Effective `find_mod_by_workspace_id` / workspace reverse lookup | Display field ≠ entity key |
| Path-as-identity resolve | Paths are storage only |
| Folder-name / digit-folder identity (`Unknown Mod 123…`) | Digits in titles are not proof |
| `external_id` global lookup without `platform` + `app_id` | Cross-game collisions |
| Orphan auto-import / auto-create | Only Sync / Import create |
| Reconcile mint UUID / `create_mod_identity` | Reconcile binds paths only |
| Backup → create Mod | Backup restores `.info` only |
| `.info` → INSERT mods | `.info` proves; DB owns |

### Safe defaults for agents

1. Prefer leaving orphan / dirty data over wrong bind.
2. Do not “helpfully” restore old identity shortcuts.
3. Do not expand identity scope in cleanup / UI / deploy PRs.
4. New games needing a version axis need a **new product spec** — do not overload `game_version` (Witcher 3 only).

---

## Related tests (regression locks)

- `tests/test_identity_lifecycle_strict.py`
- `tests/test_identity_lifecycle_contract_freeze.py`
- `tests/test_lifecycle_boundary_architecture.py`
- `tests/test_startup_identity_resolve_architecture.py`
- `tests/test_identity_authority_final.py`

If a change contradicts this contract, **the change is wrong** — update code to obey the contract, do not weaken the contract without an explicit architecture decision.
