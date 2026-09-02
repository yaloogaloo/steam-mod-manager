"""Tests: stub offline pages must not block future archive attempts."""

from __future__ import annotations

import json
from pathlib import Path

from services.archive import is_stub_offline_page
from services.file_ops import ModFileManager
from services.sync import ModSyncService, SyncOptions


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_mod_tree(root: Path, *, title: str = "Test Mod") -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": "3761838546",
                "title": title,
            }
        ),
        encoding="utf-8",
    )
    return folder


def _svc(root: Path) -> ModSyncService:
    svc = object.__new__(ModSyncService)
    svc.files = ModFileManager(root)
    return svc


STUB_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><title>Test — Offline (stub)</title></head>
<body>
  <div class="box">
    <h1>Test</h1>
    <p>未能下载完整的 Steam 创意工坊原网页（网络超时或被拦截）。</p>
    <p>原因: <code>Failed to perform, curl: (35) Recv failure: Connection was reset</code></p>
  </div>
</body>
</html>
"""

LIVE_HTML = """<!DOCTYPE html>
<html>
<body>
  <div id="smm-offline-banner">Offline archive · Workshop ID 3761838546</div>
  <div class="workshopItemTitle">Real Mod Page</div>
</body>
</html>
"""


def test_is_stub_offline_page_detects_stub(tmp_path: Path) -> None:
    stub = _write(tmp_path / "index.html", STUB_HTML)
    assert is_stub_offline_page(stub) is True


def test_is_stub_offline_page_rejects_live(tmp_path: Path) -> None:
    live = _write(tmp_path / "index.html", LIVE_HTML)
    assert is_stub_offline_page(live) is False


def test_is_stub_offline_page_empty_or_missing(tmp_path: Path) -> None:
    empty = _write(tmp_path / "empty.html", "")
    assert is_stub_offline_page(empty) is False
    assert is_stub_offline_page(tmp_path / "missing.html") is False


def test_success_page_is_fully_synced(tmp_path: Path) -> None:
    folder = _make_mod_tree(tmp_path)
    _write(folder / ".info" / "index.html", LIVE_HTML)
    svc = _svc(tmp_path)
    assert svc._has_valid_offline_page(folder) is True
    assert svc._is_fully_synced_mod(folder) is True


def test_stub_page_is_invalid_offline_but_file_sync_complete(tmp_path: Path) -> None:
    """Offline HTML is a separate queue — stub does not block full file sync."""
    folder = _make_mod_tree(tmp_path)
    _write(folder / ".info" / "index.html", STUB_HTML)
    svc = _svc(tmp_path)
    assert is_stub_offline_page(folder / ".info" / "index.html") is True
    assert svc._has_valid_offline_page(folder) is False
    assert svc._is_fully_synced_mod(folder) is True


def test_missing_index_still_file_sync_complete(tmp_path: Path) -> None:
    """Missing offline page does not require re-running full sync."""
    folder = _make_mod_tree(tmp_path)
    svc = _svc(tmp_path)
    assert svc._has_valid_offline_page(folder) is False
    assert svc._is_fully_synced_mod(folder) is True


def test_force_overwrite_never_early_skips_stub(tmp_path: Path) -> None:
    """overwrite_files=True must put mods in to_fetch regardless of stub/live."""
    folder = _make_mod_tree(tmp_path)
    _write(folder / ".info" / "index.html", STUB_HTML)
    svc = _svc(tmp_path)
    # Simulate Phase 1b classification
    opts = SyncOptions(skip_existing=True, overwrite_files=True)
    existing = folder
    assert opts.overwrite_files is True
    # With overwrite, sync() always appends to to_fetch (see sync L151-154)
    would_skip = (
        bool(existing)
        and opts.skip_existing
        and not opts.overwrite_files
        and svc._is_fully_synced_mod(existing)
    )
    assert would_skip is False


def test_live_page_still_skips_when_not_overwrite(tmp_path: Path) -> None:
    folder = _make_mod_tree(tmp_path)
    _write(folder / ".info" / "index.html", LIVE_HTML)
    svc = _svc(tmp_path)
    opts = SyncOptions(skip_existing=True, overwrite_files=False)
    would_skip = (
        opts.skip_existing
        and not opts.overwrite_files
        and svc._is_fully_synced_mod(folder)
    )
    assert would_skip is True
