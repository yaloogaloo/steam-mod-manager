"""Tests: offline-pages-only sync, cache reuse, no 429 storm."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.models import ModMetadata
from services import archive as archive_mod
from services.archive import (
    OfflinePageArchiver,
    RATE_LIMIT_USER_MESSAGE,
    SteamArchiveLimiter,
    is_stub_offline_page,
    write_archive_status,
    write_last_archive_attempt,
)
from services.file_ops import ModFileManager
from services.sync import OFFLINE_ARCHIVE_MOD_WORKERS, ModSyncService, SyncOptions


STUB_HTML = """<!DOCTYPE html>
<html><head><title>X — Offline (stub)</title></head>
<body><p>未能下载完整的 Steam 创意工坊原网页</p></body></html>
"""

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
        json.dumps({"published_file_id": pub_id, "title": title}),
        encoding="utf-8",
    )
    if index_html is not None:
        (info / "index.html").write_text(index_html, encoding="utf-8")
    (folder / "dummy.pak").write_bytes(b"x")
    return folder


def test_offline_sync_does_not_call_metadata_or_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mod_tree(tmp_path, pub_id="111", title="Mod A", index_html=None)

    client = MagicMock()
    files = ModFileManager(tmp_path)
    archiver = MagicMock(spec=OfflinePageArchiver)
    archiver.ensure_offline_page.side_effect = lambda info, pub, **k: (
        Path(info) / "index.html"
    ).write_text(LIVE_HTML, encoding="utf-8") or (Path(info) / "index.html")

    svc = ModSyncService(
        tmp_path,
        tmp_path,
        client=client,
        file_manager=files,
        archiver=archiver,
    )
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    copy_called = {"n": 0}

    def boom_copy(*_a: Any, **_k: Any) -> None:
        copy_called["n"] += 1
        raise AssertionError("copy_mod must not run")

    monkeypatch.setattr(files, "copy_mod", boom_copy)

    result = svc.sync_offline_pages_only(
        options=SyncOptions(proxy_url=""),
    )

    client.get_details_batch.assert_not_called()
    client.resolve_game_names.assert_not_called()
    assert copy_called["n"] == 0
    assert archiver.ensure_offline_page.called
    assert len(result.success) + len(result.failed) + len(result.skipped) >= 1


def test_offline_sync_skips_valid_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mod_tree(tmp_path, pub_id="222", title="Good Mod", index_html=LIVE_HTML)

    archiver = MagicMock(spec=OfflinePageArchiver)
    svc = ModSyncService(
        tmp_path,
        tmp_path,
        client=MagicMock(),
        file_manager=ModFileManager(tmp_path),
        archiver=archiver,
    )
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    result = svc.sync_offline_pages_only()
    assert len(result.skipped) == 1
    archiver.ensure_offline_page.assert_not_called()
    archiver.archive.assert_not_called()


def test_one_mod_cache_reuse_after_first_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 mod × 5 syncs: first archives, subsequent reads cache (skip)."""
    _mod_tree(tmp_path, pub_id="555", title="Cache Mod", index_html=None)
    calls = {"n": 0}

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
        force_refresh: bool = False,
        **_kwargs: Any,
    ) -> Path:
        calls["n"] += 1
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

    results = [svc.sync_offline_pages_only() for _ in range(5)]
    assert calls["n"] == 1
    assert len(results[0].success) == 1
    for later in results[1:]:
        assert len(later.skipped) == 1
        assert len(later.success) == 0


