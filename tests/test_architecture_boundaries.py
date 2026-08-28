"""Architecture import guards — prevent Library / Deployment domain cross-pollution."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

LIBRARY_DOMAIN_FILES = (
    REPO_ROOT / "services" / "mod_refresh.py",
    REPO_ROOT / "services" / "library_reconcile.py",
    REPO_ROOT / "services" / "metadata_refresh.py",
    REPO_ROOT / "services" / "modio_metadata_refresh.py",
    REPO_ROOT / "services" / "local_file_index.py",
)

FORBIDDEN_IN_LIBRARY = frozenset(
    {
        "services.mod_source_integrity",
        "services.deploy_verifier",
        "services.deploy_errors",
        "mod_source_integrity",
        "deploy_verifier",
        "deploy_errors",
        "DeploySourceError",
    }
)

DEPLOYMENT_DOMAIN_FILES = (
    REPO_ROOT / "services" / "mod_source_integrity.py",
    REPO_ROOT / "services" / "deploy_verifier.py",
)

FORBIDDEN_IN_DEPLOYMENT = frozenset(
    {
        "services.mod_refresh",
        "services.library_reconcile",
        "services.metadata_refresh",
        "services.modio_metadata_refresh",
        "mod_refresh",
        "library_reconcile",
        "metadata_refresh",
        "modio_metadata_refresh",
    }
)


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = alias.asname or alias.name.split(".")[-1]
                aliases[key] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                key = alias.asname or alias.name
                aliases[key] = f"{node.module}.{alias.name}"
    return aliases


def _collect_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _module_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                found.add(node.module.split(".")[-1])
            for alias in node.names:
                found.add(alias.name)
                if node.module:
                    found.add(f"{node.module}.{alias.name}")
    found.update(aliases.values())
    found.update(aliases.keys())
    return found


def _violations(imports: set[str], forbidden: frozenset[str]) -> list[str]:
    hits: list[str] = []
    for item in sorted(imports):
        for bad in forbidden:
            if item == bad or item.endswith(f".{bad}") or item.startswith(f"{bad}."):
                hits.append(item)
                break
    return hits


@pytest.mark.parametrize("path", LIBRARY_DOMAIN_FILES, ids=lambda p: p.name)
def test_library_domain_files_do_not_import_deployment_validation(path: Path) -> None:
    imports = _collect_imports(path)
    bad = _violations(imports, FORBIDDEN_IN_LIBRARY)
    assert not bad, f"{path.name} imports forbidden Deployment symbols: {bad}"


@pytest.mark.parametrize("path", DEPLOYMENT_DOMAIN_FILES, ids=lambda p: p.name)
def test_deployment_domain_files_do_not_import_library_refresh(path: Path) -> None:
    imports = _collect_imports(path)
    bad = _violations(imports, FORBIDDEN_IN_DEPLOYMENT)
    assert not bad, f"{path.name} imports forbidden Library refresh modules: {bad}"


def test_local_file_index_has_no_deploy_source_error_reference() -> None:
    text = (REPO_ROOT / "services" / "local_file_index.py").read_text(encoding="utf-8")
    assert "DeploySourceError" not in text
    assert "mod_source_integrity" not in text
    assert "deploy_verifier" not in text
    assert "validate_archive_content" not in text
    assert "has_deployable_source" not in text


def test_mod_refresh_swallows_local_file_reconcile_errors() -> None:
    source = (REPO_ROOT / "services" / "mod_refresh.py").read_text(encoding="utf-8")
    assert "local file reconcile failed" in source
    assert "local_file_reconcile_failed" in source
    assert "reconcile_local_files" in source
    assert "mod_source_integrity" not in source
