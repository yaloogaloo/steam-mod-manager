"""Static guard for the canonical identity boundary. Not a repo-wide grep."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN_KEYS = {
    "token",
    "mod_id",
    "mod_pk",
    "workspace_id",
    "launcher_id",
    "pak_uuid",
}


def _function(path: Path, name: str) -> tuple[str, ast.FunctionDef]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = ast.get_source_segment(source, node) or ""
            return body, node
    raise AssertionError(f"{path.name}:{name} missing")


def _constants_and_attrs(fn: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_canonical_id_reader_ignores_other_identities() -> None:
    path = ROOT / "services" / "canonical_membership.py"
    for name in ("_frozen_internal_id", "canonical_deployed_internal_ids"):
        _body, fn = _function(path, name)
        hit = sorted(_constants_and_attrs(fn) & _FORBIDDEN_KEYS)
        assert hit == [], name


def test_membership_predicate_does_not_require_a_uuid() -> None:
    _body, fn = _function(ROOT / "services" / "canonical_membership.py", "entry_is_deployed")
    calls = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "is_frozen_internal_uuid" not in calls


def test_paradox_ref_construction_does_not_or_mod_id() -> None:
    body, _fn = _function(ROOT / "services" / "paradox_activation.py", "_row_to_ref")
    assert "mod_id" not in body
    assert "internal_id or" not in body


def test_order_tokens_are_not_read_from_pk() -> None:
    paradox_body, _fn = _function(ROOT / "services" / "order_backend.py", "sort_token")
    # First sort_token in the file is ParadoxOrderBackend.
    assert "mod_id" not in paradox_body.split("def bind_token", 1)[0]
    bg3_body, _bg3 = _function(ROOT / "services" / "bg3_activation.py", "sort_token")
    assert "mod_id" not in bg3_body
    handle_body, _handle = _function(
        ROOT / "services" / "paradox_activation.py", "_order_handle_map"
    )
    assert "or pk" not in handle_body
    assert "iid or" not in handle_body