def test_ten_mods_no_429_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global fuse armed → remaining mods do not hit Steam."""
    for i in range(10):
        _mod_tree(tmp_path, pub_id=str(1000 + i), title=f"Mod {i}", index_html=None)

    network_hits = {"n": 0}

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
        force_refresh: bool = False,
        **_kwargs: Any,
    ) -> Path:
        network_hits["n"] += 1
        # Simulate consecutive HTML 429 → global block.
        archive_mod.STEAM_ARCHIVE_LIMITER.block_for_rate_limit(seconds=1800)
        write_archive_status(
            info_dir,
            reason="rate_limited",
            published_file_id=published_file_id,
            detail="429",
        )
        path = Path(info_dir) / "index.html"
        path.write_text(STUB_HTML, encoding="utf-8")
        if on_status:
            on_status("rate_limited")
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

    result = svc.sync_offline_pages_only()
    # Up to OFFLINE_ARCHIVE_MOD_WORKERS may already be in-flight before the
    # first 429 sets the global block; remaining mods must not hit Steam.
    assert 1 <= network_hits["n"] <= OFFLINE_ARCHIVE_MOD_WORKERS
    assert len(result.rate_limited) == 10
    assert RATE_LIMIT_USER_MESSAGE in result.rate_limited[0][1]


def test_single_mod_429_without_global_fuse_does_not_freeze_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Mod HTML 429 stub must not mark the rest of the batch rate_limited."""
    for i in range(5):
        _mod_tree(tmp_path, pub_id=str(2000 + i), title=f"Soft {i}", index_html=None)

    hits: list[str] = []

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
        force_refresh: bool = False,
        **_kwargs: Any,
    ) -> Path:
        hits.append(str(published_file_id))
        path = Path(info_dir) / "index.html"
        if str(published_file_id).endswith("0"):
            # Transient failure recorded as rate_limited status WITHOUT global fuse.
            write_archive_status(
                info_dir,
                reason="rate_limited",
                published_file_id=published_file_id,
                detail="soft 429",
            )
            path.write_text(STUB_HTML, encoding="utf-8")
            if on_status:
                on_status("fail")
            return path
        if on_status:
            on_status("start")
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

    result = svc.sync_offline_pages_only()
    assert len(hits) == 5
    assert len(result.rate_limited) == 0
    assert len(result.failed) == 1
    assert len(result.success) == 4
    assert not archive_mod.STEAM_ARCHIVE_LIMITER.is_blocked()


def test_offline_sync_retries_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _mod_tree(tmp_path, pub_id="333", title="Stub Mod", index_html=STUB_HTML)
    assert is_stub_offline_page(folder / ".info" / "index.html")

    write_last_archive_attempt(folder / ".info", failed=True, when=0)

    calls: list[str] = []

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
        force_refresh: bool = False,
        **_kwargs: Any,
    ) -> Path:
        if on_status:
            on_status("start")
        path = Path(info_dir) / "index.html"
        path.write_text(LIVE_HTML, encoding="utf-8")
        if on_status:
            on_status("ok")
        calls.append(str(published_file_id))
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

    result = svc.sync_offline_pages_only()
    assert calls == ["333"]
    assert len(result.success) == 1


def test_offline_sync_emits_archive_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mod_tree(tmp_path, pub_id="444", title="Pal Analyzer", index_html=None)
    events: list[tuple[str, int, int, str]] = []

    def fake_ensure(
        info_dir: Any,
        published_file_id: Any,
        *,
        metadata: Any = None,
        on_status: Any = None,
        force_refresh: bool = False,
        **_kwargs: Any,
    ) -> Path:
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

    svc.sync_offline_pages_only(
        on_progress=lambda phase, cur, tot, msg: events.append((phase, cur, tot, msg)),
    )

    # Extended progress uses kwargs; legacy 4-arg capture still gets phase/message.
    assert any(e[0] == "offline" for e in events)
    assert events[-1][0] == "done"
    assert "429" in events[-1][3] or "成功" in events[-1][3]


