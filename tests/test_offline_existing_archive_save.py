"""Regression: Save Offline Page with existing archive must refresh, not reuse stale errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_STEAM
from services import archive as archive_mod
from services.archive import (
    ARCHIVE_OUTCOME_FAILED,
    ARCHIVE_OUTCOME_SUCCESS,
    ArchiveEnsureResult,
    OfflinePageArchiver,
    SteamArchiveLimiter,
    write_archive_status,
)
from services.file_ops import INFO_DIR_NAME
from services.identity_service import identity_create_scope
from services.offline.base import OFFLINE_OUTCOME_FAILED, OFFLINE_OUTCOME_SUCCESS
from services.offline.steam import (
    SteamOfflineProvider,
    resolve_steam_workshop_id_for_archive,
)

VALID_HTML = """<!DOCTYPE html>
<html><head><title>Cool Mod</title></head><body>
<div id="smm-offline-banner">Offline archive · Workshop ID 3596053192</div>
<div class="workshopItemTitle">Cool Mod</div>
</body></html>
"""

ERROR_HTML = """<!DOCTYPE html>
<html><head><title>Steam 创意工坊 :: 错误</title></head><body>
<p>error page body</p>
</body></html>
"""


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
    manager = DatabaseManager.instance(tmp_path / "offline_existing.db")
    manager.upsert_game(GameInfo(app_id=262060, name="DarkestDungeon"))
    yield manager
    DatabaseManager.reset_instance()


def _seed_rebuilt_steam_mod(
    db: DatabaseManager,
    *,
    folder: Path,
    mod_id: int = 7,
    workspace_id: str = "3596053192",
    offline_status: str = "archived",
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / INFO_DIR_NAME).mkdir(parents=True, exist_ok=True)
    with identity_create_scope():
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, workspace_id, external_id, internal_id,
                last_known_path, folder_present, offline_status, platform,
                source_url, updated_at, enabled
            ) VALUES (?, 262060, 'Cool Mod', ?, ?, ?, ?, 1, ?, 'steam',
                      ?, datetime('now'), 1)
            """,
            (
                mod_id,
                workspace_id,
                str(mod_id),  # polluted external_id == Internal PK (post-rebuild)
                f"uuid-{mod_id}",
                str(folder),
                offline_status,
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={workspace_id}",
            ),
        )
        db._conn.commit()


