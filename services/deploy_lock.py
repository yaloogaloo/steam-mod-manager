"""Per-mod deploy concurrency guard."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock_guard = threading.Lock()
_active: set[str] = set()


def _key(internal_id: str, app_id: int = 0) -> str:
    mid = str(internal_id or "").strip()
    if app_id > 0:
        return f"{app_id}:{mid}"
    return mid


@contextmanager
def deploy_operation_lock(
    internal_id: str,
    *,
    app_id: int = 0,
) -> Iterator[None]:
    """
    Reject concurrent deploy/undeploy for the same mod.

  Raises ``RuntimeError`` when another operation holds the lock.
    """
    key = _key(internal_id, app_id)
    with _lock_guard:
        if key in _active:
            raise RuntimeError(
                f"Mod {internal_id} 已有部署任务正在执行，请等待完成后再试"
            )
        _active.add(key)
    try:
        yield
    finally:
        with _lock_guard:
            _active.discard(key)
