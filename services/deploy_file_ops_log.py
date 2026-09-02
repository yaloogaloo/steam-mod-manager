"""Structured deploy file-operation logging (copy / link / verify)."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def log_deploy_file_start(*, source: Path | str, target: Path | str, mode: str) -> None:
    logger.info(
        "[DEPLOY FILE START] source=%s target=%s mode=%s",
        source,
        target,
        mode,
    )


def log_deploy_file_success(
    *, source: Path | str, target: Path | str, bytes_written: int
) -> None:
    logger.info(
        "[DEPLOY FILE SUCCESS] source=%s target=%s bytes=%s",
        source,
        target,
        bytes_written,
    )


def log_deploy_file_failed(
    *,
    source: Path | str,
    target: Path | str,
    error_type: str,
    error: str,
) -> None:
    logger.warning(
        "[DEPLOY FILE FAILED] source=%s target=%s error_type=%s error=%s",
        source,
        target,
        error_type,
        error,
    )