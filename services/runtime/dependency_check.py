"""Verify declared Python dependencies at application startup."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

INSTALL_HINT = "pip install -r requirements.txt"


@dataclass(frozen=True)
class RuntimeDependency:
    """One importable package declared in requirements.txt."""

    pip_name: str
    import_name: str
    required: bool = True


# Mirrors requirements.txt — import_name is the module used at runtime.
RUNTIME_DEPENDENCIES: tuple[RuntimeDependency, ...] = (
    RuntimeDependency("PySide6", "PySide6"),
    RuntimeDependency("requests", "requests"),
    RuntimeDependency("PySocks", "socks"),
    RuntimeDependency("curl_cffi", "curl_cffi"),
    RuntimeDependency("beautifulsoup4", "bs4"),
    RuntimeDependency("lxml", "lxml"),
    RuntimeDependency("playwright", "playwright"),
    RuntimeDependency("py7zr", "py7zr"),
    RuntimeDependency("rarfile", "rarfile"),
    RuntimeDependency("psutil", "psutil", required=False),
)


@dataclass(frozen=True)
class DependencyStatus:
    pip_name: str
    import_name: str
    ok: bool
    version: str
    required: bool


def check_runtime_dependencies(
    dependencies: Sequence[RuntimeDependency] | None = None,
) -> list[DependencyStatus]:
    """Return import status for each declared dependency."""
    items = list(dependencies or RUNTIME_DEPENDENCIES)
    results: list[DependencyStatus] = []
    for dep in items:
        try:
            module = importlib.import_module(dep.import_name)
            version = str(getattr(module, "__version__", "OK"))
            results.append(
                DependencyStatus(
                    pip_name=dep.pip_name,
                    import_name=dep.import_name,
                    ok=True,
                    version=version,
                    required=dep.required,
                )
            )
        except ImportError:
            results.append(
                DependencyStatus(
                    pip_name=dep.pip_name,
                    import_name=dep.import_name,
                    ok=False,
                    version="MISSING",
                    required=dep.required,
                )
            )
    return results


def missing_required_dependencies(
    statuses: Sequence[DependencyStatus] | None = None,
) -> list[DependencyStatus]:
    rows = list(statuses or check_runtime_dependencies())
    return [row for row in rows if row.required and not row.ok]


def format_missing_dependency_message(
    missing: Sequence[DependencyStatus],
) -> str:
    names = sorted({row.pip_name for row in missing})
    joined = "\n".join(f"- {name}" for name in names)
    return (
        "缺少 Python 依赖:\n"
        f"{joined}\n\n"
        "请运行:\n"
        f"{INSTALL_HINT}"
    )


def log_runtime_dependencies(
    log: logging.Logger | None = None,
    *,
    prefix: str = "[RUNTIME_DEPENDENCY]",
) -> list[DependencyStatus]:
    """Log one line per dependency and return the full status list."""
    active = log or logger
    statuses = check_runtime_dependencies()
    for row in statuses:
        label = "OK" if row.ok else "MISSING"
        active.warning(
            "%s %s=%s %s",
            prefix,
            row.pip_name,
            row.version,
            label,
        )
    missing = missing_required_dependencies(statuses)
    if missing:
        active.error("%s %s", prefix, format_missing_dependency_message(missing))
    return statuses


def ensure_runtime_dependencies_or_raise() -> list[DependencyStatus]:
    """Log dependency status and raise RuntimeError when required deps are missing."""
    statuses = log_runtime_dependencies()
    missing = missing_required_dependencies(statuses)
    if missing:
        raise RuntimeError(format_missing_dependency_message(missing))
    return statuses
