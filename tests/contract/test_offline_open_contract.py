"""Phase 11: leftover OPEN closed, async miss, fail-closed finalize."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.info_asset_runtime import (
    OFFLINE_ASSET_UNAVAILABLE,
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
    prepare_offline_open,
    probe_live_offline_view_hit,
    probe_offline_open,
    safe_finalize_live_offline,
)
from services.offline.backup_closure import (
    ensure_backup_offline_openable,
    snapshot_offline_closure,
)
from services.offline_view_cache import is_offline_view_path

TINY_A = b"phase11-aaa"


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def _steam_mod(folder: Path, assets: dict[str, bytes]) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    return folder


def test_open_never_uses_live_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    # Leftover physical tree, no manifest / Store.
    opened = ensure_live_offline_openable(mod)
    assert opened is None
    result = prepare_offline_open(mod, mod_id="11")
    assert not result.ok
    assert result.reason == OFFLINE_ASSET_UNAVAILABLE
    assert (mod / ".info" / "assets" / "a.png").is_file()

    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    opened = ensure_live_offline_openable(mod, store=store, mod_id="11")
    assert opened is not None
    assert is_offline_view_path(opened)
    assert ".info" not in opened.parts or "offline_view" in str(opened)
    assert opened != (mod / ".info" / "index.html").resolve()
    assert not (mod / ".info" / "assets").exists()
    assert sha256_file(opened.parent / "assets" / "a.png") == sha256_bytes(TINY_A)


def test_open_never_uses_backup_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    dest = tmp_path / "bak" / "offline"
    dest.mkdir(parents=True)
    (dest / "assets").mkdir()
    (dest / "assets" / "a.png").write_bytes(TINY_A)
    (dest / "index.html").write_text(
        '<html><body><img src="./assets/a.png"></body></html>', encoding="utf-8"
    )
    opened = ensure_backup_offline_openable(dest)
    assert opened is None
    assert (dest / "assets" / "a.png").is_file()

    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    dest2 = tmp_path / "bak2" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest2)
    leftover = dest2 / "assets"
    leftover.mkdir(exist_ok=True)
    (leftover / "a.png").write_bytes(TINY_A)
    opened = ensure_backup_offline_openable(dest2)
    assert opened is not None
    assert is_offline_view_path(opened)
    assert opened != (dest2 / "index.html").resolve()


def test_open_cache_miss_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok

    probe = probe_offline_open(mod, mod_id="12")
    assert probe.cache_hit is None
    assert probe.can_materialize is True
    assert probe_live_offline_view_hit(mod, mod_id="12") is None

    entered = threading.Event()
    released = threading.Event()
    worker_name = {"name": ""}
    real_prepare = prepare_offline_open

    def blocked_prepare(*args, **kwargs):
        worker_name["name"] = threading.current_thread().name
        entered.set()
        assert released.wait(timeout=3.0)
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(
        "services.info_asset_runtime.prepare_offline_open", blocked_prepare
    )

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from core.db_manager import DatabaseManager
    from tests.helpers.identity import (
        bind_managed_path,
        create_steam_test_mod,
        write_info_sidecar,
    )
    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "p11.db")
    created = create_steam_test_mod(db, external_id="12", title="P11")
    pk = str(created.mod_id)
    write_info_sidecar(
        mod,
        internal_id=pk,
        title="P11",
        external_id="12",
        workspace_id="12",
    )
    bind_managed_path(db, pk, mod, title="P11")
    (mod / ".info" / "internal_id").write_text(pk, encoding="utf-8")

    opened: list[str] = []
    panel = ModDetailPanel()
    panel.show_mod(mod, mod_id=pk)
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    monkeypatch.setattr(
        "ui.offline_open.prepare_offline_open", blocked_prepare
    )

    ui_thread = threading.current_thread().name
    panel._open_offline()
    assert entered.wait(timeout=3.0)
    # UI thread continued while worker is blocked in prepare.
    assert worker_name["name"] != ui_thread
    assert opened == []
    released.set()

    from PySide6.QtCore import QEventLoop, QTimer

    worker = getattr(panel, "_offline_open_worker", None)
    assert worker is not None
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(8000)
    loop.exec()
    app.processEvents()
    assert opened, "worker should open cache/offline_view"
    assert is_offline_view_path(Path(opened[0]))
    DatabaseManager.reset_instance()


def test_finalize_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert (mod / ".info" / "assets" / "a.png").is_file()

    def boom(*_a, **_k):
        raise RuntimeError("migrate boom")

    monkeypatch.setattr("services.info_asset_runtime.migrate_info_assets", boom)
    result = safe_finalize_live_offline(mod, context="test")
    assert not result.ok
    assert "exception" in result.reason
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / "index.html").is_file()

    from services.info_asset_runtime import require_cas_finalize, CasFinalizeError

    mod2 = _steam_mod(tmp_path / "N", {"a.png": TINY_A})
    with pytest.raises(CasFinalizeError):
        require_cas_finalize(mod2, context="test2")
    assert not (mod2 / ".info" / "assets").exists()
