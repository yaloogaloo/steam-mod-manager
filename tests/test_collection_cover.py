"""Collection Phase 3 — Collection Cover storage, isolation, membership scope."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from core.paths import collection_covers_dir
from services import collection as coll
from services.collection_cover import (
    list_member_cover_choices,
    member_cover_file,
    own_cover_files,
)
from services.file_ops import COVER_BASENAME, INFO_DIR_NAME
from services.identity_service import create_mod_identity, identity_create_scope

STARDEW = 413150


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "collection_cover.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _game(db: DatabaseManager) -> None:
    db.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew"))


def _mod(db: DatabaseManager, workshop_id: str, title: str) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=STARDEW,
            game_name="Stardew Valley",
            operation="import",
        )
    return str(created.mod_id)


def _save_image(path: Path, fmt: str = "PNG", color: int = 0x336699) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(24, 24, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path), fmt)
    return path


def test_local_jpg_and_png_install(qapp: QApplication, db: DatabaseManager, tmp_path: Path) -> None:
    _game(db)
    rec = coll.create_collection(STARDEW, "Covers", db=db)
    jpg = _save_image(tmp_path / "src.jpg", "JPEG")
    updated = coll.set_collection_cover(rec.collection_id, jpg, db=db)
    assert updated.cover_path == f"collection_covers/{rec.collection_id}.jpg"
    dest = collection_covers_dir() / f"{rec.collection_id}.jpg"
    assert dest.is_file()
    png = _save_image(tmp_path / "src.png", "PNG")
    updated = coll.set_collection_cover(rec.collection_id, png, db=db)
    assert updated.cover_path == f"collection_covers/{rec.collection_id}.png"
    assert (collection_covers_dir() / f"{rec.collection_id}.png").is_file()
    assert not dest.exists()


def test_same_extension_replacement(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    _game(db)
    rec = coll.create_collection(STARDEW, "SameExt", db=db)
    first = _save_image(tmp_path / "a.jpg", "JPEG", color=0x112233)
    coll.set_collection_cover(rec.collection_id, first, db=db)
    dest = collection_covers_dir() / f"{rec.collection_id}.jpg"
    before = dest.read_bytes()
    second = _save_image(tmp_path / "b.jpg", "JPEG", color=0xAABBCC)
    coll.set_collection_cover(rec.collection_id, second, db=db)
    after = dest.read_bytes()
    assert dest.is_file()
    assert after != before
    assert not (collection_covers_dir() / f"{rec.collection_id}.incoming.jpg").exists()
    assert coll.get_collection(rec.collection_id, db=db).cover_path.endswith(".jpg")


def test_db_failure_rolls_back_new_file(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _game(db)
    rec = coll.create_collection(STARDEW, "Rollback", db=db)
    jpg = _save_image(tmp_path / "keep.jpg", "JPEG")
    coll.set_collection_cover(rec.collection_id, jpg, db=db)
    old = collection_covers_dir() / f"{rec.collection_id}.jpg"
    assert old.is_file()
    old_bytes = old.read_bytes()
    old_rel = coll.get_collection(rec.collection_id, db=db).cover_path

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "update_collection_cover_path", _boom)
    png = _save_image(tmp_path / "new.png", "PNG")
    with pytest.raises(RuntimeError, match="db down"):
        coll.set_collection_cover(rec.collection_id, png, db=db)
    assert old.is_file()
    assert old.read_bytes() == old_bytes
    assert not (collection_covers_dir() / f"{rec.collection_id}.png").exists()
    assert coll.get_collection(rec.collection_id, db=db).cover_path == old_rel


def test_delete_collection_removes_only_own_cover(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    _game(db)
    a = coll.create_collection(STARDEW, "KeepMe", db=db)
    b = coll.create_collection(STARDEW, "DropMe", db=db)
    coll.set_collection_cover(a.collection_id, _save_image(tmp_path / "a.png"), db=db)
    coll.set_collection_cover(b.collection_id, _save_image(tmp_path / "b.png"), db=db)
    a_file = collection_covers_dir() / f"{a.collection_id}.png"
    b_file = collection_covers_dir() / f"{b.collection_id}.png"
    assert a_file.is_file() and b_file.is_file()
    assert coll.delete_collection(b.collection_id, db=db) is True
    assert a_file.is_file()
    assert not b_file.exists()
    assert own_cover_files(b.collection_id) == []


def test_set_cover_does_not_touch_mod_cover(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _forbid_apply(*_a, **_k):
        calls.append("apply")
        raise AssertionError("apply_cover_to_mod must not run")

    def _forbid_install(*_a, **_k):
        calls.append("install")
        raise AssertionError("install_cover_file must not run")

    monkeypatch.setattr(
        "services.importers.image_picker.apply_cover_to_mod", _forbid_apply
    )
    monkeypatch.setattr(
        "services.importers.image_picker.install_cover_file", _forbid_install
    )
    _game(db)
    mid = _mod(db, "88101", "Cover Mod")
    folder = tmp_path / "Stardew" / "CoverMod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    mod_cover = _save_image(info / f"{COVER_BASENAME}.png")
    db.update_mod_identity_fields(mid, last_known_path=str(folder), folder_present=True)
    db.update_mod_cover_path(mid, f"{INFO_DIR_NAME}/{COVER_BASENAME}.png")
    before_meta = db.get_mod_display_info(mid)
    rec = coll.create_collection(STARDEW, "FromMod", db=db)
    coll.add_mod_to_collection(rec.collection_id, mid, db=db)
    coll.set_collection_cover_from_member(rec.collection_id, mid, db=db)
    assert mod_cover.is_file()
    after_meta = db.get_mod_display_info(mid)
    assert after_meta is not None and before_meta is not None
    assert after_meta.cover_path == before_meta.cover_path
    copied = collection_covers_dir() / f"{rec.collection_id}.png"
    assert copied.is_file()
    assert copied.resolve() != mod_cover.resolve()
    assert calls == []


def test_member_cover_dialog_scope(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    _game(db)
    a = _mod(db, "88201", "Mod A")
    b = _mod(db, "88202", "Mod B")
    c = _mod(db, "88203", "Mod C")
    pack_a = coll.create_collection(STARDEW, "Pack A", db=db)
    pack_b = coll.create_collection(STARDEW, "Pack B", db=db)
    coll.add_mods_to_collection(pack_a.collection_id, [a, b], db=db)
    coll.add_mod_to_collection(pack_b.collection_id, c, db=db)
    names = {row.name for row in list_member_cover_choices(pack_a.collection_id, db=db)}
    ids = {row.internal_id for row in list_member_cover_choices(pack_a.collection_id, db=db)}
    assert "Mod A" in names
    assert "Mod B" in names
    assert "Mod C" not in names
    assert c not in ids
    assert a in ids and b in ids
    from ui.collection_cover_dialog import CollectionCoverDialog

    dlg = CollectionCoverDialog(
        "Pack A", list_member_cover_choices(pack_a.collection_id, db=db)
    )
    listed = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert listed == ["Mod A", "Mod B"]
    dlg.deleteLater()


def test_member_without_cover_fails_without_writing(
    qapp: QApplication, db: DatabaseManager
) -> None:
    _game(db)
    mid = _mod(db, "88301", "Bare")
    rec = coll.create_collection(STARDEW, "EmptyCover", db=db)
    coll.add_mod_to_collection(rec.collection_id, mid, db=db)
    choices = list_member_cover_choices(rec.collection_id, db=db)
    assert len(choices) == 1
    assert choices[0].name == "Bare"
    assert choices[0].cover_file is None
    assert member_cover_file(mid, db=db) is None
    with pytest.raises(ValueError, match="没有可用封面"):
        coll.set_collection_cover_from_member(rec.collection_id, mid, db=db)
    assert coll.get_collection(rec.collection_id, db=db).cover_path == ""


def test_non_member_cannot_supply_cover(
    qapp: QApplication, db: DatabaseManager
) -> None:
    _game(db)
    inside = _mod(db, "88401", "Inside")
    outside = _mod(db, "88402", "Outside")
    rec = coll.create_collection(STARDEW, "MembersOnly", db=db)
    coll.add_mod_to_collection(rec.collection_id, inside, db=db)
    with pytest.raises(ValueError, match="not a member"):
        coll.set_collection_cover_from_member(rec.collection_id, outside, db=db)
    assert coll.get_collection(rec.collection_id, db=db).cover_path == ""


def test_rejects_user_absolute_path_in_db(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    from services.collection_cover import absolute_collection_cover_path

    _game(db)
    rec = coll.create_collection(STARDEW, "RelOnly", db=db)
    src = _save_image(tmp_path / "ok.png")
    updated = coll.set_collection_cover(rec.collection_id, src, db=db)
    assert not Path(updated.cover_path).is_absolute()
    assert updated.cover_path.startswith("collection_covers/")
    with pytest.raises(ValueError, match="relative"):
        absolute_collection_cover_path(str(tmp_path / "ok.png"))
