# Agent / contributor guardrails

These invariants are enforced by tests, not by the editor you use.

## UI SAFETY / COVER OWNERSHIP REGRESSION GATE

If you touch any of:

- `ui/mod_detail_panel.py` (`show_mod`, success handlers)
- `ui/dependency_item_widget.py` (`set_items`)
- `ui/window_lifecycle.py` (`setParent`, `detach_owned_widget`)
- `ui/library_view.py` (`notify_mod_changed` flush)
- `services/file_ops.py` (`_build_unified_payload`, `cover_path`)
- `services/info_sidecar.py` (`apply_sidecar_to_db`)
- `services/metadata_refresh.py`

run:

```
python -m pytest tests/test_success_path_ui_invariants.py tests/test_cover_ownership_invariants.py tests/test_widget_detach_lifecycle.py tests/test_ui_lifecycle_static_guard.py
```

Do not treat the task as done if that gate fails.

## COVER PRESENTATION REGRESSION GATE

If card cover markup/loading, `.cover` image sizing, or the Web cover preview
changes, run:

```
python -m pytest tests/test_web_cover_visual.py tests/test_card_scheme_c.py tests/test_web_library.py
```

Also capture the canonical read-only Qt UI with `tools/cover_visual_probe.py`
at two desktop viewports and record the actual DPI (include DPR 1.0 and the
normal desktop DPR when they differ). The probe checks frozen real image
hashes, image bounds, visible source regions, native painted pixels, Inspector
resource identity, placeholders, and return after scrolling.

Container height alone is not a cover-content acceptance criterion. Preserve
the approved references in `tests/fixtures/cover_visual/manifest.json`; only
explicit user visual approval may promote candidate screenshots. Missing
approval remains `USER VISUAL REVIEW PENDING`, even when automated checks pass.

## CANONICAL MEMBERSHIP REGRESSION GATE

Deployed Filter and Sorting Mode both call ``entry_is_deployed`` on the
library snapshot (``mods.deploy_status = deployed``). Order files only
sequence that set.
Launcher descriptors, playsets, ``dlc_load.json``, ``used_mods.txt``, pak
UUIDs, and ``modsettings.lsx`` are external projection: unresolved must not
hide a deployed Mod.

If you touch any of:

- `services/deploy.py`
- `services/order_backend.py`
- `services/canonical_membership.py`
- `services/paradox_activation.py`
- `services/bg3_activation.py`
- `services/wh3_activation.py`
- `ui/library_view.py`
- `ui/library_query.py`

run:

```
python -m pytest tests/test_canonical_membership_contract.py tests/test_canonical_membership_static_guard.py tests/test_canonical_identity_contract.py tests/test_canonical_identity_static_guard.py tests/test_ck3_sort_mode_ui.py tests/test_stellaris_activation.py tests/test_bg3_sort_mode_ui.py tests/test_wh3_activation.py
```

Do not treat the task as done if that gate fails. A non-empty sort list, a
changed order file, or a successful drag is not this contract.

## Invariants

VISIBLE CHILD LIFECYCLE: a visible child is `hide` → detach → `deleteLater`
via `detach_owned_widget`. `setParent(None)` on a visible `QWidget` maps a
top-level window.

USER COVER OWNERSHIP: `user_override_fields.cover` means generic refresh,
official sync, and sidecar rescan must not change `mods.cover_path` or the
live cover bytes. A foreign absolute path (another tree) must not be stored.

SUCCESS PATH UI: cover upload, cover refresh, metadata refresh, and offline
save must not open an unsolicited modal, top-level widget, or Explorer window.
Open Folder and Open Offline are separate explicit actions.

DETAIL SYNC: one logical mutation → one `show_mod` (`_canonical_detail_sync`).
Projection flush updates cards and must not rebuild Detail again for that id.

## Production mutation boundary

Cover, metadata, backup, and deploy writes go through
`services.mutation_context.assert_mutation_allowed`.

- Test / forensic code must activate an explicit temporary root
  (`activate_test_context`). Every target path has to sit under that root.
- Production writes are allowed only after `activate_production_context()`
  in the application entry point (`main._run_gui`). A pytest process
  (`PYTEST_CURRENT_TEST` or `SMM_TEST_DB`) cannot activate that context.
- No context means refuse. Do not fall back to the current library, the
  first visible card, or a drive-letter prefix.

A diagnostic that opens the real `MainWindow` against `data/mod_manager.db`
and clicks cover change is a production-data bug, not a test convenience.

Longer notes: `docs/architecture/UI_LIFECYCLE_AND_COVER_OWNERSHIP.md`.
## SMM Agent 临时工作区（强制）

- 所有 AI/Agent 主动创建的临时文件只能位于本项目 `_tmp/`；不得在 D 盘根目录、其他项目或用户工作区创建测试、证据、回放或临时 worktree。
- 每次任务优先使用 `_tmp/agent_runs/<任务标识>/`。测试数据库、测试归档、Qt 截图、性能采样、日志、缓存和临时 worktree 均纳入 `_tmp/`；按需创建目录，不在项目根散落输出。
- pytest 使用 `_tmp/tests/` 下的独立 `--basetemp`，Python 临时目录由测试进程或具体调用显式配置。临时目录不可写时立即报告，禁止回退至 D 盘根目录。
- 未关闭事故的唯一恢复资料放入 `_tmp/protected_recovery/`，禁止自动过期清理。移动恢复资料必须核验哈希、依赖引用及恢复可用性。
- 任务完成后删除已确认无用的临时产物；删除前核实绝对路径、归属、进程占用及恢复依赖，不跟随链接、junction 或 reparse point。
- 不得为满足 Agent 临时目录规则修改生产业务路径、Windows 全局 TEMP/TMP、真实归档或正式运行时临时资源机制。正式源码、文档和用户数据保持其权威位置。
- 所有后续 Cursor/Agent 任务必须复用本规则，不自行另设证据根目录。现有业务安全及回归门禁继续有效。

Agent 测试优先通过 `tools/run_tests.ps1 <测试路径>` 执行；该入口仅设置子进程临时环境，不修改 Windows 全局配置。

临时 Git worktree 使用 `_tmp/worktrees/<任务标识>`，不得使用会在项目外自动生成工作树的入口。
