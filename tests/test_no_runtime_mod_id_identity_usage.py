"""Guard: runtime identity domains must not use ambiguous ``mod_id`` naming.

Scoped modules (deploy / status / projection / archive / reconcile) must:

- use ``internal_id`` for entity-identity parameters
- never emit ``mod_id=`` log keys
- use ``published_file_id`` for Steam Workshop archive identifiers

Allowed to keep ``mod_id``:

- SQL / schema references
- ``row[\"mod_id\"]`` / persistence JSON keys (e.g. DeployManifest)
- calls into DB / out-of-scope APIs that still take ``mod_id=``
- plural batch helpers like ``mod_ids`` / ``exclude_mod_id`` / ``expected_mod_id``
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCOPED_FILES: tuple[str, ...] = (
    "services/deploy.py",
    "services/deploy_paths.py",
    "services/deploy_status.py",
    "services/deploy_lock.py",
    "services/deploy_stage_log.py",
    "services/deploy_result.py",
    "services/deploy_file_plan.py",
    "services/deploy_security.py",
    "services/deployment_record.py",
    "services/content_status_eval.py",
    "services/mod_library_cache.py",
    "services/mod_projection_events.py",
    "services/archive.py",
    "services/archive_observability.py",
    "services/library_reconcile.py",
)

# deploy_rules except manifest.py (manifest keeps JSON field ``mod_id``)
DEPLOY_RULES_GLOB = "services/deploy_rules/*.py"
MANIFEST_KEEP = "services/deploy_rules/manifest.py"

# External APIs that still use keyword ``mod_id=`` (DB / file_ops / backup).
ALLOWED_CALLEE_KEYWORDS = frozenset(
    {
        "list_mod_list_items",
        "has_local_mod_payload",
        "is_missing_mod_content",
        "mark_deployed",
        "discover_folder_by_internal_id",  # expected_mod_id kw stays
        "get_mod_files",
        "get_mod",
        "get_mod_backup_row",
        "update_mod_identity_fields",
        "update_mod_content_status",
        "update_mod_deploy_status",
        "rescan_mod_folder",
        "DeployManifest",  # JSON field
        "save_manifest",
        "load_manifest",
        "infer_initial_source_type",  # Workshop heuristic API
        "sanitize_platform_external_id",  # authority API kw
        "persist_workspace_id",  # identity_service API kw
    }
)

ALLOWED_PARAM_NAMES = frozenset(
    {
        "mod_ids",
        "exclude_mod_id",
        "expected_mod_id",
        "source_mod_id",
        "target_mod_id",
        "candidate_mod_id",
    }
)

LOG_FORBIDDEN = re.compile(r"\bmod_id=")


def _scoped_paths() -> list[Path]:
    paths: list[Path] = []
    for rel in SCOPED_FILES:
        paths.append(ROOT / rel)
    for path in (ROOT / "services" / "deploy_rules").glob("*.py"):
        if path.name == "manifest.py":
            continue
        if path.name == "__init__.py":
            continue
        paths.append(path)
    return [p for p in paths if p.is_file()]


def _is_allowed_mod_id_param(name: str) -> bool:
    if name in ALLOWED_PARAM_NAMES:
        return True
    if name.endswith("_mod_id") and name != "mod_id":
        return True
    return False


class _ParamCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bad: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_args(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_args(node)
        self.generic_visit(node)

    def _check_args(self, node: ast.AST) -> None:
        args = getattr(node, "args", None)
        if args is None:
            return
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            if arg.arg == "mod_id" and not _is_allowed_mod_id_param(arg.arg):
                self.bad.append(
                    f"{getattr(node, 'name', '<fn>')}:{getattr(node, 'lineno', 0)}"
                )


def test_no_runtime_mod_id_entity_parameters() -> None:
    bad: list[str] = []
    for path in _scoped_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _ParamCollector()
        collector.visit(tree)
        for item in collector.bad:
            bad.append(f"{path.relative_to(ROOT)}:{item}")
    assert not bad, "entity identity params must be internal_id, found mod_id:\n" + "\n".join(
        bad
    )


def _looks_like_mod_id_log_line(line: str) -> bool:
    """True only for log-ish strings that embed mod_id=, not out-of-scope API kwargs."""
    stripped = line.strip()
    if not LOG_FORBIDDEN.search(line):
        return False
    if stripped.startswith("#"):
        return False
    if "Forbidden" in line or "never emit" in line.lower():
        return False
    api_kw_markers = (
        "is_missing_mod_content(",
        "has_local_mod_payload(",
        "list_mod_list_items(",
        "mark_deployed(",
        "DeployManifest(",
        "infer_initial_source_type(",
        "sanitize_platform_external_id(",
        "persist_workspace_id(",
    )
    if any(m in line for m in api_kw_markers):
        return False
    if "mod_id=man.mod_id" in line or re.search(r"\bmod_id=man\.", line):
        return False
    if "[DEPLOY" in line:
        return True
    if "logger." in line:
        return True
    if re.search(r"['\"].*mod_id=%", line):
        return True
    if re.search(r"f['\"].*mod_id=", line):
        return True
    return False


def test_no_runtime_mod_id_log_keys() -> None:
    bad: list[str] = []
    for path in _scoped_paths():
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _looks_like_mod_id_log_line(line):
                bad.append(f"{path.relative_to(ROOT)}:{i}:{stripped[:120]}")
    assert not bad, "logs must not use mod_id=; use internal_id= or published_file_id=:\n" + "\n".join(
        bad
    )


def test_archive_uses_published_file_id_not_internal_for_workshop() -> None:
    obs = (ROOT / "services" / "archive_observability.py").read_text(encoding="utf-8")
    assert "published_file_id=" in obs
    assert "mod_id=" not in obs
    assert "def log_archive_start" in obs
    assert "published_file_id" in obs


def test_deploy_manifest_json_key_preserved() -> None:
    """DB/manifest compatibility: on-disk JSON key remains ``mod_id``."""
    text = (ROOT / MANIFEST_KEEP).read_text(encoding="utf-8")
    assert '"mod_id": self.mod_id' in text or '"mod_id":' in text
    assert "mod_id: str" in text
