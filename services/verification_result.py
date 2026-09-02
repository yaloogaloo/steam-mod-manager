"""Machine-readable P0 mutation / verification status.

Tests never imply production. Plan never implies apply. Apply never implies verified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

CODE_IMPLEMENTED = "CODE_IMPLEMENTED"
CODE_MISSING = "CODE_MISSING"
TESTED = "TESTED"
UNTESTED = "UNTESTED"
TEST_FAILED = "TEST_FAILED"
DRY_RUN_PLANNED = "DRY_RUN_PLANNED"
NO_PLAN = "NO_PLAN"
NOT_APPLIED = "NOT_APPLIED"
APPLY_EXECUTED = "APPLY_EXECUTED"
APPLY_FAILED = "APPLY_FAILED"
UNCHANGED = "UNCHANGED"
MUTATED = "MUTATED"
UNKNOWN = "UNKNOWN"
NOT_VERIFIED = "NOT_VERIFIED"
APPLY_UNVERIFIED = "APPLY_UNVERIFIED"
PRODUCTION_VERIFIED = "PRODUCTION_VERIFIED"
PRODUCTION_DELETED_AND_VERIFIED = "PRODUCTION_DELETED_AND_VERIFIED"


@dataclass
class VerificationResult:
    code_status: str = CODE_IMPLEMENTED
    test_status: str = UNTESTED
    plan_status: str = NO_PLAN
    apply_status: str = NOT_APPLIED
    production_status: str = UNCHANGED
    verification_status: str = NOT_VERIFIED
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = dict(self.evidence or {})
        return payload

    def mark_apply_unverified(self, **evidence: Any) -> None:
        """ACTION succeeded (or ran) but post-checks failed — never PRODUCTION_VERIFIED."""
        if self.apply_status == APPLY_EXECUTED:
            self.production_status = MUTATED
        self.verification_status = APPLY_UNVERIFIED
        extra = dict(self.evidence or {})
        extra.update(evidence)
        self.evidence = extra


def cannot_claim_production_verified(*, critical: int, high: int, apply_status: str) -> str:
    if apply_status != APPLY_EXECUTED:
        return NOT_VERIFIED
    if int(critical or 0) or int(high or 0):
        return APPLY_UNVERIFIED
    return APPLY_UNVERIFIED


def production_verified_or_unverified(
    *,
    critical: int,
    high: int,
    requires_review: int = 0,
    extra_checks_passed: bool = False,
) -> str:
    """PRODUCTION_VERIFIED only when post-apply protocol checks all pass."""
    if not extra_checks_passed:
        return APPLY_UNVERIFIED
    if int(critical or 0) or int(high or 0) or int(requires_review or 0):
        return APPLY_UNVERIFIED
    return PRODUCTION_VERIFIED