def test_full_sync_default_does_not_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full sync with default options must not call archive / ensure_offline_page."""
    events: list[tuple[str, int, int, str]] = []

    workshop = tmp_path / "ws" / "content" / "1" / "999"
    workshop.mkdir(parents=True)
    (workshop / "file.txt").write_text("x", encoding="utf-8")

    target = tmp_path / "lib"

    from core.scanner import ScannedMod

    class FakeScanner:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def scan(self, recursive: bool = True) -> list[ScannedMod]:
            return [ScannedMod(published_file_id="999", path=workshop)]

    monkeypatch.setattr("services.sync.WorkshopScanner", FakeScanner)

    meta = ModMetadata(
        published_file_id="999",
        title="Prog Mod",
        app_id=1,
        game_name="Game",
        source_path=str(workshop),
    )

    client = MagicMock()
    client.get_details_batch.return_value = [meta]
    client.resolve_game_names.side_effect = lambda metas, on_progress=None: metas

    archiver = MagicMock(spec=OfflinePageArchiver)
    svc = ModSyncService(tmp_path / "ws", target, client=client, archiver=archiver)
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    def fake_copy(meta: ModMetadata, **_k: Any) -> Path:
        folder = target / "Game" / "Prog Mod"
        folder.mkdir(parents=True)
        (folder / ".info").mkdir(exist_ok=True)
        (folder / ".info" / "mod.json").write_text("{}", encoding="utf-8")
        return folder

    monkeypatch.setattr(svc.files, "copy_mod", fake_copy)
    monkeypatch.setattr(svc, "_rename_numeric_if_needed", lambda folder, meta: folder)

    # Default SyncOptions: archive_pages=False
    svc.sync(
        SyncOptions(skip_existing=False),
        on_progress=lambda phase, cur, tot, msg: events.append((phase, cur, tot, msg)),
    )

    archiver.archive.assert_not_called()
    archiver.ensure_offline_page.assert_not_called()
    sync_events = [e for e in events if e[0] == "sync"]
    assert sync_events
    assert "开始同步 Mod 文件、元数据与封面" in sync_events[0][3]


def test_full_sync_emits_sync_phase_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entering phase sync must report current=0 before long archive work."""
    events: list[tuple[str, int, int, str]] = []

    workshop = tmp_path / "ws" / "content" / "1" / "999"
    workshop.mkdir(parents=True)
    (workshop / "file.txt").write_text("x", encoding="utf-8")

    target = tmp_path / "lib"

    from core.scanner import ScannedMod

    class FakeScanner:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def scan(self, recursive: bool = True) -> list[ScannedMod]:
            return [ScannedMod(published_file_id="999", path=workshop)]

    monkeypatch.setattr("services.sync.WorkshopScanner", FakeScanner)

    meta = ModMetadata(
        published_file_id="999",
        title="Prog Mod",
        app_id=1,
        game_name="Game",
        source_path=str(workshop),
    )

    client = MagicMock()
    client.get_details_batch.return_value = [meta]
    client.resolve_game_names.side_effect = lambda metas, on_progress=None: metas

    svc = ModSyncService(tmp_path / "ws", target, client=client)
    monkeypatch.setattr(svc, "_begin_archive_batch", lambda _opts: None)
    monkeypatch.setattr(svc, "_end_archive_batch", lambda: None)

    def fake_copy(meta: ModMetadata, **_k: Any) -> Path:
        folder = target / "Game" / "Prog Mod"
        folder.mkdir(parents=True)
        (folder / ".info").mkdir(exist_ok=True)
        return folder

    monkeypatch.setattr(svc.files, "copy_mod", fake_copy)
    monkeypatch.setattr(svc, "_rename_numeric_if_needed", lambda folder, meta: folder)

    def fake_enrich(
        meta: ModMetadata,
        managed: Path,
        opts: SyncOptions,
        on_status: Any = None,
    ) -> None:
        if on_status:
            on_status("start")
            on_status("ok")
        (managed / ".info" / "index.html").write_text(LIVE_HTML, encoding="utf-8")
        meta.offline_page_path = str(managed / ".info" / "index.html")

    monkeypatch.setattr(svc, "_network_enrich", fake_enrich)

    svc.sync(
        SyncOptions(skip_existing=False, archive_pages=True),
        on_progress=lambda phase, cur, tot, msg: events.append((phase, cur, tot, msg)),
    )

    sync_events = [e for e in events if e[0] == "sync"]
    assert sync_events, events
    assert sync_events[0][1] == 0
    assert "开始同步文件与 Steam 离线网页" in sync_events[0][3]
    assert any("正在下载离线页面: Prog Mod" in e[3] for e in sync_events)
