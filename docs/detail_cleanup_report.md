# Detail Panel Legacy Cleanup Report

**Date:** 2026-09-07  
**Task:** Phase 2 Task 3 — Detail Panel Legacy Cleanup  
**Policy:** Delete only surfaces with zero runtime refs, zero test deps, and no business meaning. Do **not** change metadata layout or the style system.

---

## Effective layout (unchanged)

```
Name (meta_rich_label)
  ↓
Description Frame (meta_desc_frame)
  ↓
Source (meta_source_line)
  ↓
Workspace ID (meta_workspace_line)
  ↓
Actions (footer / op_status_label)
```

---

## Audit scope

| Area | Result |
|------|--------|
| `ui/mod_detail_panel.py` | Audited |
| `ui/styles.py` | Audited |
| `ui/widgets/` | **Absent** — no widget package in tree |

Search hits: `legacy` / `deprecated` / `hidden` / `compat` / `view_` / `old` / `statusBanner` / `detailStatusBanner` / `op_status_label`.

---

## Deleted (Category A)

| Item | Why safe |
|------|----------|
| `_ElidedLabel` alias | Zero callers |
| Empty `_header_actions` QWidget | Never populated; never referenced |
| Dead `view_name` branch in batch mode | Attribute never created |
| CSS `QPushButton#detailButton` | No widget uses `objectName="detailButton"` |
| CSS `QWidget#detailHeaderActions` | Orphan — no matching objectName |

### Composition fix (not a layout redesign)

| Before | After |
|--------|-------|
| `_legacy_host` embedded in scroll body (hidden) | `_offscreen_host` parented to panel, **outside** scroll composition |
| Named “legacy” in view tree | `detailOffscreenHost` — still hidden; fill/wire attributes preserved |

Rename: `_build_legacy_user_tags_section` → `_build_offscreen_user_tags_section`.

---

## Kept (Category B) — delete conditions not met

| Item | Why keep |
|------|----------|
| `detailStatusBanner` / `_show_error_banner` | **Error path** for deploy / irrecoverable failures (tests + runtime) |
| `_show_status_banner` deprecated alias | Tests call it; success tone already refused |
| `op_status_label` | Soft refresh / deploy progress feedback |
| Off-screen status / version / relations / tags widgets | Fill helpers + deploy UI tests (`view_deploy*`, `_rel_lists`, `tag_*`) |
| Hidden header stubs (`btn_edit`, `btn_copy_info`, `btn_copy_link`, `btn_remove_mod`, …) | Wired and/or asserted by tests |
| Hidden metadata mirrors (`view_id`, `view_steam`, `meta_name_line`, …) | Platform metadata tests + fill/copy helpers |
| `meta_desc_browser` alias | Witcher3 / getattr tests |

### `detailStatusBanner` rule

| Feedback | Surface |
|----------|---------|
| Refresh success / soft failure | **`op_status_label` only** — banner success tone refused |
| Deploy / irrecoverable error | **`detailStatusBanner`** (error tone) |

---

## Category C — follow-up (needs test migration first)

| Item | Blocker |
|------|---------|
| Delete off-screen status/version/relations/tags widgets entirely | Deploy / relationship / user-tag tests still bind attributes |
| Delete hidden `view_*` / `meta_name_line` mirrors | `test_platform_metadata_*` assert on mirrors |
| Delete header action stubs | `test_copy_*`, toolbar hide asserts |
| Remove `_show_status_banner` alias | Update callers → `_show_error_banner` |

---

## Tests

**New:** `tests/test_detail_legacy_cleanup.py`

- No `_legacy_host` / `_header_actions` / `_ElidedLabel`
- Off-screen host not in scroll body
- Refresh success → `op_status_label`; banner stays hidden
- Error banner path still works
- Metadata widget order unchanged
- Orphan CSS removed; banner + op_status CSS retained

```text
pytest tests/test_detail_legacy_cleanup.py -q  → 5 passed
pytest tests/test_refresh_button_ux.py -q      → passed
```

---

## Verdict

Safe dead surfaces removed. Visible metadata layout untouched. Status banner retained for **errors only**; refresh continues to use `op_status_label`. Remaining off-screen attribute surfaces are intentional until tests migrate to visible widgets.
