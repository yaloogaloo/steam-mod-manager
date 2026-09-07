#!/usr/bin/env python3
"""One-shot cleanup: historical ``*_900000000000xxxx`` Mod folder copies.

Default is dry-run. Never touches Identity Service / reconcile / sync / import /
deploy / projection. Never deletes DB entities or game directories.

Usage::

    # safe dry-run (default)
    python tools/cleanup_legacy_suffix_folders.py
    python tools/cleanup_legacy_suffix_folders.py --dry-run

    # explicit gated apply
    python tools/cleanup_legacy_suffix_folders.py --apply --confirm

    # one-shot auto-clean (before/after audit + verification)
    python tools/cleanup_legacy_suffix_folders.py --auto-clean
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_REPORT = OUT_DIR / "legacy_suffix_cleanup_report.json"
BEFORE_REPORT = OUT_DIR / "legacy_suffix_cleanup_before.json"
AFTER_REPORT = OUT_DIR / "legacy_suffix_cleanup_after.json"

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")

# Historical duplicate-import copies: <name>_900000000000xxxx
LEGACY_SUFFIX_RE = re.compile(r"^(.+)_900000000000\d*$")

ACTION_DELETE = "delete_legacy_suffix"

# Identity contract suite used by --auto-clean verification.
IDENTITY_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/test_id_architecture_contract.py",
    "tests/test_identity_boundary_contract.py",
    "tests/test_identity_lifecycle_contract_freeze.py",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_legacy_suffix_dirname(name: str) -> bool:
    return bool(LEGACY_SUFFIX_RE.match(_text(name)))


def strip_legacy_suffix(name: str) -> str:
    m = LEGACY_SUFFIX_RE.match(_text(name))
    return m.group(1) if m else ""


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _norm_path(path: str | Path) -> str:
    raw = _text(path)
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower().replace("/", "\\")
    except OSError:
        return raw.lower().replace("/", "\\")


def _read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
    if not folder.is_dir():
        return None, ""
    for info_name in INFO_DIR_NAMES:
        for meta_name in METADATA_NAMES:
            meta = folder / info_name / meta_name
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {"_read_error": True}, str(meta)
            if isinstance(data, dict):
                return data, str(meta)
            return {"_read_error": True}, str(meta)
    return None, ""


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def load_db_path_index(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    con = _connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        wanted = [
            "mod_id",
            "internal_id",
            "workspace_id",
            "app_id",
            "platform",
            "title",
            "last_known_path",
            "folder_present",
        ]
        select = ", ".join(c for c in wanted if c in cols)
        return [{k: r[k] for k in r.keys()} for r in con.execute(f"SELECT {select} FROM mods")]
    finally:
        con.close()


def find_db_rows_for_path(
    rows: list[dict[str, Any]], folder: Path
) -> list[dict[str, Any]]:
    target = _norm_path(folder)
    if not target:
        return []
    hits: list[dict[str, Any]] = []
    for row in rows:
        if _norm_path(row.get("last_known_path")) == target:
            hits.append(row)
    return hits


def find_replacement_folder(
    *,
    game_dir: Path,
    pollution_folder: Path,
    workspace_id: str,
) -> Path | None:
    """
    Same-game non-suffix directory whose ``.info.workspace_id`` matches.

    Prefer the stripped-name sibling when it qualifies.
    """
    wid = _text(workspace_id)
    if not wid or not game_dir.is_dir():
        return None

    preferred_name = strip_legacy_suffix(pollution_folder.name)
    candidates: list[Path] = []
    preferred: Path | None = None

    for child in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.resolve() == pollution_folder.resolve():
            continue
        if is_legacy_suffix_dirname(child.name):
            continue
        info, _ = _read_info(child)
        if not info or info.get("_read_error"):
            continue
        if _text(info.get("workspace_id")) != wid:
            continue
        if preferred_name and child.name == preferred_name:
            preferred = child
        else:
            candidates.append(child)

    if preferred is not None:
        return preferred
    return candidates[0] if candidates else None


def scan_legacy_suffix_folders(
    *,
    library_root: Path,
    db_path: Path,
) -> dict[str, Any]:
    """Dry-run scan. Every matched folder is a delete candidate."""
    rows = load_db_path_index(db_path)
    items: list[dict[str, Any]] = []

    if not library_root.is_dir():
        return {
            "generated_at": _now(),
            "mode": "dry-run",
            "library_root": str(library_root),
            "db_path": str(db_path),
            "summary": {"matched": 0, "db_bound": 0, "rebindable": 0},
            "items": [],
        }

    for game_dir in sorted(
        (p for p in library_root.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    ):
        game = game_dir.name
        for child in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if not is_legacy_suffix_dirname(child.name):
                continue

            info, info_path = _read_info(child)
            meta_iid = _text((info or {}).get("internal_id")) if info else ""
            meta_wid = _text((info or {}).get("workspace_id")) if info else ""
            db_hits = find_db_rows_for_path(rows, child)

            # Prefer DB workspace when rebound; else metadata workspace.
            wid_for_match = ""
            if db_hits:
                wid_for_match = _text(db_hits[0].get("workspace_id")) or meta_wid
            else:
                wid_for_match = meta_wid

            replacement = find_replacement_folder(
                game_dir=game_dir,
                pollution_folder=child,
                workspace_id=wid_for_match,
            )

            db_match: dict[str, Any] | None = None
            if db_hits:
                hit = db_hits[0]
                db_match = {
                    "mod_id": _text(hit.get("mod_id")),
                    "internal_id": _text(hit.get("internal_id"))
                    or _text(hit.get("mod_id")),
                    "workspace_id": _text(hit.get("workspace_id")),
                    "app_id": int(hit.get("app_id") or 0),
                    "last_known_path": _text(hit.get("last_known_path")),
                    "folder_present": int(hit.get("folder_present") or 0),
                }

            path_rebind = "none"
            if db_match and replacement is not None:
                path_rebind = "rebind_to_normal"
            elif db_match:
                path_rebind = "mark_missing"

            items.append(
                {
                    "game": game,
                    "folder": child.name,
                    "path": str(child.resolve()),
                    "metadata": {
                        "internal_id": meta_iid,
                        "workspace_id": meta_wid,
                        "info_path": info_path,
                    },
                    "db_match": db_match,
                    "replacement_folder": (
                        str(replacement.resolve()) if replacement else ""
                    ),
                    "action": ACTION_DELETE,
                    "path_rebind": path_rebind,
                    "confirmed": True,
                }
            )

    summary = {
        "matched": len(items),
        "db_bound": sum(1 for i in items if i.get("db_match")),
        "rebindable": sum(
            1 for i in items if i.get("path_rebind") == "rebind_to_normal"
        ),
        "mark_missing": sum(
            1 for i in items if i.get("path_rebind") == "mark_missing"
        ),
    }
    return {
        "generated_at": _now(),
        "mode": "dry-run",
        "library_root": str(library_root.resolve()),
        "db_path": str(db_path),
        "summary": summary,
        "items": items,
    }


def _update_db_path(
    db_path: Path,
    *,
    mod_id: str,
    last_known_path: str,
    folder_present: bool,
) -> None:
    """Direct path patch only — no identity / lifecycle calls."""
    con = _connect(db_path)
    try:
        con.execute(
            """
            UPDATE mods
            SET last_known_path = ?, folder_present = ?
            WHERE mod_id = ?
            """,
            (
                _text(last_known_path),
                1 if folder_present else 0,
                int(str(mod_id).strip()),
            ),
        )
        con.commit()
    finally:
        con.close()


def apply_legacy_suffix_cleanup(
    plan: dict[str, Any],
    *,
    apply: bool,
    confirm: bool,
    library_root: Path,
    db_path: Path,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """
    Delete matched suffix folders.

    Requires ``--apply --confirm``. Always deletes matched folders under
    ``mod/<game>/<folder>``. Never deletes DB entities.
    """
    root = Path(project_root) if project_root is not None else ROOT
    lib = library_root.resolve()

    result: dict[str, Any] = {
        "generated_at": _now(),
        "applied": False,
        "dry_run": not (apply and confirm),
        "results": [],
    }

    if not (apply and confirm):
        result["reason"] = "apply requires explicit --apply --confirm"
        result["results"] = [
            {
                "path": i.get("path"),
                "action": i.get("action"),
                "skipped": True,
                "reason": "dry-run",
            }
            for i in plan.get("items") or []
        ]
        return result

    result["applied"] = True
    for item in plan.get("items") or []:
        if _text(item.get("action")) != ACTION_DELETE:
            result["results"].append(
                {
                    "path": item.get("path"),
                    "skipped": True,
                    "reason": "not_delete_action",
                }
            )
            continue
        if not item.get("confirmed", True):
            result["results"].append(
                {
                    "path": item.get("path"),
                    "skipped": True,
                    "reason": "not_confirmed",
                }
            )
            continue

        folder = Path(_text(item.get("path")))
        game = _text(item.get("game"))
        name = _text(item.get("folder"))

        # Strict scope: only mod/<game>/<matched_folder> under library.
        expected = (lib / game / name).resolve()
        try:
            folder_res = folder.resolve()
        except OSError:
            folder_res = folder

        if folder_res != expected and _norm_path(folder_res) != _norm_path(expected):
            # Still allow if path is exactly under library/game and matches name.
            ok_scope = (
                _path_under(folder_res, lib)
                and folder_res.parent.name == game
                and folder_res.name == name
                and is_legacy_suffix_dirname(name)
            )
            if not ok_scope:
                result["results"].append(
                    {
                        "path": str(folder),
                        "ok": False,
                        "reason": "outside_allowed_scope",
                    }
                )
                continue

        if not _path_under(folder_res, root):
            result["results"].append(
                {
                    "path": str(folder_res),
                    "ok": False,
                    "reason": "outside_project",
                }
            )
            continue

        if not _path_under(folder_res, lib):
            result["results"].append(
                {
                    "path": str(folder_res),
                    "ok": False,
                    "reason": "outside_mod_library",
                }
            )
            continue

        rebind_note = "none"
        db_match = item.get("db_match") or None
        if isinstance(db_match, dict) and _text(db_match.get("mod_id")):
            mid = _text(db_match.get("mod_id"))
            replacement = _text(item.get("replacement_folder"))
            if replacement and Path(replacement).is_dir():
                _update_db_path(
                    db_path,
                    mod_id=mid,
                    last_known_path=str(Path(replacement).resolve()),
                    folder_present=True,
                )
                rebind_note = "rebind_to_normal"
            else:
                # Clear path so last_known_path never points at a deleted folder.
                _update_db_path(
                    db_path,
                    mod_id=mid,
                    last_known_path="",
                    folder_present=False,
                )
                rebind_note = "mark_missing"

        deleted = False
        if folder_res.is_dir():
            shutil.rmtree(folder_res)
            deleted = True

        result["results"].append(
            {
                "game": game,
                "folder": name,
                "path": str(folder_res),
                "action": ACTION_DELETE,
                "ok": True,
                "deleted": deleted,
                "path_rebind": rebind_note,
                "db_match": db_match,
                "replacement_folder": item.get("replacement_folder") or "",
            }
        )

    return result


def write_report(payload: dict[str, Any], path: Path = DEFAULT_REPORT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def snapshot_db_entities(db_path: Path) -> list[dict[str, Any]]:
    """Minimal entity identity + path snapshot for before/after audits."""
    rows = load_db_path_index(db_path)
    out: list[dict[str, Any]] = []
    for row in rows:
        mid = _text(row.get("mod_id"))
        out.append(
            {
                "mod_id": mid,
                "internal_id": _text(row.get("internal_id")) or mid,
                "workspace_id": _text(row.get("workspace_id")),
                "app_id": int(row.get("app_id") or 0),
                "last_known_path": _text(row.get("last_known_path")),
                "folder_present": int(row.get("folder_present") or 0),
            }
        )
    out.sort(key=lambda r: r["mod_id"])
    return out


def list_legacy_suffix_paths(library_root: Path) -> list[str]:
    paths: list[str] = []
    if not library_root.is_dir():
        return paths
    for game_dir in library_root.iterdir():
        if not game_dir.is_dir():
            continue
        for child in game_dir.iterdir():
            if child.is_dir() and is_legacy_suffix_dirname(child.name):
                paths.append(str(child.resolve()))
    paths.sort()
    return paths


def build_audit_bundle(
    *,
    library_root: Path,
    db_path: Path,
    label: str,
) -> dict[str, Any]:
    plan = scan_legacy_suffix_folders(library_root=library_root, db_path=db_path)
    entities = snapshot_db_entities(db_path)
    return {
        "generated_at": _now(),
        "label": label,
        "library_root": str(library_root.resolve()),
        "db_path": str(db_path),
        "entity_count": len(entities),
        "entities": entities,
        "legacy_suffix_paths": list_legacy_suffix_paths(library_root),
        "scan": plan,
    }


def run_identity_contract_tests(
    *,
    project_root: Path | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """Run frozen identity contract tests; returns pass/fail payload."""
    root = Path(project_root) if project_root is not None else ROOT
    exe = python_exe or sys.executable
    cmd = [
        exe,
        "-m",
        "pytest",
        *IDENTITY_CONTRACT_TESTS,
        "-q",
        "--tb=line",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {
            "ok": False,
            "returncode": -1,
            "command": cmd,
            "error": str(exc),
            "stdout": "",
            "stderr": "",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def verify_auto_clean(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    deleted_paths: list[str],
    library_root: Path,
    run_contract_tests: bool = True,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """
    Auto acceptance checks after --auto-clean.

    1. no leftover ``*_900000000000xxxx`` dirs
    2. no new DB entities
    3. internal_id unchanged per mod_id
    4. workspace_id unchanged per mod_id
    5. no last_known_path points at a deleted folder
    6. identity contract tests pass (optional in unit tests)
    """
    checks: list[dict[str, Any]] = []
    ok = True

    leftover = list_legacy_suffix_paths(library_root)
    c1 = {
        "name": "no_legacy_suffix_dirs",
        "ok": len(leftover) == 0,
        "leftover": leftover,
    }
    checks.append(c1)
    ok = ok and c1["ok"]

    before_ents = {
        _text(e.get("mod_id")): e for e in (before.get("entities") or [])
    }
    after_ents = {
        _text(e.get("mod_id")): e for e in (after.get("entities") or [])
    }
    new_ids = sorted(set(after_ents) - set(before_ents))
    c2 = {
        "name": "no_new_db_entities",
        "ok": len(new_ids) == 0,
        "new_mod_ids": new_ids,
        "before_count": len(before_ents),
        "after_count": len(after_ents),
    }
    checks.append(c2)
    ok = ok and c2["ok"]

    iid_changed: list[dict[str, Any]] = []
    wid_changed: list[dict[str, Any]] = []
    for mid, before_e in before_ents.items():
        after_e = after_ents.get(mid)
        if after_e is None:
            # Entity deletion is forbidden by this tool; treat as failure.
            iid_changed.append(
                {"mod_id": mid, "reason": "entity_missing_after_clean"}
            )
            continue
        if _text(before_e.get("internal_id")) != _text(after_e.get("internal_id")):
            iid_changed.append(
                {
                    "mod_id": mid,
                    "before": before_e.get("internal_id"),
                    "after": after_e.get("internal_id"),
                }
            )
        if _text(before_e.get("workspace_id")) != _text(after_e.get("workspace_id")):
            wid_changed.append(
                {
                    "mod_id": mid,
                    "before": before_e.get("workspace_id"),
                    "after": after_e.get("workspace_id"),
                }
            )

    c3 = {
        "name": "internal_id_unchanged",
        "ok": len(iid_changed) == 0,
        "changed": iid_changed,
    }
    c4 = {
        "name": "workspace_id_unchanged",
        "ok": len(wid_changed) == 0,
        "changed": wid_changed,
    }
    checks.extend([c3, c4])
    ok = ok and c3["ok"] and c4["ok"]

    deleted_norm = {_norm_path(p) for p in deleted_paths if _text(p)}
    dangling: list[dict[str, Any]] = []
    for mid, after_e in after_ents.items():
        lkp = _text(after_e.get("last_known_path"))
        if not lkp:
            continue
        if _norm_path(lkp) in deleted_norm:
            dangling.append({"mod_id": mid, "last_known_path": lkp})
            continue
        # Also reject any still-pointing legacy suffix path that no longer exists.
        try:
            p = Path(lkp)
            if is_legacy_suffix_dirname(p.name) and not p.is_dir():
                dangling.append({"mod_id": mid, "last_known_path": lkp})
        except OSError:
            pass

    c5 = {
        "name": "no_last_known_path_points_at_deleted",
        "ok": len(dangling) == 0,
        "dangling": dangling,
    }
    checks.append(c5)
    ok = ok and c5["ok"]

    if run_contract_tests:
        contract = run_identity_contract_tests(project_root=project_root)
        c6 = {
            "name": "identity_contract_tests",
            "ok": bool(contract.get("ok")),
            "returncode": contract.get("returncode"),
            "stdout_tail": contract.get("stdout", "")[-1000:],
            "stderr_tail": contract.get("stderr", "")[-1000:],
        }
        checks.append(c6)
        ok = ok and c6["ok"]
    else:
        checks.append(
            {
                "name": "identity_contract_tests",
                "ok": True,
                "skipped": True,
                "reason": "run_contract_tests=False",
            }
        )

    return {
        "generated_at": _now(),
        "ok": ok,
        "checks": checks,
    }


def run_auto_clean(
    *,
    library_root: Path,
    db_path: Path,
    project_root: Path | None = None,
    before_path: Path = BEFORE_REPORT,
    after_path: Path = AFTER_REPORT,
    run_contract_tests: bool = True,
) -> dict[str, Any]:
    """
    Auto-clean mode: snapshot → apply all confirmed suffix deletes → snapshot → verify.
    """
    root = Path(project_root) if project_root is not None else ROOT

    before = build_audit_bundle(
        library_root=library_root, db_path=db_path, label="before"
    )
    write_report(before, before_path)

    plan = before.get("scan") or scan_legacy_suffix_folders(
        library_root=library_root, db_path=db_path
    )
    # Ensure every matched item is confirmed for auto-clean.
    for item in plan.get("items") or []:
        item["confirmed"] = True
        item["action"] = ACTION_DELETE

    apply_result = apply_legacy_suffix_cleanup(
        plan,
        apply=True,
        confirm=True,
        library_root=library_root,
        db_path=db_path,
        project_root=root,
    )

    after = build_audit_bundle(
        library_root=library_root, db_path=db_path, label="after"
    )
    deleted_paths = [
        _text(r.get("path"))
        for r in (apply_result.get("results") or [])
        if r.get("deleted") or r.get("ok")
    ]
    # Prefer planned delete paths (covers already-absent folders).
    planned_deleted = [_text(i.get("path")) for i in (plan.get("items") or [])]
    deleted_paths = sorted({*_text_set(deleted_paths), *_text_set(planned_deleted)})

    verification = verify_auto_clean(
        before=before,
        after=after,
        deleted_paths=deleted_paths,
        library_root=library_root,
        run_contract_tests=run_contract_tests,
        project_root=root,
    )
    after["apply_result"] = apply_result
    after["verification"] = verification
    write_report(after, after_path)

    return {
        "generated_at": _now(),
        "mode": "auto-clean",
        "before_path": str(before_path),
        "after_path": str(after_path),
        "before_summary": (before.get("scan") or {}).get("summary"),
        "after_summary": (after.get("scan") or {}).get("summary"),
        "apply_result": apply_result,
        "verification": verification,
        "ok": bool(verification.get("ok")) and bool(apply_result.get("applied")),
    }


def _text_set(values: list[str]) -> set[str]:
    return {_text(v) for v in values if _text(v)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run (default) or delete historical "
            "*_900000000000xxxx Mod folder copies."
        )
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default when neither --apply nor --auto-clean)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply confirmed actions (requires --confirm)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Second gate required with --apply",
    )
    parser.add_argument(
        "--auto-clean",
        action="store_true",
        help=(
            "One-shot: write before.json, apply all confirmed suffix deletes, "
            "write after.json, run acceptance checks"
        ),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Optional existing report JSON to apply",
    )
    parser.add_argument(
        "--skip-contract-tests",
        action="store_true",
        help="Skip identity contract pytest suite during --auto-clean verify",
    )
    args = parser.parse_args(argv)

    from core.paths import database_path, default_mod_library, project_root

    db_path = Path(args.db) if args.db else database_path()
    library = Path(args.library) if args.library else default_mod_library()
    root = project_root()

    if args.auto_clean and args.apply:
        print("error: use either --auto-clean or --apply --confirm", file=sys.stderr)
        return 2

    if args.auto_clean:
        result = run_auto_clean(
            library_root=library,
            db_path=db_path,
            project_root=root,
            before_path=BEFORE_REPORT,
            after_path=AFTER_REPORT,
            run_contract_tests=not args.skip_contract_tests,
        )
        write_report(result, Path(args.report))
        print(f"wrote {BEFORE_REPORT}")
        print(f"wrote {AFTER_REPORT}")
        print(f"wrote {args.report}")
        print(json.dumps(result.get("verification") or {}, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.plan is not None:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)

    if args.apply:
        apply_result = apply_legacy_suffix_cleanup(
            plan,
            apply=True,
            confirm=bool(args.confirm),
            library_root=library,
            db_path=db_path,
            project_root=root,
        )
        report = {
            **plan,
            "mode": "apply" if apply_result.get("applied") else "dry-run",
            "apply_result": apply_result,
        }
        if apply_result.get("applied"):
            after = scan_legacy_suffix_folders(
                library_root=library, db_path=db_path
            )
            report["after_scan_summary"] = after.get("summary")
    else:
        # Default and explicit --dry-run: report only.
        report = {**plan, "mode": "dry-run"}
        if args.dry_run:
            report["dry_run_explicit"] = True

    out = write_report(report, Path(args.report))
    print(f"wrote {out}")
    print(json.dumps(report.get("summary") or {}, ensure_ascii=False))
    if args.apply and not args.confirm:
        print(
            "note: --apply without --confirm performs no mutations",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
