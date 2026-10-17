"""Static guard: external projection must not define Sorting Mode membership."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_MEMBERSHIP_FUNCTIONS = (
    ("services/order_backend.py", "is_sortable_member"),
    ("services/bg3_activation.py", "is_sortable_member"),
)

_FORBIDDEN = (
    "map_to_launcher_id",
    "parse_mod_descriptor",
    "resolve_bg3_mod_metadata",
    "inspect_bg3_membership",
    "_descriptor_exists",
    "get_sortable_mods",
    "is_file",
    "exists",
)


def _calls(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def test_membership_predicates_do_not_probe_external_projection() -> None:
    violations: list[str] = []
    for rel, fn_name in _MEMBERSHIP_FUNCTIONS:
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != fn_name:
                continue
            found = True
            hit = sorted(_calls(node) & set(_FORBIDDEN))
            if hit:
                violations.append(f"{rel}:{fn_name} calls {hit}")
        if not found:
            violations.append(f"{rel}:{fn_name} missing")
    assert violations == []


def test_library_sort_mode_uses_canonical_membership() -> None:
    path = ROOT / "ui" / "library_view.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_view_filter":
            target = node
            break
    assert target is not None
    body = ast.get_source_segment(source, target) or ""
    assert "entry_is_deployed" in body
    assert "map_to_launcher_id" not in body
    assert "is_sortable_member" not in body
    assert "modsettings" not in body
    assert "ugc_" not in body


def test_deployed_filter_uses_canonical_membership() -> None:
    source = (ROOT / "ui" / "library_query.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("matches_status_filter", "index_matches_current_view"):
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        body = ast.get_source_segment(source, fn) or ""
        assert "entry_is_deployed" in body, name
        assert "map_to_launcher_id" not in body
        assert "is_file" not in body
