"""Detail Open Offline: UI-thread probe + BackgroundAssetWorker materialize.

Never hashes Asset Store objects or copies CAS closures on the Qt thread.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QMessageBox, QWidget

from services.info_asset_runtime import (
    OFFLINE_ASSET_UNAVAILABLE,
    OfflineOpenResult,
    prepare_offline_open,
    probe_offline_open,
)
from services.perf_stage import log_perf_stage
from ui.background_asset_thread import BackgroundAssetWorker

_UNAVAILABLE_TITLE = "离线页面不可用"
_UNAVAILABLE_TEXT = (
    f"无法打开离线页面（{OFFLINE_ASSET_UNAVAILABLE}）。\n"
    "资源必须来自 Asset Store + manifest，经 cache/offline_view 打开。\n"
    "可尝试修复后重开。"
)


def start_detail_offline_open(
    owner: QWidget,
    *,
    mod_id: str,
    managed_path: Path | str | None,
    open_url: Callable[[Path], None],
    set_status: Callable[[str], None] | None = None,
) -> None:
    """Probe on the UI thread; materialize via BackgroundAssetWorker on miss."""
    t0 = time.perf_counter()
    mid = str(mod_id or "").strip()
    path = Path(managed_path) if managed_path is not None else None

    probe_t0 = time.perf_counter()
    probe = probe_offline_open(path, mod_id=mid)
    log_perf_stage(
        "detail_open_probe_ms",
        (time.perf_counter() - probe_t0) * 1000.0,
        cache_hit=bool(probe.cache_hit),
        can_materialize=bool(probe.can_materialize),
    )

    if probe.cache_hit is not None:
        open_url(Path(probe.cache_hit))
        log_perf_stage(
            "detail_open_total_ms",
            (time.perf_counter() - t0) * 1000.0,
            path="cache_hit",
        )
        return

    if not probe.can_materialize:
        log_perf_stage(
            "detail_open_total_ms",
            (time.perf_counter() - t0) * 1000.0,
            path="unavailable",
        )
        _offer_unavailable(owner, mod_id=mid, managed_path=path, open_url=open_url)
        return

    if set_status is not None:
        set_status("正在准备离线页面…")

    token = int(getattr(owner, "_offline_open_token", 0) or 0) + 1
    setattr(owner, "_offline_open_token", token)
    setattr(owner, "_offline_open_t0", t0)

    prev = getattr(owner, "_offline_open_worker", None)
    if prev is not None:
        try:
            prev.request_cancel()
        except Exception:  # noqa: BLE001
            pass

    def _runner(task) -> OfflineOpenResult:
        del task
        mat_t0 = time.perf_counter()
        result = prepare_offline_open(path, mod_id=mid)
        log_perf_stage(
            "detail_open_materialize_ms",
            (time.perf_counter() - mat_t0) * 1000.0,
            ok=bool(result.ok),
            source=result.source or "",
        )
        return result

    worker = BackgroundAssetWorker(
        _runner,
        task_name="detail_open_offline",
        parent=owner,
    )

    def _still_current() -> bool:
        return int(getattr(owner, "_offline_open_token", 0) or 0) == token

    def _on_ok(result: object) -> None:
        if not _still_current():
            return
        opened = result if isinstance(result, OfflineOpenResult) else None
        log_perf_stage(
            "detail_open_total_ms",
            (time.perf_counter() - float(getattr(owner, "_offline_open_t0", t0)))
            * 1000.0,
            path="worker",
            ok=bool(opened and opened.ok),
        )
        if opened is not None and opened.ok and opened.path is not None:
            if set_status is not None:
                set_status("")
            open_url(Path(opened.path))
            return
        _offer_unavailable(
            owner, mod_id=mid, managed_path=path, open_url=open_url
        )

    def _on_fail(message: str) -> None:
        if not _still_current():
            return
        log_perf_stage(
            "detail_open_total_ms",
            (time.perf_counter() - float(getattr(owner, "_offline_open_t0", t0)))
            * 1000.0,
            path="worker_fail",
        )
        _offer_unavailable(
            owner,
            mod_id=mid,
            managed_path=path,
            open_url=open_url,
            detail=str(message or ""),
        )

    worker.finished_ok.connect(_on_ok)
    worker.failed.connect(_on_fail)
    worker.cancelled.connect(lambda: None)
    setattr(owner, "_offline_open_worker", worker)
    worker.start()


def _offer_unavailable(
    owner: QWidget,
    *,
    mod_id: str,
    managed_path: Path | None,
    open_url: Callable[[Path], None],
    detail: str = "",
) -> None:
    text = _UNAVAILABLE_TEXT
    if detail:
        text = f"{text}\n\n{detail}"
    box = QMessageBox(owner)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(_UNAVAILABLE_TITLE)
    box.setText(text)
    repair_btn = box.addButton("修复", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    setattr(owner, "_offline_unavail_box", box)

    def _finished(_code: int) -> None:
        if box.clickedButton() is repair_btn:
            _start_repair_then_open(
                owner,
                mod_id=mod_id,
                managed_path=managed_path,
                open_url=open_url,
            )

    box.finished.connect(_finished)
    box.open()


def _start_repair_then_open(
    owner: QWidget,
    *,
    mod_id: str,
    managed_path: Path | None,
    open_url: Callable[[Path], None],
) -> None:
    t0 = time.perf_counter()
    token = int(getattr(owner, "_offline_open_token", 0) or 0) + 1
    setattr(owner, "_offline_open_token", token)
    setattr(owner, "_offline_open_t0", t0)

    def _runner(task) -> OfflineOpenResult:
        del task
        mat_t0 = time.perf_counter()
        result = prepare_offline_open(
            managed_path, mod_id=mod_id, repair_first=True
        )
        log_perf_stage(
            "detail_open_materialize_ms",
            (time.perf_counter() - mat_t0) * 1000.0,
            ok=bool(result.ok),
            source="repair_then_open",
        )
        return result

    worker = BackgroundAssetWorker(
        _runner, task_name="detail_repair_open_offline", parent=owner
    )

    def _still_current() -> bool:
        return int(getattr(owner, "_offline_open_token", 0) or 0) == token

    def _on_ok(result: object) -> None:
        if not _still_current():
            return
        opened = result if isinstance(result, OfflineOpenResult) else None
        log_perf_stage(
            "detail_open_total_ms",
            (time.perf_counter() - t0) * 1000.0,
            path="repair",
            ok=bool(opened and opened.ok),
        )
        if opened is not None and opened.ok and opened.path is not None:
            open_url(Path(opened.path))
            return
        box = QMessageBox(owner)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(_UNAVAILABLE_TITLE)
        box.setText(f"修复后仍无法打开（{OFFLINE_ASSET_UNAVAILABLE}）。")
        box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        box.open()

    worker.finished_ok.connect(_on_ok)
    worker.failed.connect(lambda _m: _on_ok(None) if _still_current() else None)
    setattr(owner, "_offline_open_worker", worker)
    worker.start()
