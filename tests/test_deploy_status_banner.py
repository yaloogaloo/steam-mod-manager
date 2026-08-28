"""Deploy failure status banner + archive error reasons."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DEPLOY_STATUS_FAILED, DatabaseManager
from core.models import ModMetadata
from services.importers.archive import (
    TOOL_UNAVAILABLE_MSG,
    RAR_TOOL_UNAVAILABLE_MSG,
    UNSUPPORTED_FMT_MSG,
    configure_rarfile_unrar_tool,
    extract_archive,
    resolve_bundled_unrar_tool,
)
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "banner.db")
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(root: Path, *, pub_id: str, title: str) -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_status_banner_hidden_by_default(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _mod_folder(tmp_path, pub_id="95001", title="OkMod")
    db.upsert_mod(ModMetadata(published_file_id="95001", title="OkMod"))
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()
    assert panel._status_banner.isHidden()


def test_status_banner_shows_concrete_deploy_failure(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _mod_folder(tmp_path, pub_id="95002", title="FailMod")
    db.upsert_mod(ModMetadata(published_file_id="95002", title="FailMod"))
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()

    panel.apply_deploy_result(
        {
            "success": False,
            "error": f"{UNSUPPORTED_FMT_MSG} .rar",
            "mod_id": "95002",
        }
    )
    qapp.processEvents()
    assert not panel._status_banner.isHidden()
    body = panel._status_banner_body.text()
    assert "部署失败" in body
    assert ".rar" in body
    assert body.strip() != "部署失败"
    assert panel._status_banner.property("tone") == "error"


def test_unified_ops_parent(qapp: QApplication) -> None:
    panel = ModDetailPanel()
    footer = panel._view_footer
    for btn in (
        panel.btn_folder,
        panel.btn_steam,
        panel.btn_offline,
        panel.btn_download_offline,
        panel.btn_deploy,
        panel.btn_redeploy,
        panel.btn_undeploy,
    ):
        assert btn.parentWidget() is footer


def test_resolve_bundled_unrar_tool_finds_project_binary() -> None:
    tool = resolve_bundled_unrar_tool()
    assert tool is not None
    assert tool.is_file()
    assert tool.name.lower() in {"unrar.exe", "unrar"}


def test_configure_rarfile_sets_unrar_tool() -> None:
    rarfile = pytest.importorskip("rarfile")
    path = configure_rarfile_unrar_tool(rarfile)
    assert path is not None
    assert Path(rarfile.UNRAR_TOOL) == path


def test_extract_unknown_suffix_has_concrete_reason(tmp_path: Path) -> None:
    bogus = tmp_path / "x.abc"
    bogus.write_bytes(b"nope")
    with pytest.raises(ValueError) as exc:
        extract_archive(bogus, dest_dir=tmp_path / "out")
    assert UNSUPPORTED_FMT_MSG in str(exc.value)
    assert ".abc" in str(exc.value)


def test_rar_without_tools_reports_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rar = tmp_path / "mod.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x00not-a-real-rar")
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable", lambda: None
    )

    def _boom(*_a, **_k):
        raise RuntimeError(RAR_TOOL_UNAVAILABLE_MSG)

    monkeypatch.setattr(
        "services.importers.archive._extract_rar_with_rarfile", _boom
    )
    with pytest.raises(RuntimeError) as exc:
        extract_archive(rar, dest_dir=tmp_path / "out")
    msg = str(exc.value)
    assert msg == RAR_TOOL_UNAVAILABLE_MSG
    assert "unrar" in msg.lower() or "UnRAR" in msg


def test_failed_db_status_rehydrates_banner(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _mod_folder(tmp_path, pub_id="95003", title="PersistFail")
    db.upsert_mod(ModMetadata(published_file_id="95003", title="PersistFail"))
    db.update_mod_deploy_status(
        "95003",
        deploy_status=DEPLOY_STATUS_FAILED,
        deploy_error=RAR_TOOL_UNAVAILABLE_MSG,
        deploy_path="",
        deploy_time="",
    )
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()
    assert not panel._status_banner.isHidden()
    body = panel._status_banner_body.text()
    assert "UnRAR" in body or "unrar" in body.lower()
    assert RAR_TOOL_UNAVAILABLE_MSG in body
