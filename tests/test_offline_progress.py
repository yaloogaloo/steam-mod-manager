"""Tests: offline archive progress percent + extended payload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services import archive as archive_mod
from services.archive import OfflinePageArchiver, SteamArchiveLimiter
from services.file_ops import ModFileManager
from services.sync import (
    OFFLINE_ARCHIVE_MOD_WORKERS,
    ModSyncService,
    SyncOptions,
    emit_progress,
)
from ui.sync_thread import (
    _phase_to_percent,
    format_offline_progress_message,
)


LIVE_HTML = """<!DOCTYPE html>
<html><body>
<div id="smm-offline-banner">Offline archive</div>
<div class="workshopItemTitle">Real</div>
</body></html>
"""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)


def _mod_tree(root: Path, *, pub_id: str, title: str, index_html: str | None) -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
            }
        ),
        encoding="utf-8",
    )
    if index_html is not None:
        (info / "index.html").write_text(index_html, encoding="utf-8")
    (folder / "dummy.pak").write_bytes(b"x")
    return folder


def test_offline_in_progress_percent_not_zero() -> None:
    """1 mod downloading: must not stay at 0%."""
    assert _phase_to_percent("offline", 0, 1, in_progress=True) >= 1
    assert _phase_to_percent("offline", 0, 10, in_progress=True) >= 1


def test_offline_completed_mod_progress_retained() -> None:
    """Completed count still maps upward within the offline span."""
    p0 = _phase_to_percent("offline", 0, 10, in_progress=True)
    p1 = _phase_to_percent("offline", 1, 10, in_progress=False)
    p5 = _phase_to_percent("offline", 5, 10, in_progress=False)
    assert p1 > p0 or p1 >= 9
    assert p5 > p1
    assert p5 < 100


def test_done_phase_is_100() -> None:
    assert _phase_to_percent("done", 10, 10) == 100


def test_format_shows_current_mod_and_detail() -> None:
    text = format_offline_progress_message(
        phase="offline",
        current=0,
        total=10,
        message="Steam 离线网页同步",
        current_mod_name="[Palworld] 4x Storage",
        phase_detail="正在下载页面资源…",
        queued_count=7,
        running_count=3,
        completed_count=0,
    )
    assert "Steam 离线网页同步" in text
    assert "[Palworld] 4x Storage" in text
    assert "正在下载页面资源…" in text
    assert "正在处理:" in text
    assert "3/10" in text
    assert "等待:" in text
    assert "7" in text.split("等待:")[1]
    assert "已完成:" in text
    assert "当前Mod:" in text


def test_emit_progress_falls_back_to_legacy_4arg() -> None:
    events: list[tuple[Any, ...]] = []

    def legacy(phase: str, current: int, total: int, message: str) -> None:
        events.append((phase, current, total, message))

    emit_progress(
        legacy,
        "offline",
        0,
        1,
        "msg",
        current_mod_name="X",
        phase_detail="Y",
        in_progress=True,
    )
    assert events == [("offline", 0, 1, "msg")]


def test_one_mod_progress_leaves_zero_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mod_tree(tmp_path, pub_id="1", title="4x Storage", index_html=None)
    percents: list[int] = []
    details: list[str] = []

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        *,
        current_mod_name: str = "",
        phase_detail: str = "",
        in_progress: bool = False,
        **_: object,
    ) -> None:
        percents.append(
            _phase_to_percent(phase, current, total, in_progress=in_progress)
        )
        details.append(phase_detail)
        if current_mod_name:
            details.append(current_mod_name)

    archiver = MagicMock(spec=OfflinePageArchiver)

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
    ) -> Path:
        if on_status:
            on_status("start")
        path = Path(info_dir) / "index.html"
        path.write_text(LIVE_HTML, encoding="utf-8")
        if on_status:
            on_status("ok")
        return path

    archiver.ensure_offline_page.side_effect = fake_ensure
    svc = ModSyncService(
        tmp_path,
        tmp_path,
        client=MagicMock(),
        file_manager=ModFileManager(tmp_path),
        archiver=archiver,
    )
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    result = svc.sync_offline_pages_only(
        options=SyncOptions(),
        on_progress=on_progress,
    )

    assert any(p >= 1 for p in percents[:-1] or percents)
    assert any("正在下载页面资源" in d for d in details)
    assert any("4x Storage" in d for d in details)
    assert percents[-1] == 100  # done phase
    assert len(result.success) == 1


def test_ten_mods_first_download_has_ui_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(10):
        _mod_tree(tmp_path, pub_id=str(100 + i), title=f"Mod {i}", index_html=None)

    seen_download = False
    first_download_percent: int | None = None
    names: list[str] = []

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        *,
        current_mod_name: str = "",
        phase_detail: str = "",
        in_progress: bool = False,
        **_: object,
    ) -> None:
        nonlocal seen_download, first_download_percent
        if "正在下载页面资源" in phase_detail and not seen_download:
            seen_download = True
            first_download_percent = _phase_to_percent(
                phase, current, total, in_progress=in_progress
            )
            if current_mod_name:
                names.append(current_mod_name)

    hits = {"n": 0}

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
    ) -> Path:
        hits["n"] += 1
        if on_status:
            on_status("start")
        path = Path(info_dir) / "index.html"
        path.write_text(LIVE_HTML, encoding="utf-8")
        if on_status:
            on_status("ok")
        return path

    archiver = MagicMock(spec=OfflinePageArchiver)
    archiver.ensure_offline_page.side_effect = fake_ensure
    svc = ModSyncService(
        tmp_path,
        tmp_path,
        client=MagicMock(),
        file_manager=ModFileManager(tmp_path),
        archiver=archiver,
    )
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    result = svc.sync_offline_pages_only(on_progress=on_progress)

    assert seen_download
    assert first_download_percent is not None and first_download_percent >= 1
    assert names and "Palworld" in names[0]
    assert hits["n"] == 10
    assert len(result.success) == 10


def test_offline_queue_runs_mods_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mod-level pool must overlap multiple ensure_offline_page calls."""
    import threading
    import time

    n_mods = max(6, OFFLINE_ARCHIVE_MOD_WORKERS * 2)
    for i in range(n_mods):
        _mod_tree(tmp_path, pub_id=str(200 + i), title=f"CMod {i}", index_html=None)

    lock = threading.Lock()
    current = 0
    peak = 0
    release = threading.Event()

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
    ) -> Path:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        if on_status:
            on_status("start")
        # Hold until enough overlap is observed (or timeout).
        release.wait(timeout=2.0)
        path = Path(info_dir) / "index.html"
        path.write_text(LIVE_HTML, encoding="utf-8")
        if on_status:
            on_status("ok")
        with lock:
            current -= 1
        return path

    archiver = MagicMock(spec=OfflinePageArchiver)
    archiver.ensure_offline_page.side_effect = fake_ensure
    svc = ModSyncService(
        tmp_path,
        tmp_path,
        client=MagicMock(),
        file_manager=ModFileManager(tmp_path),
        archiver=archiver,
    )
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    running_seen: list[int] = []

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        *,
        running_count: int = 0,
        queued_count: int = 0,
        completed_count: int = 0,
        **_: object,
    ) -> None:
        if phase == "offline":
            running_seen.append(running_count)
            if running_count >= OFFLINE_ARCHIVE_MOD_WORKERS:
                release.set()

    t0 = time.perf_counter()
    result = svc.sync_offline_pages_only(on_progress=on_progress)
    elapsed = time.perf_counter() - t0
    release.set()

    assert peak >= 2
    assert max(running_seen or [0]) >= 2
    assert len(result.success) == n_mods
    # Concurrent pool should finish faster than strict serial holds.
    assert elapsed < 8.0
