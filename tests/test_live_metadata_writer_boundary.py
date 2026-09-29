"""Live `.info/metadata.json` writers must go through persist_unified_metadata_dict."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup_sync import drain_backup_queue
from services.metadata_ownership import merge_official_sidecar_fields
from services.metadata_refresh import (
    _clear_placeholder_display_name,
    _persist_cleared_fetch_error,
)
from services.modio_api import map_mod_object
from services.modio_metadata_refresh import (
    _patch_metadata_json,
    refresh_modio_mod_metadata,
)
from services.path_lifecycle import commit_path_change
from tests.helpers.identity import bind_managed_path, create_modio_test_mod, write_info_sidecar

APP_ID = 4242
GAME = "小丑牌"
REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = REPO_ROOT / "services"

_ALLOWED_LIVE_METADATA_WRITE_FUNCS = frozenset({"_commit_unified_metadata"})


class _MetadataWriteProbe:
    def __init__(self) -> None:
        self.live_writes = 0
        self.live_bytes = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = Path.write_text
        probe = self

        def wrapped(self_path: Path, data: str, *args: object, **kwargs: object) -> int:
            if (
                Path(self_path).name == METADATA_FILENAME
                and Path(self_path).parent.name == INFO_DIR_NAME
            ):
                encoding = str(kwargs.get("encoding") or "utf-8")
                nbytes = (
                    len(data)
                    if isinstance(data, (bytes, bytearray))
                    else len(str(data).encode(encoding))
                )
                probe.live_writes += 1
                probe.live_bytes += nbytes
            return real(self_path, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", wrapped)


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def _pin_mtime(path: Path) -> int:
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    return path.stat().st_mtime_ns


def _meta(folder: Path) -> Path:
    return folder / INFO_DIR_NAME / METADATA_FILENAME


def _load(folder: Path) -> dict:
    return json.loads(_meta(folder).read_text(encoding="utf-8"))


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "live_meta_boundary.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name=GAME, folder_name=GAME))
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


@pytest.fixture()
def library_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "mod"
    lib.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.paths.default_mod_library", lambda: lib)
    monkeypatch.setattr("services.mod_path_validation.default_mod_library", lambda: lib)
    monkeypatch.setattr(
        "services.path_lifecycle.default_mod_library", lambda: lib, raising=False
    )
    return lib


def _seed_steam(
    db: DatabaseManager,
    library: Path,
    *,
    folder: str,
    workshop_id: str,
    title: str,
    extra: dict | None = None,
) -> tuple[Path, str, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=APP_ID,
            game_name=GAME,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    path = library / GAME / folder
    path.mkdir(parents=True)
    (path / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        path,
        internal_id=frozen,
        title=title,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=APP_ID,
        game_name=GAME,
        extra=extra,
    )
    bind_managed_path(db, pk, path, game_name=GAME, title=title)
    db.update_mod_identity_fields(
        pk,
        workspace_id=str(created.workspace_id or workshop_id),
        last_known_path=str(path.resolve()),
        folder_present=True,
    )
    return path, pk, frozen


def test_steam_fetch_error_clear_writes_then_second_pass_skips(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, _pk, _frozen = _seed_steam(
        db,
        library_root,
        folder="FetchErr",
        workshop_id="88021",
        title="FetchErr",
        extra={"fetch_error": "GetPublishedFileDetails timeout"},
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    _persist_cleared_fetch_error(folder)
    assert "fetch_error" not in _load(folder)
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime

    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe.live_writes = 0
    _persist_cleared_fetch_error(folder)
    _persist_cleared_fetch_error(folder)
    assert "fetch_error" not in _load(folder)
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime


def test_steam_placeholder_clear_writes_then_skips(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk, _frozen = _seed_steam(
        db,
        library_root,
        folder="Placeholder",
        workshop_id="88022",
        title="Placeholder",
        extra={"display_name": "Unknown_Mod_88022"},
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    _clear_placeholder_display_name(pk, folder)
    disk = _load(folder)
    assert "display_name" not in disk
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime

    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe.live_writes = 0
    _clear_placeholder_display_name(pk, folder)
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime


def test_steam_user_override_protects_placeholder(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk, _frozen = _seed_steam(
        db,
        library_root,
        folder="Override",
        workshop_id="88023",
        title="Override",
        extra={"display_name": "Unknown_Mod_88023"},
    )
    db.set_user_override_field(pk, "display_name", overridden=True)
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    _clear_placeholder_display_name(pk, folder)
    assert _load(folder)["display_name"] == "Unknown_Mod_88023"
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime


def _modio_details():
    return map_mod_object(
        {
            "id": 424242,
            "game_id": 1111,
            "name": "Harbor Life",
            "name_id": "harborlife",
            "summary": "Short summary",
            "description": "Full description from API",
            "profile_url": "https://mod.io/g/anno-1800/m/harborlife",
            "logo": {
                "original": "https://example.com/logo.png",
                "thumb_640x360": "https://example.com/logo_640.png",
            },
            "submitted_by": {"username": "harbor_author"},
        }
    )


def _seed_modio(db: DatabaseManager, library: Path) -> tuple[Path, str]:
    created = create_modio_test_mod(
        db,
        external_id="harborlife",
        title="Harbor Life",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        app_id=916440,
        game_name="Anno 1800",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = library / "Anno 1800" / "Harbor Life"
    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="Harbor Life",
        external_id="harborlife",
        workspace_id=str(created.workspace_id or "harborlife"),
        app_id=916440,
        game_name="Anno 1800",
        platform=PLATFORM_MODIO,
        extra={
            "url": "https://mod.io/g/anno-1800/m/harborlife",
            "source_url": "https://mod.io/g/anno-1800/m/harborlife",
            "source_type": PLATFORM_MODIO,
            "display_name": "Harbor Life",
            "description": "Full description from API",
            "author": "harbor_author",
            "modio_mod_id": 424242,
            "modio_game_id": 1111,
            "modio_name_id": "harborlife",
            "preview_url": "https://example.com/logo.png",
        },
    )
    bind_managed_path(db, pk, folder, game_name="Anno 1800", title="Harbor Life")
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        workspace_id=str(created.workspace_id or "harborlife"),
    )
    return folder, pk


def test_modio_metadata_unchanged_skips_write(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_modio(db, library_root)
    details = _modio_details()
    _patch_metadata_json(
        folder,
        title=details.name,
        description=details.description,
        url="https://mod.io/g/anno-1800/m/harborlife",
        author=details.author,
        preview_url=details.logo_url,
        modio_mod_id=details.mod_id,
        modio_game_id=details.game_id,
        name_id=details.name_id,
        cover_rel=".info/cover.png",
        mod_id=pk,
        db=db,
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    _patch_metadata_json(
        folder,
        title=details.name,
        description=details.description,
        url="https://mod.io/g/anno-1800/m/harborlife",
        author=details.author,
        preview_url=details.logo_url,
        modio_mod_id=details.mod_id,
        modio_game_id=details.game_id,
        name_id=details.name_id,
        cover_rel=".info/cover.png",
        mod_id=pk,
        db=db,
    )
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime
    assert _load(folder)["title"] == "Harbor Life"
    assert _load(folder)["internal_id"]


def test_modio_metadata_change_writes(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_modio(db, library_root)
    details = _modio_details()
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    _patch_metadata_json(
        folder,
        title="Harbor Life Revised",
        description=details.description,
        url="https://mod.io/g/anno-1800/m/harborlife",
        author=details.author,
        preview_url=details.logo_url,
        modio_mod_id=details.mod_id,
        modio_game_id=details.game_id,
        name_id=details.name_id,
        cover_rel=".info/cover.png",
        mod_id=pk,
        db=db,
    )
    assert _load(folder)["title"] == "Harbor Life Revised"
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime


def test_modio_cover_unchanged_skips_write(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_modio(db, library_root)
    details = _modio_details()
    _patch_metadata_json(
        folder,
        title=details.name,
        description=details.description,
        url="https://mod.io/g/anno-1800/m/harborlife",
        author=details.author,
        preview_url=details.logo_url,
        modio_mod_id=details.mod_id,
        modio_game_id=details.game_id,
        name_id=details.name_id,
        cover_rel=".info/cover.png",
        mod_id=pk,
        db=db,
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    overrides = db.get_user_override_fields(pk)
    merged = merge_official_sidecar_fields(
        read_info_metadata_dict(folder) or {},
        mod_id=pk,
        overrides=overrides,
        official_title=details.name,
        official_description=details.description,
        official_preview_url=details.logo_url,
        cover_rel=".info/cover.png",
    )
    persist_unified_metadata_dict(
        folder, merged, sync_backup=False, sync_reason="refresh"
    )
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime
    assert _load(folder).get("cover_path") == ".info/cover.png"


def test_modio_cover_change_writes(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_modio(db, library_root)
    details = _modio_details()
    _patch_metadata_json(
        folder,
        title=details.name,
        description=details.description,
        url="https://mod.io/g/anno-1800/m/harborlife",
        author=details.author,
        preview_url=details.logo_url,
        modio_mod_id=details.mod_id,
        modio_game_id=details.game_id,
        name_id=details.name_id,
        cover_rel="",
        mod_id=pk,
        db=db,
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    overrides = db.get_user_override_fields(pk)
    merged = merge_official_sidecar_fields(
        read_info_metadata_dict(folder) or {},
        mod_id=pk,
        overrides=overrides,
        official_title=details.name,
        official_description=details.description,
        official_preview_url=details.logo_url,
        cover_rel=".info/cover.webp",
    )
    persist_unified_metadata_dict(
        folder, merged, sync_backup=False, sync_reason="refresh"
    )
    assert _load(folder).get("cover_path") == ".info/cover.webp"
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime


def test_modio_refresh_second_pass_zero_metadata_writes(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_modio(db, library_root)
    details = _modio_details()
    (folder / INFO_DIR_NAME / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    first = refresh_modio_mod_metadata(
        pk,
        folder,
        library_root=library_root,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
        db=db,
    )
    assert first.success
    live = Path(first.managed_path or folder)
    meta = _meta(live)
    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    second = refresh_modio_mod_metadata(
        pk,
        live,
        library_root=library_root,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
        db=db,
    )
    assert second.success
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime


def test_path_lifecycle_real_change_writes_then_repeat_skips(
    db: DatabaseManager, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = library_root / GAME / "GhostOld"
    old_prefix = str(ghost.resolve())
    folder, pk, _frozen = _seed_steam(
        db,
        library_root,
        folder="MovedMod",
        workshop_id="88024",
        title="MovedMod",
        extra={
            "managed_path": old_prefix,
            "local_path": old_prefix,
            "offline_page_path": old_prefix + "/.info/offline/index.html",
            "is_missing_content": False,
        },
    )
    meta = _meta(folder)
    mtime = _pin_mtime(meta)
    probe = _MetadataWriteProbe()
    probe.install(monkeypatch)
    first = commit_path_change(
        pk,
        old_path=ghost,
        new_path=folder,
        renamed=True,
        reason="refresh",
        sync_backup=False,
        db=db,
    )
    assert first.success
    disk = _load(folder)
    resolved = str(folder.resolve())
    assert disk.get("managed_path") == resolved
    assert str(disk.get("offline_page_path") or "").startswith(resolved)
    assert probe.live_writes == 1
    assert _mtime_ns(meta) != mtime

    mtime = _pin_mtime(meta)
    body = meta.read_bytes()
    probe.live_writes = 0
    second = commit_path_change(
        pk,
        old_path=folder,
        new_path=folder,
        renamed=False,
        reason="refresh",
        sync_backup=False,
        db=db,
    )
    assert second.success
    assert probe.live_writes == 0
    assert meta.read_bytes() == body
    assert _mtime_ns(meta) == mtime


def _call_enclosing_function(tree: ast.AST, lineno: int) -> str:
    found = ""
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = parent.end_lineno or parent.lineno
        if parent.lineno <= lineno <= end:
            if not found or parent.lineno >= 0:
                found = parent.name
    return found


def _live_metadata_write_text_hits() -> list[str]:
    hits: list[str] = []
    for path in SERVICES_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            kind = ""
            if isinstance(func, ast.Attribute) and func.attr == "write_text":
                kind = "write_text"
            else:
                continue
            enclosing = _call_enclosing_function(tree, node.lineno)
            start = max(0, node.lineno - 10)
            window = "\n".join(lines[start : node.lineno])
            if "metadata.json" not in window and "METADATA_FILENAME" not in window:
                continue
            if enclosing in _ALLOWED_LIVE_METADATA_WRITE_FUNCS:
                continue
            if "BACKUP_METADATA_NAME" in window:
                continue
            if "output_dir" in window:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            hits.append(f"{rel}:{node.lineno}:{enclosing or '?'} {kind}")
    return hits


def test_services_have_zero_live_metadata_direct_writers() -> None:
    hits = _live_metadata_write_text_hits()
    assert hits == [], "Live metadata direct writers remain:\n" + "\n".join(hits)
