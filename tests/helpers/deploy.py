"""Deploy test helpers aligned with Core FilePlan lifecycle.

Production contract (``ModDeployer._deploy_with_context``)::

    strategy.plan() → FilePlan → apply_file_plan → verify_file_plan

``Strategy.deploy`` is inert — do not patch or call it for Core behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from services.deploy_apply import apply_file_plan
from services.deploy_file_plan import (
    file_plan_core_applicable,
    file_plan_from_strategy_result,
    strategy_result_from_file_plan,
)
from services.deploy_rules.base import DeployContext, StrategyResult
from services.deploy_verifier import verify_file_plan


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def execute_strategy_fileplan(strategy: Any, ctx: DeployContext) -> StrategyResult:
    """
    Run ``plan → FilePlan → apply → verify`` for strategy unit tests.

    Replaces obsolete ``strategy.deploy(ctx)`` filesystem mutation.
    """
    planned = strategy.plan(ctx)
    if not planned.success:
        return planned
    file_plan = file_plan_from_strategy_result(planned, ctx)
    if not file_plan_core_applicable(file_plan):
        return StrategyResult(
            success=False,
            error="FilePlan not Core-applicable",
            deploy_type=str(planned.deploy_type or ctx.deploy_type or ""),
            target=str(planned.target or ""),
        )
    apply_out = apply_file_plan(file_plan)
    if not apply_out.success:
        return StrategyResult(
            success=False,
            error=str(apply_out.error or "apply failed"),
            deploy_type=file_plan.deploy_type,
            target=file_plan.target_root,
        )
    verify_out = verify_file_plan(file_plan)
    if not verify_out.success:
        return StrategyResult(
            success=False,
            error=str(verify_out.error or "verify failed"),
            deploy_type=file_plan.deploy_type,
            target=file_plan.target_root,
        )
    return strategy_result_from_file_plan(
        file_plan, deploy_time=_utc_now(), success=True
    )


def patch_apply_then_unlink_targets(
    *,
    only: Path | None = None,
    raise_after: BaseException | None = None,
    partial_write: tuple[Path, str] | None = None,
) -> Any:
    """
    Wrap ``services.deploy_apply.apply_file_plan`` for failure-lifecycle tests.

    - Default: real apply, then unlink all (or ``only``) targets → verify fails
      with ``reason=missing_targets``.
    - ``partial_write`` + ``raise_after``: simulate mid-apply crash (rollback).
    """
    from services.deploy_apply import apply_file_plan as real_apply

    def _wrapped(plan: Any, *args: Any, **kwargs: Any) -> Any:
        if raise_after is not None:
            if partial_write is not None:
                path, body = partial_write
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            raise raise_after
        out = real_apply(plan, *args, **kwargs)
        if getattr(out, "success", False):
            if only is not None:
                only.unlink(missing_ok=True)
            else:
                for entry in list(getattr(plan, "files", None) or []):
                    target = Path(str(getattr(entry, "target_absolute", "") or ""))
                    if target.is_file():
                        target.unlink(missing_ok=True)
        return out

    return patch("services.deploy_apply.apply_file_plan", _wrapped)
