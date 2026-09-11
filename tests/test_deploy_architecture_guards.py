"""Deploy architecture guards — permanent lock against a second Deploy pipeline.

Future AI / maintainers: if these fail, you reintroduced Strategy-as-engine,
after-before accounting, Strategy extract, or Core bypass of FilePlan.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from services.deploy import ModDeployer
from services.deploy_rules import (
    Anno1800Strategy,
    CustomPathStrategy,
    FolderCopyStrategy,
    get_strategy,
    supported_deploy_types,
)
from services.deploy_rules import anno as anno_mod
from services.deploy_rules import custom as custom_mod
from services.deploy_rules import duckov as duckov_mod
from services.deploy_rules import generic as generic_mod
from services.deploy_rules import pak_mod_path as pak_mod
from services.deploy_rules import palworld as palworld_mod
from services.deploy_rules import slay_the_spire as sts_mod
from services.deploy_rules import stardew_valley as stardew_mod
from services.deploy_rules import warhammer3 as warhammer3_mod

_STRATEGY_MODULES = (
    anno_mod,
    custom_mod,
    duckov_mod,
    generic_mod,
    pak_mod,
    palworld_mod,
    sts_mod,
    stardew_mod,
    warhammer3_mod,
)

_CORE_MODULES = (
    Path("services/deploy_apply.py"),
    Path("services/deploy_verifier.py"),
    Path("services/deploy_file_plan.py"),
)


def _deploy_method_source(cls: type) -> str:
    return inspect.getsource(cls.deploy)


def _module_source(mod) -> str:
    return Path(mod.__file__).read_text(encoding="utf-8")


# --- Guard 1: Core must not call strategy.deploy() ---


def test_guard1_core_path_does_not_call_strategy_deploy() -> None:
    import textwrap

    src = inspect.getsource(ModDeployer._deploy_with_context)
    # Strip docstrings so contract text mentioning Strategy.deploy is not a false positive.
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:]
    body = ast.unparse(tree)
    assert "strategy.deploy(" not in body
    assert "apply_file_plan" in src
    assert "verify_file_plan" in src
    assert "file_plan_from_strategy_result" in src


# --- Guard 2: Strategy.deploy must not call self.plan() ---


def test_guard2_no_self_plan_inside_deploy() -> None:
    for key in supported_deploy_types():
        strategy = get_strategy(key)
        assert strategy is not None
        src = _deploy_method_source(type(strategy))
        assert "self.plan(" not in src, f"{key}.deploy must not call self.plan()"


# --- Guard 3: Strategy must not call ArchiveExtractor.extract ---


def test_guard3_strategy_modules_no_archive_extractor_extract() -> None:
    for mod in _STRATEGY_MODULES:
        src = _module_source(mod)
        assert "ArchiveExtractor.extract" not in src, (
            f"{mod.__name__} must not call ArchiveExtractor.extract"
        )
        assert "from services.archive_extractor import" not in src
        assert "services.archive_extractor" not in src


# --- Guard 4: after-before / snapshot accounting ---


def test_guard4_no_after_before_snapshot_accounting() -> None:
    for mod in _STRATEGY_MODULES:
        src = _module_source(mod)
        assert "_snapshot_files" not in src, f"{mod.__name__} still has _snapshot_files"
        compact = "".join(src.split())
        assert "after-before" not in compact.lower(), (
            f"{mod.__name__} still has after-before deployment accounting"
        )


# --- Guard 5: post-copy rescan accounting ---


def test_guard5_no_post_copy_rescan_accounting() -> None:
    custom_src = _module_source(custom_mod)
    assert "Rebuild file list after copy" not in custom_src
    deploy_src = _deploy_method_source(CustomPathStrategy)
    assert "_iter_deployable_files" not in deploy_src
    for mod in _STRATEGY_MODULES:
        deploy_fn = None
        tree = ast.parse(_module_source(mod))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == "deploy"
                    ):
                        deploy_fn = item
                        break
        if deploy_fn is None:
            continue
        body_src = ast.get_source_segment(_module_source(mod), deploy_fn) or ""
        assert "rglob" not in body_src
        assert "os.walk" not in body_src


# --- Guard 6: Strategy deploy path must not mutate filesystem ---


def test_guard6_strategy_deploy_no_filesystem_mutation_pipeline() -> None:
    forbidden = (
        "shutil.copy",
        "shutil.copy2",
        "shutil.move",
        "ArchiveExtractor",
        "extract_archive",
        "_copy_file(",
        "_copy_entries(",
    )
    for key in supported_deploy_types():
        strategy = get_strategy(key)
        assert strategy is not None
        src = _deploy_method_source(type(strategy))
        assert "inert_strategy_deploy" in src, (
            f"{key} deploy() must be an inert compatibility shell"
        )
        for token in forbidden:
            assert token not in src, f"{key}.deploy contains forbidden {token}"
    # Dead Strategy-owned copy helpers must not return.
    for mod in _STRATEGY_MODULES:
        src = _module_source(mod)
        assert "def _copy_file(" not in src, f"{mod.__name__} still defines _copy_file"
        assert "def _copy_entries(" not in src, (
            f"{mod.__name__} still defines _copy_entries"
        )
        assert "shutil.copy2" not in src, (
            f"{mod.__name__} must not use shutil.copy2 (Core Apply owns copy)"
        )


# --- Guard 7: Manifest from FilePlan only (no Strategy post-scan reconstruct) ---


def test_guard7_manifest_from_file_plan_not_rescan() -> None:
    from services import deploy_file_plan as plan_mod

    src = Path(plan_mod.__file__).read_text(encoding="utf-8")
    assert "def manifest_from_file_plan" in src
    fn_src = inspect.getsource(plan_mod.manifest_from_file_plan)
    assert "rglob" not in fn_src
    assert "os.walk" not in fn_src
    assert "safe_iter_files" not in fn_src
    assert "plan.files" in fn_src

    core = inspect.getsource(ModDeployer._deploy_with_context)
    assert "manifest_from_file_plan" in core or "strategy_result_from_file_plan" in core


# --- Guard 8: Core stages consume FilePlan ---


def test_guard8_core_stages_use_fileplan() -> None:
    src = inspect.getsource(ModDeployer._deploy_with_context)
    assert "file_plan_from_strategy_result" in src
    assert "apply_file_plan" in src
    assert "verify_file_plan" in src
    # Backup uses planned targets from FilePlan (target_absolutes / planned_targets).
    assert "target_absolutes" in src or "planned_targets" in src
    apply_src = Path("services/deploy_apply.py").read_text(encoding="utf-8")
    assert "plan.files" in apply_src
    assert "_snapshot_files" not in apply_src
    verify_src = Path("services/deploy_verifier.py").read_text(encoding="utf-8")
    assert "def verify_file_plan" in verify_src
    vfn = inspect.getsource(
        __import__("services.deploy_verifier", fromlist=["verify_file_plan"]).verify_file_plan
    )
    assert "rglob" not in vfn
    assert "os.walk" not in vfn


def test_guard_archive_extract_only_via_deploy_apply() -> None:
    apply_src = Path("services/deploy_apply.py").read_text(encoding="utf-8")
    assert "ArchiveExtractor.extract" in apply_src
    assert "def extract_archive_via_core" in apply_src
    deploy_src = Path("services/deploy.py").read_text(encoding="utf-8")
    assert "ArchiveExtractor.extract" not in deploy_src
    assert "extract_archive_via_core" in deploy_src


def test_guard_code_level_contracts_present() -> None:
    """Docstrings must state ownership even if docs/ are unread."""
    base = Path("services/deploy_rules/base.py").read_text(encoding="utf-8")
    assert "PATH-MAPPING ADAPTER" in base or "path-mapping adapter" in base.lower()
    assert "DeployFilePlan" in base

    plan = Path("services/deploy_file_plan.py").read_text(encoding="utf-8")
    assert "SINGLE SOURCE OF TRUTH" in plan

    apply = Path("services/deploy_apply.py").read_text(encoding="utf-8")
    assert "DEPLOYMENT EXECUTION BOUNDARY" in apply

    verify = Path("services/deploy_verifier.py").read_text(encoding="utf-8")
    assert "DEPLOYMENT SUCCESS AUTHORITY" in verify

    deploy = Path("services/deploy.py").read_text(encoding="utf-8")
    assert "ARCHITECTURE CONTRACT" in deploy
    assert "Strategy.deploy" in deploy


def test_folder_and_anno_deploy_shells_are_inert() -> None:
    for cls in (FolderCopyStrategy, CustomPathStrategy, Anno1800Strategy):
        src = _deploy_method_source(cls)
        assert "inert_strategy_deploy" in src
        assert "self.plan(" not in src
