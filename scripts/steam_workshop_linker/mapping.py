"""Plan and apply Workshop Junction mapping. SMM is the only real store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import GameConfig
from database import DatabaseError, lookup_steam_mod, list_steam_workspace_ids, open_readonly
from junction import (
    create_junction,
    is_junction,
    is_reparse_point,
    junction_points_at,
    lexists,
    get_junction_target,
)
from metadata import normalize_workspace_id, scan_smm_workspace_index
from resolve import resolve_smm_mod_path
from safety import (
    SafetyError,
    assert_roots_exist,
    assert_target_is_safe,
    assert_workshop_path_in_root,
    delete_workshop_path,
)


def decide_workshop_action(
    *,
    exists: bool,
    is_link: bool,
    correct_target: bool,
) -> str:
    """Return create / keep / repair / replace."""
    if not exists:
        return "create"
    if is_link:
        return "keep" if correct_target else "repair"
    return "replace"


@dataclass(frozen=True)
class PlanItem:
    action: str
    workspace_id: str
    smm_path: Path | None
    workshop_path: Path
    reason: str
    old_target: str = ""
    status: str = ""


@dataclass
class SyncResult:
    game: GameConfig
    aborted: bool = False
    abort_reason: str = ""
    dry_run: bool = True
    workshop_mods: int = 0
    registered: int = 0
    unregistered: int = 0
    created: int = 0
    repaired: int = 0
    replaced: int = 0
    kept: int = 0
    missing_workshop: int = 0
    skipped_safety: int = 0
    errors: int = 0
    error_lines: list[str] = field(default_factory=list)
    items: list[PlanItem] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    # Aliases kept so older tests still read ignored_unregistered.
    @property
    def ignored_unregistered(self) -> int:
        return self.unregistered

    @property
    def smm_steam_mods(self) -> int:
        return self.registered


def _log(result: SyncResult, tag: str, message: str, *extra: str) -> None:
    line = f"[{tag}] {message}"
    result.logs.append(line)
    print(line, flush=True)
    for item in extra:
        detail = f"      {item}"
        result.logs.append(detail)
        print(detail, flush=True)


def list_workshop_names(workshop_root: Path) -> list[str]:
    names: list[str] = []
    try:
        for entry in os_scandir_dirs(workshop_root):
            names.append(entry)
    except OSError:
        return []
    names.sort(key=str.casefold)
    return names


def os_scandir_dirs(root: Path) -> list[str]:
    import os

    names: list[str] = []
    with os.scandir(root) as iterator:
        for entry in iterator:
            try:
                if entry.is_dir(follow_symlinks=False) or is_reparse_point(Path(entry.path)):
                    names.append(entry.name)
            except OSError:
                continue
    return names


def classify_item(workshop_path: Path, smm_path: Path) -> tuple[str, str]:
    if not lexists(workshop_path):
        return "create", ""
    if is_reparse_point(workshop_path) or is_junction(workshop_path):
        old = get_junction_target(workshop_path)
        old_text = str(old) if old is not None else ""
        if junction_points_at(workshop_path, smm_path):
            return "keep", old_text
        return "repair", old_text
    return "replace", ""


def _status_for_action(action: str) -> str:
    if action == "create":
        return "REGISTERED + CREATE"
    if action == "keep":
        return "REGISTERED + KEEP"
    if action == "repair":
        return "REGISTERED + REPAIR"
    if action == "replace":
        return "REGISTERED + REPLACE"
    return action.upper()


def build_plan(game: GameConfig, db_path: Path | None = None) -> SyncResult:
    result = SyncResult(game=game)
    try:
        assert_roots_exist(game.workshop_root, game.smm_mod_root)
    except SafetyError as exc:
        result.aborted = True
        result.abort_reason = str(exc)
        return result
    if db_path is None:
        result.aborted = True
        result.abort_reason = "database path required"
        return result

    try:
        conn = open_readonly(db_path)
    except DatabaseError as exc:
        result.aborted = True
        result.abort_reason = str(exc)
        return result

    fs_index = None

    def index() -> dict:
        nonlocal fs_index
        if fs_index is None:
            fs_index = scan_smm_workspace_index(game.smm_mod_root)
        return fs_index

    try:
        workshop_names = list_workshop_names(game.workshop_root)
        result.workshop_mods = len(workshop_names)
        seen: set[str] = set()
        for name in workshop_names:
            _plan_workshop_name(result, game, conn, name, index, seen)
        for workspace_id in list_steam_workspace_ids(conn, game.app_id):
            if workspace_id in seen:
                continue
            if normalize_workspace_id(workspace_id) is None:
                continue
            _plan_registered_missing(result, game, conn, workspace_id, index, seen)
    finally:
        conn.close()
    return result


def _plan_workshop_name(result, game, conn, name, index, seen) -> None:
    workshop_path = game.workshop_root / name
    workspace_id = normalize_workspace_id(name)
    if workspace_id is None:
        result.skipped_safety += 1
        result.items.append(
            PlanItem(
                "skip",
                name,
                None,
                workshop_path,
                "INVALID_WORKSPACE_ID",
                status="INVALID_WORKSPACE_ID",
            )
        )
        return
    seen.add(workspace_id)
    try:
        assert_workshop_path_in_root(workshop_path, game.workshop_root)
    except SafetyError as exc:
        result.errors += 1
        result.error_lines.append(f"{workspace_id}: {exc}")
        result.items.append(
            PlanItem("error", workspace_id, None, workshop_path, str(exc), status="ERROR")
        )
        return
    rows = lookup_steam_mod(conn, app_id=game.app_id, workspace_id=workspace_id)
    if not rows:
        result.unregistered += 1
        result.items.append(
            PlanItem(
                "skip",
                workspace_id,
                None,
                workshop_path,
                "UNREGISTERED + IGNORE",
                status="UNREGISTERED + IGNORE",
            )
        )
        return
    if len(rows) > 1:
        result.skipped_safety += 1
        result.items.append(
            PlanItem(
                "skip",
                workspace_id,
                None,
                workshop_path,
                "AMBIGUOUS_REGISTRATION",
                status="AMBIGUOUS_REGISTRATION",
            )
        )
        return
    _plan_registered_row(result, game, rows[0], workspace_id, workshop_path, index)


def _plan_registered_missing(result, game, conn, workspace_id, index, seen) -> None:
    workshop_path = game.workshop_root / workspace_id
    seen.add(workspace_id)
    rows = lookup_steam_mod(conn, app_id=game.app_id, workspace_id=workspace_id)
    if len(rows) != 1:
        if len(rows) > 1:
            result.skipped_safety += 1
            result.items.append(
                PlanItem(
                    "skip",
                    workspace_id,
                    None,
                    workshop_path,
                    "AMBIGUOUS_REGISTRATION",
                    status="AMBIGUOUS_REGISTRATION",
                )
            )
        return
    _plan_registered_row(result, game, rows[0], workspace_id, workshop_path, index)


def _plan_registered_row(result, game, row, workspace_id, workshop_path, index) -> None:
    resolved = resolve_smm_mod_path(
        row,
        workspace_id=workspace_id,
        workshop_path=workshop_path,
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
        fs_index=index(),
    )
    if resolved.path is None:
        result.skipped_safety += 1
        reason = resolved.reason or "SMM_PATH_NOT_FOUND"
        result.items.append(
            PlanItem("skip", workspace_id, None, workshop_path, reason, status=reason)
        )
        return
    try:
        assert_target_is_safe(
            resolved.path,
            workshop_path=workshop_path,
            workshop_root=game.workshop_root,
            smm_mod_root=game.smm_mod_root,
        )
    except SafetyError as exc:
        result.skipped_safety += 1
        result.items.append(
            PlanItem(
                "skip",
                workspace_id,
                resolved.path,
                workshop_path,
                "INVALID_TARGET",
                status="INVALID_TARGET",
            )
        )
        result.error_lines.append(f"{workspace_id}: {exc}")
        return
    result.registered += 1
    action, old_target = classify_item(workshop_path, resolved.path)
    result.items.append(
        PlanItem(
            action,
            workspace_id,
            resolved.path,
            workshop_path,
            row.internal_id,
            old_target,
            status=_status_for_action(action),
        )
    )
    if action == "create":
        result.missing_workshop += 1


def apply_plan(result: SyncResult, *, dry_run: bool) -> SyncResult:
    result.dry_run = dry_run
    game = result.game
    if result.aborted:
        _log(result, "ERROR", result.abort_reason)
        return result

    _log(
        result,
        "GAME",
        f"{game.name}  app_id={game.app_id}",
        f"SMM:      {game.smm_mod_root}",
        f"Workshop: {game.workshop_root}",
        "DRY RUN" if dry_run else "EXECUTE",
        f"execute={not dry_run}",
        f"dry_run={dry_run}",
    )

    for item in result.items:
        try:
            _apply_item(result, item, dry_run=dry_run)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            result.errors += 1
            result.error_lines.append(f"{item.workspace_id}: {exc}")
            _log(
                result,
                "ERROR",
                f"{item.action} failed",
                f"app_id: {game.app_id}",
                f"workspace_id: {item.workspace_id}",
                f"SMM: {item.smm_path}",
                f"Workshop: {item.workshop_path}",
                f"reason: {exc}",
            )
    return result


def _apply_item(result: SyncResult, item: PlanItem, *, dry_run: bool) -> None:
    game = result.game
    smm = item.smm_path
    status = item.status or item.reason or item.action
    if item.action == "skip":
        if status == "UNREGISTERED + IGNORE":
            return
        _log(
            result,
            status,
            item.workspace_id,
            f"app_id={game.app_id}",
            f"Workshop={item.workshop_path}",
            f"reason={item.reason}",
        )
        return
    if item.action == "error":
        _log(
            result,
            "ERROR",
            item.workspace_id,
            f"app_id={game.app_id}",
            f"SMM={smm}",
            f"Workshop={item.workshop_path}",
            f"reason={item.reason}",
        )
        return
    if item.action == "keep":
        result.kept += 1
        _log(
            result,
            "KEEP",
            item.workspace_id,
            "REGISTERED + KEEP",
            f"app_id={game.app_id}",
            f"SMM={smm}",
            f"Workshop={item.workshop_path}",
        )
        return
    if smm is None:
        raise SafetyError(f"missing SMM path for {item.workspace_id}")
    assert_target_is_safe(
        smm,
        workshop_path=item.workshop_path,
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )

    if item.action == "create":
        if dry_run:
            result.created += 1
            _log(
                result,
                "WOULD CREATE",
                item.workspace_id,
                "REGISTERED + CREATE",
                f"app_id={game.app_id}",
                f"SMM={smm}",
                f"Workshop={item.workshop_path}",
            )
            return
        create_junction(item.workshop_path, smm)
        result.created += 1
        _log(
            result,
            "CREATE",
            item.workspace_id,
            "REGISTERED + CREATE",
            f"app_id={game.app_id}",
            f"SMM={smm}",
            f"Workshop={item.workshop_path}",
        )
        return

    if item.action == "repair":
        if dry_run:
            result.repaired += 1
            _log(
                result,
                "WOULD REPAIR",
                item.workspace_id,
                "REGISTERED + REPAIR",
                f"app_id={game.app_id}",
                f"old target: {item.old_target or '(unknown)'}",
                f"new target: {smm}",
            )
            return
        delete_workshop_path(
            item.workshop_path,
            workshop_root=game.workshop_root,
            smm_target=smm,
            smm_mod_root=game.smm_mod_root,
        )
        create_junction(item.workshop_path, smm)
        result.repaired += 1
        _log(
            result,
            "REPAIR",
            item.workspace_id,
            "REGISTERED + REPAIR",
            f"app_id={game.app_id}",
            f"old target: {item.old_target or '(unknown)'}",
            f"new target: {smm}",
        )
        return

    if item.action == "replace":
        if dry_run:
            result.replaced += 1
            _log(
                result,
                "WOULD DELETE + LINK",
                item.workspace_id,
                "REGISTERED + REPLACE",
                f"app_id={game.app_id}",
                f"SMM={smm}",
                f"Workshop={item.workshop_path}",
            )
            return
        delete_workshop_path(
            item.workshop_path,
            workshop_root=game.workshop_root,
            smm_target=smm,
            smm_mod_root=game.smm_mod_root,
        )
        create_junction(item.workshop_path, smm)
        result.replaced += 1
        _log(
            result,
            "DELETE + LINK",
            item.workspace_id,
            "REGISTERED + REPLACE",
            "deleted real Workshop directory; created Junction",
            f"app_id={game.app_id}",
            f"SMM={smm}",
            f"Workshop={item.workshop_path}",
        )
        return

    raise SafetyError(f"unknown action {item.action!r}")


def sync_game(game: GameConfig, *, dry_run: bool, db_path: Path | None = None) -> SyncResult:
    result = build_plan(game, db_path)
    return apply_plan(result, dry_run=dry_run)


def print_summary(result: SyncResult) -> None:
    game = result.game
    title = "DRY RUN 完成（未修改文件系统）" if result.dry_run else "同步完成"
    if result.aborted:
        title = "已中止（未执行删除）"
    print()
    print("==============================")
    print(title)
    print("==============================")
    print()
    print(f"{game.name} ({game.app_id})")
    print()
    print(f"Workshop Mods: {result.workshop_mods}")
    print()
    print("Registered:")
    print(f"  {result.registered}")
    print()
    print("Unregistered / Ignored:")
    print(f"  {result.unregistered}")
    print()
    print("KEEP:")
    print(f"  {result.kept}")
    print()
    print("CREATE:")
    print(f"  {result.created}")
    print()
    print("REPAIR:")
    print(f"  {result.repaired}")
    print()
    print("REPLACE:")
    print(f"  {result.replaced}")
    print()
    print("Skipped / Safety:")
    print(f"  {result.skipped_safety}")
    print()
    print("Errors:")
    print(f"  {result.errors}")
    if result.aborted:
        print()
        print(f"ABORT: {result.abort_reason}")
    if result.errors and result.error_lines:
        print()
        print("Error details:")
        for line in result.error_lines:
            print(f"  - {line}")
