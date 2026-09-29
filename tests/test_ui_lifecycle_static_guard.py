"""Static guards: visible-child detach + success-path popups + cover clobber."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ui"

# QThread / helper internals — not visible QWidget children.
_SETPARENT_NONE_ALLOW = {
    "ui/window_lifecycle.py",
    "ui/mod_detail_dialog.py",
}

_SUCCESS_METHODS = {
    "ui/mod_detail_panel.py": (
        "_change_cover",
        "_on_metadata_refresh_finished_body",
        "_on_offline_archive_finished",
    ),
}


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _fn_has_setparent_none(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name != "setParent":
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and arg0.value is None:
            return True
        if isinstance(arg0, ast.Name) and arg0.id == "None":
            return True
    return False


def _fn_has_safe_detach(fn: ast.AST, source: str) -> bool:
    text = ast.get_source_segment(source, fn) or ""
    if "detach_owned_widget(" in text:
        return True
    if ".hide(" in text:
        return True
    return False


def test_ui_setparent_none_uses_lifecycle_helper() -> None:
    """Production UI must not detach a visible child with raw setParent(None)."""
    violations: list[str] = []
    for path in _py_files(UI_ROOT):
        rel = _rel(path)
        if rel in _SETPARENT_NONE_ALLOW:
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _fn_has_setparent_none(node):
                continue
            if _fn_has_safe_detach(node, src):
                continue
            violations.append(f"{rel}:{node.name}")
    assert violations == [], (
        "setParent(None) on a QWidget must use detach_owned_widget or hide() first: "
        + ", ".join(violations)
    )


def test_success_paths_do_not_raise_information_dialogs() -> None:
    """Cover / refresh / offline success must not pop QMessageBox.information."""
    violations: list[str] = []
    for rel, names in _SUCCESS_METHODS.items():
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        wanted = set(names)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in wanted:
                continue
            text = ast.get_source_segment(src, node) or ""
            if "QMessageBox.information" in text:
                violations.append(f"{rel}:{node.name}")
    assert violations == [], (
        "success path must not create an unrequested information dialog: "
        + ", ".join(violations)
    )


def test_build_unified_payload_does_not_clobber_cover() -> None:
    path = ROOT / "services" / "file_ops.py"
    src = path.read_text(encoding="utf-8")
    assert 'key == "cover_path"' in src
    assert "payload.get(\"cover_path\")" in src or "payload.get('cover_path')" in src
    assert "cover_reference_is_foreign" in src


_SUCCESS_EXTERNALS = (
    "os.startfile",
    "QDesktopServices",
    "webbrowser.open",
    "explorer.exe",
    "ShellExecute",
)


def test_offline_and_cover_success_do_not_launch_shell() -> None:
    """Save webpage / cover / refresh success must not open Explorer or a browser."""
    import inspect

    from ui.mod_detail_panel import ModDetailPanel

    for name in (
        "_change_cover",
        "_on_metadata_refresh_finished_body",
        "_on_offline_archive_finished",
        "_download_offline_page",
    ):
        text = inspect.getsource(getattr(ModDetailPanel, name))
        for token in _SUCCESS_EXTERNALS:
            assert token not in text, f"{name} launches external UI via {token}"


def test_success_paths_use_one_canonical_detail_sync() -> None:
    import inspect

    from ui.mod_detail_panel import ModDetailPanel

    for name in (
        "_change_cover",
        "_on_metadata_refresh_finished_body",
        "_on_offline_archive_finished",
    ):
        text = inspect.getsource(getattr(ModDetailPanel, name))
        assert "_canonical_detail_sync" in text, name
        assert text.count("_reload_current_detail_from_projection") == 1, name
        assert "show_mod(" not in text, name


def test_agents_gate_is_repository_level() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "UI SAFETY / COVER OWNERSHIP REGRESSION GATE" in agents
    assert "tests/test_success_path_ui_invariants.py" in agents
    assert "tests/test_cover_ownership_invariants.py" in agents
    assert "VISIBLE CHILD LIFECYCLE" in agents
    assert "USER COVER OWNERSHIP" in agents
    for rel in (
        "tests/test_success_path_ui_invariants.py",
        "tests/test_cover_ownership_invariants.py",
        "tests/test_widget_detach_lifecycle.py",
        "tests/test_ui_lifecycle_static_guard.py",
    ):
        assert (ROOT / rel).is_file(), rel
    flush = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "_projection_skip_detail" in flush
    assert "_suppress_projection_detail_rebuild" in flush
