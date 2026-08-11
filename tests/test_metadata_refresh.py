"""Steam metadata refresh / retry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.steam_api import SteamWorkshopClient
from services.file_ops import COVER_BASENAME, INFO_DIR_NAME, ModFileManager
from services.metadata_refresh import (
    is_unknown_mod_title,
    needs_metadata_refresh,
    refresh_steam_mod_metadata,
    refresh_steam_mods_metadata,
    rename_managed_folder_for_title,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "meta_refresh.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_failed_mod(lib: Path, *, mid: str = "3413520661") -> Path:
    folder = lib / "Game" / f"Unknown_Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": f"Unknown_Mod_{mid}",
                "fetch_error": "GetPublishedFileDetails timeout",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return folder


def test_unknown_title_detection() -> None:
    assert is_unknown_mod_title("Unknown_Mod_3413520661", published_file_id="3413520661")
    assert is_unknown_mod_title("Unknown Mod 3413520661", published_file_id="3413520661")
    assert is_unknown_mod_title("")
    assert is_unknown_mod_title("12345")
    assert not is_unknown_mod_title("Bigger Harbour")


def test_needs_refresh_skips_healthy() -> None:
    healthy = ModMetadata(published_file_id="1", title="Real Title", fetch_error=None)
    assert needs_metadata_refresh(healthy) is False
    failed = ModMetadata(
        published_file_id="1",
        title="Unknown_Mod_1",
        fetch_error="timeout",
    )
    assert needs_metadata_refresh(failed) is True


def test_failed_metadata_retries_successfully(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    folder = _seed_failed_mod(lib)
    mid = "3413520661"

    fresh = ModMetadata(
        published_file_id=mid,
        title="Cool Workshop Mod",
        description="A real description",
        preview_url="https://example.com/cover.jpg",
        time_updated=1700000000,
        app_id=123,
    )

    def fake_refresh(self, ids, **kwargs):
        return [fresh]

    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", fake_refresh)

    cover_calls: list[str] = []

    def fake_cover(self, metadata, dest_dir, *, filename=COVER_BASENAME):
        dest = Path(dest_dir) / f"{filename}.jpg"
        dest.write_bytes(b"\xff\xd8\xff")
        metadata.cover_path = str(dest)
        cover_calls.append(str(dest))
        return dest

    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", fake_cover)

    result = refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=False, download_cover=True
    )
    assert result.success is True
    assert result.skipped is False
    assert result.title == "Cool Workshop Mod"
    assert result.renamed is True
    assert result.managed_path is not None
    assert result.managed_path.name == "Cool Workshop Mod"
    assert result.managed_path.is_dir()

    meta = ModFileManager(lib).load_metadata(result.managed_path)
    assert meta is not None
    assert meta.title == "Cool Workshop Mod"
    assert meta.description == "A real description"
    assert not meta.fetch_error
    disk = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert "fetch_error" not in disk
    assert cover_calls
    assert Path(cover_calls[0]).is_file() or (
        result.managed_path / INFO_DIR_NAME / "cover.jpg"
    ).is_file()


def test_cover_updated_on_refresh(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    folder = _seed_failed_mod(lib, mid="99")

    fresh = ModMetadata(
        published_file_id="99",
        title="Cover Mod",
        preview_url="https://example.com/a.png",
    )
    monkeypatch.setattr(
        SteamWorkshopClient,
        "refresh_details",
        lambda self, ids, **k: [fresh],
    )

    def fake_cover(self, metadata, dest_dir, *, filename=COVER_BASENAME):
        dest = Path(dest_dir) / f"{filename}.png"
        dest.write_bytes(b"\x89PNG\r\n")
        metadata.cover_path = str(dest)
        return dest

    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", fake_cover)

    result = refresh_steam_mod_metadata("99", folder, library_root=lib)
    assert result.success
    assert result.cover_path
    assert Path(result.cover_path).is_file() or (
        result.managed_path / INFO_DIR_NAME / "cover.png"
    ).is_file()


def test_batch_skips_healthy_and_dedupes_requests(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    failed = _seed_failed_mod(lib, mid="111")
    healthy_folder = lib / "Game" / "Healthy Mod"
    (healthy_folder / INFO_DIR_NAME).mkdir(parents=True)
    (healthy_folder / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {"published_file_id": "222", "title": "Healthy Mod"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fetch_ids: list[list[str]] = []

    def fake_refresh(self, ids, **kwargs):
        fetch_ids.append([str(i) for i in ids])
        return [
            ModMetadata(published_file_id=str(i), title=f"Fixed {i}") for i in ids
        ]

    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", fake_refresh)
    monkeypatch.setattr(
        SteamWorkshopClient,
        "fetch_and_save_cover",
        lambda *a, **k: None,
    )

    # Duplicate 111 should collapse to one network call.
    results = refresh_steam_mods_metadata(
        [
            ("111", failed),
            ("222", healthy_folder),
            ("111", failed),
        ],
        library_root=lib,
        max_workers=2,
        download_cover=False,
    )
    assert len(results) == 2
    by_id = {r.mod_id: r for r in results}
    assert by_id["222"].skipped is True
    assert by_id["111"].success is True
    assert by_id["111"].skipped is False
    # Only the failed id was fetched; once.
    assert fetch_ids == [["111"]]


def test_rename_collision_handled(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    game = lib / "Game"
    existing = game / "Cool Mod"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("x", encoding="utf-8")

    unknown = game / "Unknown_Mod_55"
    unknown.mkdir(parents=True)
    (unknown / INFO_DIR_NAME).mkdir()
    (unknown / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps({"published_file_id": "55", "title": "Cool Mod"}),
        encoding="utf-8",
    )

    meta = ModMetadata(published_file_id="55", title="Cool Mod")
    new_path, renamed = rename_managed_folder_for_title(
        unknown, meta, library_root=lib
    )
    assert renamed is True
    assert new_path.name == "Cool Mod_55"
    assert new_path.is_dir()
    assert existing.is_dir()
    assert not unknown.exists()


def test_safe_directory_rename_retries_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "Unknown_Mod_1"
    src.mkdir()
    (src / "f.txt").write_text("ok", encoding="utf-8")
    dest = tmp_path / "Real Name"

    calls = {"rename": 0, "move": 0}
    real_rename = os.rename
    sleeps: list[float] = []

    def flaky_rename(src_s: str, dst_s: str):
        calls["rename"] += 1
        if calls["rename"] < 3:
            raise PermissionError(5, "Access is denied", src_s)
        return real_rename(src_s, dst_s)

    def move_always_fails(src_s: str, dst_s: str):
        calls["move"] += 1
        raise PermissionError(5, "Access is denied", src_s)

    monkeypatch.setattr("services.metadata_refresh.os.rename", flaky_rename)
    monkeypatch.setattr(
        "services.windows_rename.move_directory_movefile_ex",
        move_always_fails,
    )
    monkeypatch.setattr(
        "services.metadata_refresh.time.sleep",
        lambda sec: sleeps.append(float(sec)),
    )

    out = safe_directory_rename(src, dest, attempts=5, delay_sec=0.2)
    assert out == dest
    assert dest.is_dir()
    assert calls["rename"] == 3
    assert calls["move"] == 2  # fallback on first two PermissionErrors
    assert not src.exists()
    assert sleeps == [0.2, 0.2]


def test_safe_directory_rename_exponential_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "Old"
    src.mkdir()
    dest = tmp_path / "New"
    calls = {"rename": 0, "move": 0}
    sleeps: list[float] = []

    def always_locked(src_s: str, dst_s: str):
        calls["rename"] += 1
        raise PermissionError(5, "Access is denied", src_s)

    def move_always_fails(src_s: str, dst_s: str):
        calls["move"] += 1
        raise PermissionError(5, "Access is denied", src_s)

    monkeypatch.setattr("services.metadata_refresh.os.rename", always_locked)
    monkeypatch.setattr(
        "services.windows_rename.move_directory_movefile_ex",
        move_always_fails,
    )
    monkeypatch.setattr(
        "services.metadata_refresh.time.sleep",
        lambda sec: sleeps.append(float(sec)),
    )

    with pytest.raises(PermissionError):
        safe_directory_rename(src, dest, backoff=(0.5, 1.0, 2.0))
    assert calls["rename"] == 4  # initial + 3 retries
    assert calls["move"] == 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_safe_directory_rename_escapes_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "Nested"
    src.mkdir()
    (src / "inner.txt").write_text("x", encoding="utf-8")
    dest = tmp_path / "Renamed"
    prev = Path.cwd()
    try:
        os.chdir(src)
        assert Path.cwd().resolve() == src.resolve()
        out = safe_directory_rename(src, dest, attempts=2, delay_sec=0.05)
        assert out == dest
        assert dest.is_dir()
        # Process must no longer sit inside the (moved) source path.
        assert Path.cwd().resolve() != src.resolve()
    finally:
        os.chdir(prev)


def test_safe_directory_rename_os_rename_success(
    tmp_path: Path,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "更大的油泵半径"
    src.mkdir()
    (src / "mod.txt").write_text("ok", encoding="utf-8")
    dest = tmp_path / "Bigger Oil Pump Radius [Spice It Up]"

    out = safe_directory_rename(src, dest, attempts=1, delay_sec=0.05)
    assert out == dest.resolve()
    assert dest.is_dir()
    assert not src.exists()
    assert (dest / "mod.txt").read_text(encoding="utf-8") == "ok"


def test_safe_directory_rename_movefile_ex_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "中文目录"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    dest = tmp_path / "English Name [OK]"

    real_rename = os.rename
    calls = {"rename": 0, "move": 0}

    def rename_denied(src_s: str, dst_s: str):
        calls["rename"] += 1
        raise PermissionError(5, "Access is denied", src_s)

    def move_ok(src_s: str, dst_s: str):
        calls["move"] += 1
        real_rename(src_s, dst_s)

    monkeypatch.setattr("services.metadata_refresh.os.rename", rename_denied)
    monkeypatch.setattr(
        "services.windows_rename.move_directory_movefile_ex",
        move_ok,
    )

    out = safe_directory_rename(src, dest, attempts=1, delay_sec=0.05)
    assert out == dest.resolve()
    assert dest.is_dir()
    assert calls == {"rename": 1, "move": 1}
    assert not src.exists()


def test_safe_directory_rename_target_exists(
    tmp_path: Path,
) -> None:
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "Source Mod"
    src.mkdir()
    dest = tmp_path / "Target Mod"
    dest.mkdir()

    with pytest.raises(FileExistsError):
        safe_directory_rename(src, dest, attempts=1, delay_sec=0.05)
    assert src.is_dir()
    assert dest.is_dir()


def test_format_directory_rename_error_mentions_external_lock() -> None:
    from services.metadata_refresh import format_directory_rename_error

    exc = PermissionError(5, "Access is denied", r"E:\mod\foo")
    msg = format_directory_rename_error(exc, source=Path(r"E:\mod\foo"))
    assert "WinError 5" in msg or "Access denied" in msg
    assert "external process" in msg


def test_safe_directory_rename_cancels_cover_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PySide6")
    from services.cover_loader import CoverLoaderManager
    from services.metadata_refresh import safe_directory_rename

    CoverLoaderManager.reset_instance()
    src = tmp_path / "Unknown Mod 9"
    src.mkdir()
    dest = tmp_path / "Shouted Out"

    cancelled: list[str] = []

    class FakeMgr:
        def cancel_for_managed_path(self, managed_path, *, wait_ms=2500):
            cancelled.append(str(managed_path))

        def request_ui_release(self, managed_path, *, wait_ms=400):
            cancelled.append(f"ui:{managed_path}")

        def inflight_count(self, managed_path):
            return 0

        def bound_card_token_count(self, managed_path):
            return 0

    monkeypatch.setattr(
        CoverLoaderManager,
        "instance",
        classmethod(lambda cls: FakeMgr()),
    )
    monkeypatch.setattr(
        "services.metadata_refresh.time.sleep",
        lambda *_a, **_k: None,
    )

    safe_directory_rename(src, dest, attempts=1, delay_sec=0.1)
    assert cancelled
    assert dest.is_dir()
    CoverLoaderManager.reset_instance()
