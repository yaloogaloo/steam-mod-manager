"""Force-refresh / outcome contract for Steam offline archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services import archive as archive_mod
from services.archive import (
    ARCHIVE_OUTCOME_FAILED,
    ARCHIVE_OUTCOME_SKIPPED,
    ARCHIVE_OUTCOME_SUCCESS,
    ArchiveEnsureResult,
    OfflinePageArchiver,
    SteamArchiveLimiter,
    classify_steam_workshop_html,
    is_valid_steam_workshop_page,
)
from services.offline.base import (
    OFFLINE_OUTCOME_FAILED,
    OFFLINE_OUTCOME_SKIPPED,
    OFFLINE_OUTCOME_SUCCESS,
    OfflineUpdateResult,
)
from services.offline.steam import SteamOfflineProvider
from ui.offline_archive_thread import OfflineArchiveWorker

VALID_HTML = """<!DOCTYPE html>
<html><head><title>Cool Mod</title></head><body>
<div id="smm-offline-banner">Offline archive · Workshop ID 3596053192</div>
<div class="workshopItemTitle">Cool Mod</div>
</body></html>
"""

ERROR_HTML = """<!DOCTYPE html>
<html><head><title>Steam 创意工坊 :: 错误</title></head><body>
<div id="smm-offline-banner">banner</div>
<p>error page body</p>
</body></html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>Sign In</title></head><body>
<a href="https://login.steampowered.com/">login</a>
<input type="password" name="password"/>
</body></html>
"""


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _fast_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "force_refresh.db")
    yield manager
    DatabaseManager.reset_instance()


def _ok_resp(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = text
    resp.charset_encoding = "utf-8"
    if status >= 400:
        err = Exception(f"HTTP {status}")
        err.response = resp  # type: ignore[attr-defined]
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status = MagicMock()
    return resp


def test_classify_error_and_login_html() -> None:
    assert classify_steam_workshop_html(ERROR_HTML) == "error_page"
    assert classify_steam_workshop_html(LOGIN_HTML) == "login_page"
    assert classify_steam_workshop_html(VALID_HTML) == "ok"


def test_error_html_is_not_valid_cache(tmp_path: Path) -> None:
    p = tmp_path / "index.html"
    p.write_text(ERROR_HTML, encoding="utf-8")
    assert is_valid_steam_workshop_page(p) is False


def test_case1_no_old_file_force_false_http_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    fetch = {"n": 0}
    write = {"n": 0}
    real_write = OfflinePageArchiver._write_atomic

    def counting_fetch(self: OfflinePageArchiver, url: str) -> str:
        fetch["n"] += 1
        return VALID_HTML

    def counting_write(path: Path, content: str) -> None:
        write["n"] += 1
        real_write(path, content)

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", counting_fetch)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_rewrite_and_download_assets",
        lambda *a, **k: {"ok": 0, "fail": 0, "unique": 0},
    )
    monkeypatch.setattr(OfflinePageArchiver, "_write_atomic", staticmethod(counting_write))

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "3596053192", force_refresh=False)

    assert fetch["n"] == 1
    assert write["n"] >= 1
    assert result.outcome == ARCHIVE_OUTCOME_SUCCESS
    assert result.http_performed is True
    assert result.write_performed is True


def test_case2_valid_cache_force_false_skipped_no_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    index = info / "index.html"
    index.write_text(VALID_HTML, encoding="utf-8")
    fetch = {"n": 0}

    def boom(self: OfflinePageArchiver, url: str) -> str:
        fetch["n"] += 1
        raise AssertionError("must not fetch")

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", boom)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "3596053192", force_refresh=False)

    assert fetch["n"] == 0
    assert result.outcome == ARCHIVE_OUTCOME_SKIPPED
    assert result.skip_reason == "cache_hit"
    assert result.http_performed is False
    assert result.write_performed is False


def test_case3_valid_cache_force_true_must_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    (info / "index.html").write_text(VALID_HTML, encoding="utf-8")
    fetch = {"n": 0}
    write = {"n": 0}
    real_write = OfflinePageArchiver._write_atomic

    def counting_fetch(self: OfflinePageArchiver, url: str) -> str:
        fetch["n"] += 1
        return VALID_HTML

    def counting_write(path: Path, content: str) -> None:
        write["n"] += 1
        real_write(path, content)

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", counting_fetch)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_rewrite_and_download_assets",
        lambda *a, **k: {"ok": 0, "fail": 0, "unique": 0},
    )
    monkeypatch.setattr(OfflinePageArchiver, "_write_atomic", staticmethod(counting_write))

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "3596053192", force_refresh=True)

    assert fetch["n"] == 1
    assert write["n"] >= 1
    assert result.outcome == ARCHIVE_OUTCOME_SUCCESS
    assert result.http_performed is True
    assert result.write_performed is True


def test_case4_force_true_http_fail_keeps_old_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    index = info / "index.html"
    index.write_text(VALID_HTML, encoding="utf-8")
    before = index.read_text(encoding="utf-8")

    def boom(self: OfflinePageArchiver, url: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", boom)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "3596053192", force_refresh=True)

    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    assert index.read_text(encoding="utf-8") == before
    assert result.write_performed is False


def test_case5_http_404_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    (info / "index.html").write_text(VALID_HTML, encoding="utf-8")

    def not_found(self: OfflinePageArchiver, url: str) -> str:
        raise RuntimeError("HTTP Error 404")

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", not_found)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "1", force_refresh=True)

    assert result.outcome == ARCHIVE_OUTCOME_FAILED


def test_case7_write_failure_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"

    monkeypatch.setattr(
        OfflinePageArchiver, "_fetch_main_html", lambda self, url: VALID_HTML
    )
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_rewrite_and_download_assets",
        lambda *a, **k: {"ok": 0, "fail": 0, "unique": 0},
    )

    def boom_write(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(OfflinePageArchiver, "_write_atomic", staticmethod(boom_write))

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "1", force_refresh=True)

    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    assert result.http_performed is True
    assert result.write_performed is False


def test_case8_error_html_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    monkeypatch.setattr(
        OfflinePageArchiver, "_fetch_main_html", lambda self, url: ERROR_HTML
    )

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "1", force_refresh=True)

    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    assert "错误页面" in (result.error or "")


def test_case9_login_html_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    monkeypatch.setattr(
        OfflinePageArchiver, "_fetch_main_html", lambda self, url: LOGIN_HTML
    )

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "1", force_refresh=True)

    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    assert "登录页面" in (result.error or "")


def test_case10_same_hash_still_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    (info / "index.html").write_text(VALID_HTML, encoding="utf-8")
    fetch = {"n": 0}
    write = {"n": 0}
    real_write = OfflinePageArchiver._write_atomic

    def counting_fetch(self: OfflinePageArchiver, url: str) -> str:
        fetch["n"] += 1
        return VALID_HTML

    def counting_write(path: Path, content: str) -> None:
        write["n"] += 1
        real_write(path, content)

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", counting_fetch)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_rewrite_and_download_assets",
        lambda *a, **k: {"ok": 0, "fail": 0, "unique": 0},
    )
    monkeypatch.setattr(OfflinePageArchiver, "_write_atomic", staticmethod(counting_write))

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        result = archiver.ensure_offline_page(info, "3596053192", force_refresh=True)

    assert fetch["n"] == 1
    assert write["n"] >= 1
    assert result.outcome == ARCHIVE_OUTCOME_SUCCESS


def test_case12_provider_force_refresh_passed(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "Game" / "Mod"
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "index.html").write_text(VALID_HTML, encoding="utf-8")
    db.upsert_mod(
        ModMetadata(
            published_file_id="3596053192",
            title="Mod",
            managed_path=str(folder),
        )
    )
    seen: dict[str, Any] = {}

    def tracking(self, info_dir, published_file_id, **kwargs):
        seen["force"] = kwargs.get("force_refresh")
        return ArchiveEnsureResult(
            path=Path(info_dir) / "index.html",
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking)

    result = SteamOfflineProvider().update_offline_page(
        "3596053192",
        managed_path=folder,
        library_root=lib,
        force_refresh=True,
    )
    assert seen["force"] is True
    assert result.outcome == OFFLINE_OUTCOME_SUCCESS
    assert result.http_performed is True


def test_case13_worker_failed_with_old_file_emits_failed(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "mod"
    info = folder / ".info"
    info.mkdir(parents=True)
    index = info / "index.html"
    index.write_text(VALID_HTML, encoding="utf-8")

    class FakeManager:
        def __init__(self, *a, **k):
            pass

        def update_mod_offline(self, mod_id, **kwargs):
            assert kwargs.get("force_refresh") is True
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=index,
                status="failed",
                provider="steam_archive",
                error="network down",
                outcome=OFFLINE_OUTCOME_FAILED,
                force_refresh=True,
                http_performed=True,
                write_performed=False,
            )

    monkeypatch.setattr("ui.offline_archive_thread.OfflineManager", FakeManager)

    finished: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    worker = OfflineArchiveWorker(
        folder,
        published_file_id="3596053192",
        library_root=tmp_path,
        force_refresh=True,
    )
    worker.archive_finished.connect(finished.append)
    worker.archive_failed.connect(failed.append)
    worker.archive_skipped.connect(skipped.append)
    worker.start()
    worker.wait(5000)
    qapp.processEvents()

    assert failed == ["network down"]
    assert finished == []
    assert skipped == []


def test_case14_worker_skipped_emits_skipped_not_finished(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "mod"
    info = folder / ".info"
    info.mkdir(parents=True)
    index = info / "index.html"
    index.write_text(VALID_HTML, encoding="utf-8")

    class FakeManager:
        def __init__(self, *a, **k):
            pass

        def update_mod_offline(self, mod_id, **kwargs):
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=index,
                status="archived",
                provider="steam_archive",
                outcome=OFFLINE_OUTCOME_SKIPPED,
                skip_reason="cache_hit",
                force_refresh=False,
            )

    monkeypatch.setattr("ui.offline_archive_thread.OfflineManager", FakeManager)

    finished: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    worker = OfflineArchiveWorker(
        folder,
        published_file_id="3596053192",
        library_root=tmp_path,
        force_refresh=False,
    )
    worker.archive_finished.connect(finished.append)
    worker.archive_failed.connect(failed.append)
    worker.archive_skipped.connect(skipped.append)
    worker.start()
    worker.wait(5000)
    qapp.processEvents()

    assert skipped == ["cache_hit"]
    assert finished == []
    assert failed == []
