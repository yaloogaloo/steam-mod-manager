"""Phase 3-B: Resolver is pure-read; backup sync only on write events."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, load_backup
from services.metadata_backup_sync import sync_after_metadata_change
from services.mod_metadata_resolver import resolve_mod_metadata


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
    library: Path,
    *,
    game: str,
    title: str,
    mod_id: str,
    meta_title: str = "",
    with_cover: bool = False,
    with_offline: bool = False,
) -> Path:
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": mod_id,
        "title": meta_title or title,
        "display_name": meta_title or title,
        "game_name": game,
        "description": f"desc-{mod_id}",
        "source_type": "github",
        "url": f"https://example.com/{mod_id}",
        "source_url": f"https://example.com/{mod_id}",
        "workspace_id": f"ws-{mod_id}",
        "external_id": mod_id,
    }
    if with_cover:
        cover = info / "cover.jpg"
        cover.write_bytes(b"cover-bytes")
        payload["cover_path"] = ".info/cover.jpg"
    if with_offline:
        offline = info / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")
        payload["offline_page_path"] = ".info/offline/index.html"
        payload["offline_status"] = "generated"
    persist_unified_metadata_dict(folder, payload)
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    return folder


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
    folder = _write_mod(
        library, game="G", title="M1", mod_id="940001", meta_title="INFO", with_cover=True
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940001",
            title="INFO",
            game_name="G",
            managed_path=str(folder),
        )
    )
    before = _snapshot_backup_state("940001")
    time.sleep(0.02)
    resolved = resolve_mod_metadata("940001", folder)
    after = _snapshot_backup_state("940001")
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
    folder = _write_mod(
        library, game="G", title="M2", mod_id="940002", meta_title="Detail", with_offline=True
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940002",
            title="Detail",
            managed_path=str(folder),
        )
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
    panel.show_mod(folder, mod_id="940002")
    app.processEvents()
    assert calls == []
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_case3_edit_syncs_backup_title(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(library, game="G", title="M3", mod_id="940003", meta_title="A")
    db.upsert_mod(
        ModMetadata(
            published_file_id="940003",
            title="A",
            managed_path=str(folder),
        )
    )
    assert load_backup("940003").metadata.get("title") == "A"
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["title"] = "B"
    data["display_name"] = "B"
    persist_unified_metadata_dict(folder, data, sync_reason="edit")
    assert load_backup("940003").metadata.get("title") == "B"


def test_case4_repeated_sync_is_idempotent(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library,
        game="G",
        title="M4",
        mod_id="940004",
        meta_title="Idem",
        with_cover=True,
        with_offline=True,
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940004",
            title="Idem",
            managed_path=str(folder),
        )
    )
    sync_after_metadata_change("940004", folder, "edit")
    sync_after_metadata_change("940004", folder, "edit")
    sync_after_metadata_change("940004", folder, "edit")
    root = backup_root("940004")
    covers = list(root.glob("cover.*"))
    assert len(covers) == 1
    assert (root / "offline" / "index.html").is_file()
    assert not (root / "offline" / "index_1.html").exists()
    assert not list(root.glob("cover_*.jpg"))


def test_case5_deleting_info_cover_removes_backup_cover(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="G", title="M5", mod_id="940005", meta_title="Cover", with_cover=True
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940005",
            title="Cover",
            managed_path=str(folder),
        )
    )
    assert list(backup_root("940005").glob("cover.*"))
    cover = folder / INFO_DIR_NAME / "cover.jpg"
    cover.unlink()
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data.pop("cover_path", None)
    persist_unified_metadata_dict(folder, data, sync_backup=False)
    sync_after_metadata_change("940005", folder, "cover_change")
    assert list(backup_root("940005").glob("cover.*")) == []


def test_case6_deleting_mod_folder_keeps_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="G", title="M6", mod_id="940006", meta_title="Keep"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940006",
            title="Keep",
            managed_path=str(folder),
        )
    )
    assert (backup_root("940006") / "metadata.json").is_file()
    shutil.rmtree(folder)
    assert not folder.exists()
    assert (backup_root("940006") / "metadata.json").is_file()
    resolved = resolve_mod_metadata("940006", folder)
    assert resolved is not None
    assert resolved.folder_present is False
    assert (resolved.display_name or resolved.title) == "Keep"


def test_case7_backup_never_writes_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="G", title="M7", mod_id="940007", meta_title="INFO"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="940007",
            title="INFO",
            managed_path=str(folder),
        )
    )
    backup_meta = backup_root("940007") / "metadata.json"
    polluted = json.loads(backup_meta.read_text(encoding="utf-8"))
    polluted["title"] = "BACKUP"
    polluted["display_name"] = "BACKUP"
    backup_meta.write_text(json.dumps(polluted, indent=2), encoding="utf-8")
    info_before = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    resolved = resolve_mod_metadata("940007", folder)
    from services.metadata_backup_sync import rebuild_metadata_backup

    rebuild_metadata_backup("940007", folder, reason="repair")
    info_after = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    assert "INFO" in info_before
    assert info_before == info_after
    assert "BACKUP" not in info_after
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "INFO"
    # Repair re-copies from .info → backup title back to INFO
    assert load_backup("940007").metadata.get("title") == "INFO"


def test_case8_offline_manager_syncs_once(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.offline.base import OfflineUpdateResult
    from services.offline.manager import OfflineManager

    library = tmp_path / "mod"
    folder = _write_mod(library, game="G", title="M8", mod_id="940008", meta_title="Off")
    db.upsert_mod(
        ModMetadata(
            published_file_id="940008",
            title="Off",
            managed_path=str(folder),
            source_type="steam",
        )
    )
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
    result = mgr.update_mod_offline("940008", managed_path=folder, platform="steam")
    assert result.index_path is not None
    assert calls == ["offline_change"]
