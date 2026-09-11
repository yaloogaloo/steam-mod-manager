"""Temporary import-crash diagnostics.

Logs full traceback to ``data/import_crash_trace.log`` and the module logger.
Does not swallow: ``traced`` re-raises. Existing except-handlers should call
``log_exception`` then keep their original control flow.
"""

from __future__ import annotations

import faulthandler
import functools
import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger("crash_trace")

F = TypeVar("F", bound=Callable[..., Any])

_HOOKS_INSTALLED = False
_FAULT_FILE = None


def crash_log_path() -> Path:
    from core.paths import data_dir

    return data_dir() / "import_crash_trace.log"


def log_exception(where: str, **ctx: Any) -> None:
    """Write the active exception with traceback to file + logger."""
    extra = " ".join(f"{k}={v!r}" for k, v in ctx.items() if v is not None)
    header = f"[CRASH_TRACE] where={where}" + (f" {extra}" if extra else "")
    logger.exception("%s", header)
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 72 + "\n")
            fh.write(header + "\n")
            traceback.print_exc(file=fh)
            fh.flush()
    except Exception:  # noqa: BLE001
        logger.exception("[CRASH_TRACE] failed to write crash log file")


def traced(where: str) -> Callable[[F], F]:
    """Wrap *fn*; on exception log full traceback then re-raise (no swallow)."""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any):
            try:
                return fn(*args, **kwargs)
            except Exception:
                log_exception(where)
                raise

        return wrapper  # type: ignore[return-value]

    return deco


def install_crash_hooks() -> None:
    """faulthandler + sys/thread exception hooks. Idempotent. Not business logic."""
    global _HOOKS_INSTALLED, _FAULT_FILE
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True
    path = crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _FAULT_FILE = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:  # noqa: BLE001
        logger.exception("[CRASH_TRACE] faulthandler.enable failed")
        try:
            faulthandler.enable(all_threads=True)
        except Exception:  # noqa: BLE001
            pass

    def _excepthook(exc_type, exc, tb) -> None:  # noqa: ANN001
        logger.critical(
            "[CRASH_TRACE] where=sys.excepthook type=%s",
            getattr(exc_type, "__name__", exc_type),
            exc_info=(exc_type, exc, tb),
        )
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "=" * 72 + "\n")
                fh.write("[CRASH_TRACE] where=sys.excepthook\n")
                traceback.print_exception(exc_type, exc, tb, file=fh)
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    _prev_thread_hook = threading.excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "[CRASH_TRACE] where=threading.excepthook thread=%s",
            getattr(args.thread, "name", None),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "=" * 72 + "\n")
                fh.write(
                    f"[CRASH_TRACE] where=threading.excepthook thread={getattr(args.thread, 'name', None)}\n"
                )
                traceback.print_exception(
                    args.exc_type, args.exc_value, args.exc_traceback, file=fh
                )
        except Exception:  # noqa: BLE001
            pass
        _prev_thread_hook(args)

    threading.excepthook = _thread_hook
    logger.info("[CRASH_TRACE] hooks installed log=%s", path)

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return

    def _qt_handler(mode, context, message) -> None:  # noqa: ANN001
        text = str(message or "")
        loc = ""
        try:
            loc = f" file={getattr(context, 'file', None)} line={getattr(context, 'line', None)}"
        except Exception:  # noqa: BLE001
            loc = ""
        header = f"[CRASH_TRACE] where=qt_message mode={mode}{loc} msg={text}"
        if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
            logger.critical("%s", header)
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("\n" + "=" * 72 + "\n")
                    fh.write(header + "\n")
                    traceback.print_stack(file=fh)
            except Exception:  # noqa: BLE001
                pass
        else:
            logger.debug("%s", header)

    try:
        qInstallMessageHandler(_qt_handler)
    except Exception:  # noqa: BLE001
        logger.exception("[CRASH_TRACE] qInstallMessageHandler failed")
