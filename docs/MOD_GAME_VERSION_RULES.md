# Mod game_version rules

This is a **game-compatibility dimension**, not Mod identity and not a Mod version string.

## Current product scope

`game_version` belongs to **The Witcher 3 only**.

```text
Witcher 3:
    game_version ∈ {original, next_gen, remake}
    default = next_gen

Other games:
    game_version = NULL / not applicable
    no version UI
```

Do **not** treat this as “every game has a version”. Adding the same dimension to another game is a **new explicit product requirement**, not a generalization of this file.

Identify Witcher 3 with the stable Steam App ID (`292030`) via `core.witcher3_game_version.is_witcher3_game`. Do not guess from folder names or Mod titles. Do not use `game.name == "巫师3"` as the primary check.

## Stored values vs UI labels

Internal persistence (English tokens only):

| Token       | UI (zh) |
|-------------|---------|
| `original`  | 原版    |
| `next_gen`  | 次世代版 |
| `remake`    | 重制版  |

Default for Witcher 3: **`next_gen`**.

Never store Chinese labels in SQLite or sidecar.

## What game_version is not

```text
game_version
  ≠ mod_version
  ≠ Mod.io version
  ≠ Steam Workshop revision
  ≠ game API version
  ≠ mod_id / Internal ID
  ≠ external_id / published_file_id / Mod.io mod ID
  ≠ workspace_id
  ≠ source_url
```

Mod.io payloads may include a field named `version`. That field must **never** be copied into `game_version`.

## Identity boundary

Changing `original` → `next_gen` (or any other legal pair) must **not**:

- allocate a new Internal ID
- change `external_id` / Workshop ID / Mod.io ID
- change `workspace_id` or `source_url`
- create a new Mod entity

Do not solve version problems by rebuilding identity or minting a duplicate directory.

## UI

- Witcher 3: show `版本` combo (原版 / 次世代版 / 重制版) in the metadata editor. Load the **database** value, not a hardcoded default, when opening an existing Mod.
- Other games: hide the field entirely. Do not show 未知 / 不适用 / 次世代版.
- Batch edit: do not expose this field.

## Create / import

Every normal create or import of a Witcher 3 Mod must persist `game_version = next_gen` unless the user later changes it.

Non-Witcher-3 Mods must stay `NULL`. Never batch-assign `next_gen` to all games.

## Preserve on lifecycle operations

`rename`, `move`, path lifecycle, library reconcile, duplicate handling, metadata refresh (Steam / Mod.io / manual / automatic) must **keep** the stored `game_version`.

Do not change identity algorithms to implement this rule.

## Historical data

Idempotent migration:

- Witcher 3 + `NULL` / empty / illegal token → `next_gen`
- non-Witcher 3 → `NULL` (never `next_gen`)

Do not migrate by deleting or recreating Mod rows.
