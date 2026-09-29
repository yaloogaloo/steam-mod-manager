"""Deploy public entry identity: Frozen UUID in, SQLite PK only after resolve."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD_ROOTS = (ROOT / "ui", ROOT / "services")
TOOL_ROOTS = (ROOT / "tools", ROOT / "scripts")
ENTRY_ATTRS = frozenset({"deploy_mod", "undeploy_mod", "redeploy_mod"})
FORBIDDEN_ARG_NAMES = frozenset(
    {"pk", "mod_pk", "dal_pk", "mod_id", "workspace_id", "external_id"}
)
PK_ARG_NAMES = frozenset(
    {"pk", "mod_pk", "dal_pk", "pk_a", "pk_b", "pk_main"}
)
INTENTIONAL_REJECT = frozenset(
    {
        "tests/test_identity_semantic_cleanup.py",
        "tests/test_warhammer3_deploy.py",
    }
)
_SIDECAR_DIGIT = re.compile(r"""["']internal_id["']\s*:\s*["']?\d+""")
_SIDECAR_PK = re.compile(r"""["']internal_id["']\s*:\s*(?:pk|mid|str\(\s*created\.mod_id)""")


def _iter_py(roots: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(p for p in root.rglob("*.py") if p.is_file())
    return out


def _arg_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _is_dal_pk_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "_dal_mod_pk"
    if isinstance(func, ast.Attribute):
        return func.attr == "dal_mod_pk"
    return False


def _entry_calls(tree: ast.AST) -> list[tuple[int, str, ast.AST | None]]:
    out: list[tuple[int, str, ast.AST | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "DeployWorker"
        ):
            out.append((node.lineno, "DeployWorker", node.args[0] if node.args else None))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ENTRY_ATTRS
        ):
            out.append(
                (node.lineno, node.func.attr, node.args[0] if node.args else None)
            )
    return out


def test_production_deploy_entry_callers_pass_frozen_uuid() -> None:
    hits: list[str] = []
    for path in _iter_py(PROD_ROOTS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        for lineno, kind, arg0 in _entry_calls(tree):
            if arg0 is None:
                hits.append(f"{rel}:{lineno}: missing identity argument")
                continue
            if _is_dal_pk_call(arg0):
                hits.append(f"{rel}:{lineno}: {kind}(_dal_mod_pk(...))")
                continue
            if isinstance(arg0, ast.Constant) and str(arg0.value).isdigit():
                hits.append(f"{rel}:{lineno}: {kind}(digit PK)")
                continue
            name = _arg_name(arg0)
            short = name.rsplit(".", 1)[-1] if name else ""
            if short in FORBIDDEN_ARG_NAMES:
                hits.append(f"{rel}:{lineno}: {kind}({name})")
    assert hits == [], "production deploy entry still passes PK:\n" + "\n".join(hits)


def test_library_deploy_worker_requires_frozen_uuid() -> None:
    src = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "is_frozen_internal_uuid(mid)" in src
    assert "DeployWorker(" in src
    assert "remove_mod(mid)" in src
    assert "remove_mod(\n            _dal_mod_pk(mid) or mid" not in src


def test_deploy_mod_docstring_names_frozen_uuid() -> None:
    src = (ROOT / "services" / "deploy.py").read_text(encoding="utf-8")
    body = src.split("def deploy_mod", 1)[1].split("def ", 1)[0]
    assert "Frozen Internal UUID" in body
    assert "resolve_deploy_entity" in body
    assert "mod_pk" in body
    for name in ("undeploy_mod", "redeploy_mod"):
        chunk = src.split(f"def {name}", 1)[1].split("def ", 1)[0]
        assert "internal_id" in chunk
        assert "resolve_deploy_entity" in chunk


def test_tooling_deploy_entry_is_frozen_uuid() -> None:
    hits: list[str] = []
    for path in _iter_py(TOOL_ROOTS):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, kind, arg0 in _entry_calls(tree):
            if arg0 is None:
                hits.append(f"{rel}:{lineno}: missing identity")
                continue
            if isinstance(arg0, ast.Constant) and str(arg0.value).isdigit():
                hits.append(f"{rel}:{lineno}: {kind}({arg0.value!r})")
                continue
            name = _arg_name(arg0)
            short = name.rsplit(".", 1)[-1] if name else ""
            if short in PK_ARG_NAMES:
                hits.append(f"{rel}:{lineno}: {kind}({name})")
    assert hits == [], "tooling still passes PK/digit to Deploy:\n" + "\n".join(hits)


def test_smoke_runner_resolves_workspace_not_workshop_digit() -> None:
    src = (ROOT / "tools" / "deploy_smoke_runner.py").read_text(encoding="utf-8")
    assert "resolve_internal_id_from_workspace_id" in src
    assert "DeployWorker(\n        internal_id" in src or "DeployWorker(\n        internal_id," in src
    assert "workspace_id → unique SMM entity → Frozen internal_id" in src
    tree = ast.parse(src)
    for lineno, kind, arg0 in _entry_calls(tree):
        if kind != "DeployWorker":
            continue
        assert arg0 is not None
        assert isinstance(arg0, ast.Name) and arg0.id == "internal_id"


def test_normal_tests_do_not_pass_pk_to_deploy() -> None:
    hits: list[str] = []
    for path in _iter_py((ROOT / "tests",)):
        rel = path.relative_to(ROOT).as_posix()
        if rel in INTENTIONAL_REJECT:
            continue
        if "helpers" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for lineno, kind, arg0 in _entry_calls(tree):
            if arg0 is None:
                continue
            if isinstance(arg0, ast.Constant) and str(arg0.value).isdigit():
                hits.append(f"{rel}:{lineno}: {kind}({arg0.value!r})")
            elif isinstance(arg0, ast.Name) and arg0.id in PK_ARG_NAMES:
                hits.append(f"{rel}:{lineno}: {kind}({arg0.id})")
    assert hits == [], "normal tests still pass PK/digit to Deploy:\n" + "\n".join(hits)


def test_intentional_reject_files_still_call_deploy_with_pk() -> None:
    cleanup = (ROOT / "tests" / "test_identity_semantic_cleanup.py").read_text(
        encoding="utf-8"
    )
    assert "deployer.deploy_mod(pk)" in cleanup
    assert "deployer.undeploy_mod(pk)" in cleanup
    assert "deployer.redeploy_mod(pk)" in cleanup
    wh3 = (ROOT / "tests" / "test_warhammer3_deploy.py").read_text(encoding="utf-8")
    assert "rejected = deployer.deploy_mod(pk)" in wh3


def test_deploy_test_sidecars_do_not_write_pk_as_internal_id() -> None:
    hits: list[str] = []
    for path in _iter_py((ROOT / "tests",)):
        text = path.read_text(encoding="utf-8")
        if "deploy_mod(" not in text and "DeployWorker(" not in text:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _SIDECAR_DIGIT.search(line) or _SIDECAR_PK.search(line):
                hits.append(f"{rel}:{i}:{stripped}")
    assert hits == [], "deploy tests write PK into sidecar internal_id:\n" + "\n".join(
        hits
    )


def test_smoke_seed_resolves_workspace_to_frozen_uuid(tmp_path: Path) -> None:
    from core.db_manager import DatabaseManager
    from services.deploy_identity import is_frozen_internal_uuid
    from tools.deploy_smoke_runner import (
        SUCCESS_WORKSPACE_ID,
        _internal_id_for_workspace,
        _seed_folder_mod,
        _setup_workspace,
    )

    DatabaseManager.reset_instance()
    library, _game_mods, _db_file, db = _setup_workspace(tmp_path / "smoke")
    folder, pk, frozen = _seed_folder_mod(
        db, library, workspace_id=SUCCESS_WORKSPACE_ID, title="SmokeMod"
    )
    resolved = _internal_id_for_workspace(db, SUCCESS_WORKSPACE_ID)
    assert is_frozen_internal_uuid(frozen)
    assert resolved == frozen
    assert resolved != SUCCESS_WORKSPACE_ID
    assert str(pk).isdigit()
    assert frozen != str(pk)
    meta = (folder / ".info" / "metadata.json").read_text(encoding="utf-8")
    assert frozen in meta
    assert f'"internal_id": "{pk}"' not in meta
    DatabaseManager.reset_instance()
