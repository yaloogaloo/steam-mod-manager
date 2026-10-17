"""Explicit boundary for production Mod mutations.

Test and diagnostic code may call the real cover, metadata, backup, and
deploy services. Those services write only when a context is active:

* ``test`` — every target path must sit under an explicit temporary root
* ``production`` — activated only by the application entry point

No context means refuse. There is no fallback to the current library, the
first visible card, or a path-prefix guess such as ``E:\\mod``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class MutationBoundaryError(RuntimeError):
    """A mutation was attempted without an explicit, matching context."""


@dataclass(frozen=True)
class MutationContext:
    mode: str
    roots: tuple[Path, ...]


class _Token:
    """Previous context, so activate/reset works across Qt worker threads.

    ``contextvars`` do not cross ``QThread``. A process-wide value does:
    the application opts into production once, and a test process opts
    into a temporary root once.
    """

    def __init__(self, previous: MutationContext | None) -> None:
        self.previous = previous


_CURRENT: MutationContext | None = None


def current_mutation_context() -> MutationContext | None:
    return _CURRENT


def activate_test_context(roots: list[Path | str]) -> _Token:
    """Allow mutations only under these temporary roots."""
    global _CURRENT
    resolved = tuple(_resolve_root(root) for root in roots)
    if not resolved:
        raise MutationBoundaryError(
            "test mutation context requires an explicit temporary root"
        )
    token = _Token(_CURRENT)
    _CURRENT = MutationContext(mode="test", roots=resolved)
    return token


def activate_production_context() -> _Token:
    """Allow production mutations. Call this only from the application entry.

    A pytest process cannot opt in. ``SMM_TEST_DB`` and ``PYTEST_CURRENT_TEST``
    are the process boundary, so a test cannot widen its temporary root to
    the real library by calling this function.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SMM_TEST_DB"):
        raise MutationBoundaryError(
            "production context cannot be activated from a test process"
        )
    global _CURRENT
    token = _Token(_CURRENT)
    _CURRENT = MutationContext(mode="production", roots=())
    return token


def reset_mutation_context(token: _Token) -> None:
    global _CURRENT
    _CURRENT = token.previous


def suspend_mutation_context() -> _Token:
    """Clear the context. Tests use this to prove the default is refuse."""
    global _CURRENT
    token = _Token(_CURRENT)
    _CURRENT = None
    return token


def assert_mutation_allowed(*paths: Path | str | None) -> MutationContext:
    """Refuse the write unless the active context explicitly allows it.

    Production mode is itself the permission (the process opted in).
    Test mode refuses when any target is missing or outside its roots.
    """
    ctx = _CURRENT
    if ctx is None:
        raise MutationBoundaryError(
            "mutation refused: no explicit test or production context"
        )
    if ctx.mode == "production":
        return ctx
    if ctx.mode != "test":
        raise MutationBoundaryError(f"mutation refused: unknown context mode {ctx.mode}")
    cleaned = [Path(path) for path in paths if path]
    if not cleaned:
        raise MutationBoundaryError(
            "mutation refused: test context has no explicit target path"
        )
    for path in cleaned:
        if not any(_is_under(path, root) for root in ctx.roots):
            raise MutationBoundaryError(
                f"mutation refused: {path} is outside the temporary data root"
            )
    return ctx


def _resolve_root(root: Path | str) -> Path:
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _is_under(path: Path, root: Path) -> bool:
    try:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True
