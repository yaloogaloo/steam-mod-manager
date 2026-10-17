# Import Design Invariants

Living constraints for Mod import. Do not invert identity rules in
`.cursor/rules/id-architecture.mdc`.

## Nexus Offline HTML Import Invariants

1. **Offline HTML metadata must be parsed before final directory naming.**
   `parse_offline_page_identity` / `parse_nexus_offline_html` run during
   identity resolve, before `materialize_imported_mod`. Attach/scrape after
   a successful import may still fill leftover fields.

2. **A valid parsed Mod title is the canonical directory name.**
   Example: parsed title `Tales of The Witcher - NEW WORLD - Cintra DLC`
   → `<game>/Tales of The Witcher - NEW WORLD - Cintra DLC/`.
   Callers pass that title into `NexusImporter.import_mod` via
   `canonical_nexus_offline_import_title`. Do not keep the HTML filename
   or a temp stub folder name when a real title exists.

3. **`Empty Mod <random>` is only a fallback when no usable title exists.**
   Empty local path still needs a temp payload folder. That stub must not
   become the final library directory once Offline HTML yielded a title.

4. **Internal IDs must never be treated as external platform IDs.**
   Derivation is `Nexus Mod ID → external_id → workspace_id`.
   Internal Database ID (`mods.mod_id`) is not a Workspace ID, not a
   Nexus Mod ID, and not a folder name.

5. **Rename must keep filesystem, DB, metadata, sidecar and identity consistent.**
   If a folder was already created as `Empty Mod <random>`, canonical
   rename goes through `path_lifecycle.record_filesystem_rename` (see
   `attach_nexus_offline_page` → `_maybe_rename_empty_mod_folder_to_parsed_title`).
   Isolated `os.rename` / `shutil.move` is forbidden here.

6. **Existing conflicting directories must never be silently deleted or overwritten.**
   If `<parsed title>/` already exists, do not `rmtree`, merge, or replace.
   Keep both trees and report `identity/path conflict`.

## Import Dialog (UI)

Offline HTML filename display must not drive dialog width. The status
label uses a compact sizeHint plus paint-time elide; the full path stays
in the tooltip. Do not “fix” this by hard-coding a large dialog width.