def test_resolve_workshop_id_prefers_workspace_over_polluted_pk(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "mod" / "G" / "M"
    _seed_rebuilt_steam_mod(db, folder=folder, mod_id=7, workspace_id="3596053192")
    meta = ModMetadata(published_file_id="7", title="Cool Mod")  # PK stand-in
    wid = resolve_steam_workshop_id_for_archive(
        7, metadata=meta, db=db, managed_path=folder
    )
    assert wid == "3596053192"


def test_existing_index_force_refresh_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case B: existing valid archive + Save Offline → refresh success (same pipeline)."""
    lib = tmp_path / "mod"
    folder = lib / "G" / "M"
    _seed_rebuilt_steam_mod(db, folder=folder, offline_status="archived")
    index = folder / INFO_DIR_NAME / "index.html"
    index.write_text(VALID_HTML, encoding="utf-8")

    seen: dict[str, Any] = {}

    def tracking(self, info_dir, published_file_id, **kwargs):
        seen["workshop_id"] = str(published_file_id)
        seen["force"] = kwargs.get("force_refresh")
        # Simulate successful refresh write.
        Path(info_dir, "index.html").write_text(VALID_HTML, encoding="utf-8")
        return ArchiveEnsureResult(
            path=Path(info_dir) / "index.html",
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking)

    meta = ModMetadata(published_file_id="7", title="Cool Mod")
    result = SteamOfflineProvider().update_offline_page(
        "7",
        managed_path=folder,
        library_root=lib,
        metadata=meta,
        force_refresh=True,
    )
    assert seen["workshop_id"] == "3596053192"
    assert seen["force"] is True
    assert result.outcome == OFFLINE_OUTCOME_SUCCESS
    assert result.http_performed is True
    assert result.status == "archived"


def test_existing_archive_with_previous_failed_status_saves(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale DB failed / archive_status.json must not block a fresh save."""
    lib = tmp_path / "mod"
    folder = lib / "G" / "M"
    _seed_rebuilt_steam_mod(db, folder=folder, offline_status="failed")
    info = folder / INFO_DIR_NAME
    (info / "index.html").write_text(VALID_HTML, encoding="utf-8")
    write_archive_status(
        info,
        reason="steam_error_page",
        published_file_id="7",
        detail="Steam 返回错误页面，该 Workshop 项目可能无法匿名访问...",
    )

    def ok(self, info_dir, published_file_id, **kwargs):
        assert str(published_file_id) == "3596053192"
        assert kwargs.get("force_refresh") is True
        Path(info_dir, "index.html").write_text(VALID_HTML, encoding="utf-8")
        return ArchiveEnsureResult(
            path=Path(info_dir) / "index.html",
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", ok)

    result = SteamOfflineProvider().update_offline_page(
        "7",
        managed_path=folder,
        library_root=lib,
        metadata=ModMetadata(published_file_id="7"),
        force_refresh=True,
    )
    assert result.outcome == OFFLINE_OUTCOME_SUCCESS
    assert result.error == ""
    row = db.get_mod_backup_row("7") or {}
    assert str(row.get("offline_status")) == "archived"


def test_steam_unavailable_returns_current_failure_not_historical(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "G" / "M"
    _seed_rebuilt_steam_mod(db, folder=folder, offline_status="archived")
    info = folder / INFO_DIR_NAME
    before = VALID_HTML
    (info / "index.html").write_text(before, encoding="utf-8")
    write_archive_status(
        info,
        reason="steam_error_page",
        published_file_id="7",
        detail="historical Steam 返回错误页面",
    )

    def boom(self, info_dir, published_file_id, **kwargs):
        return ArchiveEnsureResult(
            path=Path(info_dir) / "index.html",
            outcome=ARCHIVE_OUTCOME_FAILED,
            http_performed=True,
            write_performed=False,
            error="network down NOW",
        )

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", boom)

    result = SteamOfflineProvider().update_offline_page(
        "7",
        managed_path=folder,
        library_root=lib,
        metadata=ModMetadata(published_file_id="7"),
        force_refresh=True,
    )
    assert result.outcome == OFFLINE_OUTCOME_FAILED
    assert result.error == "network down NOW"
    assert "历史" not in result.error
    assert "无法匿名访问" not in result.error
    assert (info / "index.html").read_text(encoding="utf-8") == before


def test_new_and_existing_use_same_ensure_pipeline(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    calls: list[dict[str, Any]] = []

    def tracking(self, info_dir, published_file_id, **kwargs):
        calls.append(
            {
                "workshop_id": str(published_file_id),
                "force": bool(kwargs.get("force_refresh")),
                "info_dir": str(info_dir),
            }
        )
        Path(info_dir).mkdir(parents=True, exist_ok=True)
        Path(info_dir, "index.html").write_text(VALID_HTML, encoding="utf-8")
        return ArchiveEnsureResult(
            path=Path(info_dir) / "index.html",
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking)

    # New archive (no index yet)
    folder_a = lib / "G" / "A"
    _seed_rebuilt_steam_mod(db, folder=folder_a, mod_id=11, workspace_id="111")
    SteamOfflineProvider().update_offline_page(
        "11",
        managed_path=folder_a,
        library_root=lib,
        metadata=ModMetadata(published_file_id="11"),
        force_refresh=True,
    )

    # Existing archive
    folder_b = lib / "G" / "B"
    _seed_rebuilt_steam_mod(db, folder=folder_b, mod_id=12, workspace_id="222")
    (folder_b / INFO_DIR_NAME / "index.html").write_text(VALID_HTML, encoding="utf-8")
    SteamOfflineProvider().update_offline_page(
        "12",
        managed_path=folder_b,
        library_root=lib,
        metadata=ModMetadata(published_file_id="12"),
        force_refresh=True,
    )

    assert len(calls) == 2
    assert calls[0]["force"] is True and calls[1]["force"] is True
    assert calls[0]["workshop_id"] == "111"
    assert calls[1]["workshop_id"] == "222"
