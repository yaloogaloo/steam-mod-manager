"""Per-mod deploy concurrency guard."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock_guard = threading.Lock()
_active: set[str] = set()


def _key(internal_id: str, app_id: int = 0, *, mod_pk: int = 0) -> str:
    mid = str(internal_id or "").strip()
    if app_id > 0:
        return f"{app_id}:{mid}"
    return mid


@contextmanager
def deploy_operation_lock(
    internal_id: str,
    *,
    app_id: int = 0,
    mod_pk: int = 0,
) -> Iterator[None]:
    """
    Reject concurrent deploy/undeploy for the same Frozen entity.

    Lock key is the Frozen UUID. ``mod_pk`` is SQL-only (logged in the error).
    """
    key = _key(internal_id, app_id, mod_pk=mod_pk)
    with _lock_guard:
        if key in _active:
            pk_note = f" mod_pk={mod_pk}" if mod_pk else ""
            raise RuntimeError(
                f"Mod {internal_id}{pk_note} 已有部署任务正在执行，请等待完成后再试"
            )
        _active.add(key)
    try:
        yield
    finally:
        with _lock_guard:
            _active.discard(key)
