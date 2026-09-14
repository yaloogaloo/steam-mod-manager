"""Phase 3-B: Resolver is pure-read; backup sync only on write events."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, load_backup
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from services.mod_metadata_resolver import resolve_mod_metadata
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "readonly.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _write_mod(
    db: DatabaseManager,
    library: Path,
    *,
    game: str,
    title: str,
    workshop: str,
    meta_title: str = "",
    with_cover: bool = False,
    with_offline: bool = False,
) -> tuple[Path, str]:
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    created = create_steam_test_mod(
        db, external_id=workshop, title=meta_title or title, game_name=game
    )
    pk = str(created.mod_id)
    extra: dict = {
        "display_name": meta_title or title,
        "description": f"desc-{workshop}",
        "source_type": "github",
        "url": f"https://example.com/{workshop}",
        "source_url": f"https://example.com/{workshop}",
    }
    if with_cover:
        cover = info / "cover.jpg"
        cover.write_bytes(b"cover-bytes")
        extra["cover_path"] = ".info/cover.jpg"
    if with_offline:
        offline = info / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")
        extra["offline_page_path"] = ".info/offline/index.html"
        extra["offline_status"] = "generated"
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title=meta_title or title,
        game_name=game,
        extra=extra,
    )
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    from services.metadata_backup import sync_metadata_backup

    sync_metadata_backup(folder, mod_id=pk)
    return folder, pk


def _snapshot_backup_state(mod_id: str) -> dict[str, object]:
    root = backup_root(mod_id)
    meta = root / "metadata.json"
    covers = sorted(p.name for p in root.glob("cover.*") if p.is_file())
    offline = root / "offline" / "index.html"
    return {
        "meta_mtime": meta.stat().st_mtime_ns if meta.is_file() else None,
        "meta_text": meta.read_text(encoding="utf-8") if meta.is_file() else None,
        "covers": covers,
        "offline_exists": offline.is_file(),
        "offline_text": offline.read_text(encoding="utf-8") if offline.is_file() else None,
    }


def test_case1_resolver_is_pure_read(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db, library, game="G", title="M1", workshop="940001", meta_title="INFO", with_cover=True
    )

    before = _snapshot_backup_state(pk)
    time.sleep(0.02)
    resolved = resolve_mod_metadata(pk, folder)
    after = _snapshot_backup_state(pk)
    assert resolved is not None
    assert resolved.display_name == "INFO" or resolved.title == "INFO"
    assert after == before


def test_case2_detail_open_does_not_sync(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db,
        library,
        game="G",
        title="M2",
        workshop="940002",
        meta_title="Detail",
        with_offline=True,
    )

    calls: list[tuple] = []

    def track(*args, **kwargs):
        calls.append((args, kwargs))
        return False

    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change", track
    )
    # Resolver must not import/call sync anymore; also guard backup low-level.
    monkeypatch.setattr(
        "services.metadata_backup.sync_metadata_backup",
        lambda *_a, **_k: calls.append(("low",)),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    app.processEvents()
    assert calls == []
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_case3_edit_syncs_backup_title(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(db, library, game="G", title="M3", workshop="940003", meta_title="A")

    assert load_backup(pk).metadata.get("title") == "A"
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["title"] = "B"
    data["display_name"] = "B"
    persist_unified_metadata_dict(folder, data, sync_reason="edit")
    drain_backup_queue(timeout=5.0)
    assert load_backup(pk).metadata.get("title") == "B"


def test_case4_repeated_sync_is_idempotent(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db,
        library,
        game="G",
        title="M4",
        workshop="940004",
        meta_title="Idem",
        with_cover=True,
        with_offline=True,
    )

    sync_after_metadata_change(pk, folder, "edit")
    sync_after_metadata_change(pk, folder, "edit")
    sync_after_metadata_change(pk, folder, "edit")
    drain_backup_queue(timeout=5.0)
    root = backup_root(pk)
    covers = list(root.glob("cover.*"))
    assert len(covers) == 1
    assert (root / "offline" / "index.html").is_file()
    assert not (root / "offline" / "index_1.html").exists()
    assert not list(root.glob("cover_*.jpg"))


def test_case5_deleting_info_cover_removes_backup_cover(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db, library, game="G", title="M5", workshop="940005", meta_title="Cover", with_cover=True
    )

    assert list(backup_root(pk).glob("cover.*"))
    cover = folder / INFO_DIR_NAME / "cover.jpg"
    cover.unlink()
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data.pop("cover_path", None)
    persist_unified_metadata_dict(folder, data, sync_backup=False)
    sync_after_metadata_change(pk, folder, "cover_change", wait=True)
    assert list(backup_root(pk).glob("cover.*")) == []


def test_case6_deleting_mod_folder_keeps_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db, library, game="G", title="M6", workshop="940006", meta_title="Keep"
    )

    assert (backup_root(pk) / "metadata.json").is_file()
    shutil.rmtree(folder)
    assert not folder.exists()
    assert (backup_root(pk) / "metadata.json").is_file()
    resolved = resolve_mod_metadata(pk, folder)
    assert resolved is not None
    assert resolved.folder_present is False
    assert (resolved.display_name or resolved.title) == "Keep"


def test_case7_backup_never_writes_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        db, library, game="G", title="M7", workshop="940007", meta_title="INFO"
    )

    backup_meta = backup_root(pk) / "metadata.json"
    polluted = json.loads(backup_meta.read_text(encoding="utf-8"))
    polluted["title"] = "BACKUP"
    polluted["display_name"] = "BACKUP"
    backup_meta.write_text(json.dumps(polluted, indent=2), encoding="utf-8")
    info_before = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    resolved = resolve_mod_metadata(pk, folder)
    from services.metadata_backup_sync import rebuild_metadata_backup

    rebuild_metadata_backup(pk, folder, reason="repair")
    info_after = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    assert "INFO" in info_before
    assert info_before == info_after
    assert "BACKUP" not in info_after
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "INFO"
    # Repair re-copies from .info → backup title back to INFO
    assert load_backup(pk).metadata.get("title") == "INFO"


def test_case8_offline_manager_syncs_once(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.offline.base import OfflineUpdateResult
    from services.offline.manager import OfflineManager

    library = tmp_path / "mod"
    folder, pk = _write_mod(db, library, game="G", title="M8", workshop="940008", meta_title="Off")

    calls: list[str] = []

    def fake_sync(mod_id, managed_path, reason):
        calls.append(str(reason))
        return True

    class FakeProvider:
        def get_provider_name(self):
            return "steam_archive"

        def update_offline_page(self, mod_id, **kwargs):
            offline = folder / INFO_DIR_NAME / "offline"
            offline.mkdir(parents=True, exist_ok=True)
            index = offline / "index.html"
            index.write_text("<html>steam</html>", encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=index,
                status="archived",
                provider=self.get_provider_name(),
            )

    mgr = OfflineManager(library_root=library)
    monkeypatch.setattr(mgr, "get_provider_for_platform", lambda *_a, **_k: FakeProvider())
    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change", fake_sync
    )
    result = mgr.update_mod_offline(pk, managed_path=folder, platform="steam")
    assert result.index_path is not None
    assert calls == ["offline_change"]
