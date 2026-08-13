"""Nexus offline import — MHTML / MHT support."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.offline.manual_import import (
    UnsupportedOfflineFormat,
    import_offline_snapshot,
)
from services.offline.manager import attach_nexus_offline_page
from services.offline.mhtml import rewrite_cid_references, store_mhtml_snapshot
from services.offline.nexus_cleaner import CLEAN_VERSION

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

# Minimal 1x1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mhtml.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _build_mhtml(path: Path) -> None:
    """Write a Chrome-like multipart/related MHTML file."""
    b64 = base64.encodebytes(_PNG).decode("ascii")
    boundary = "----MultipartBoundary_TestNexusOffline"
    body = f"""From: <Saved by unit test>
Snapshot-Content-Location: https://www.nexusmods.com/palworld/mods/1
Subject: Nexus Mod Page
Date: Sat, 08 Aug 2026 00:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/related;
	type="text/html";
	boundary="{boundary}"

--{boundary}
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: quoted-printable
Content-Location: https://www.nexusmods.com/palworld/mods/1

<!DOCTYPE html><html><head>
<link rel=3D"stylesheet" href=3D"cid:style.css">
</head><body>
<h1>MHTML Nexus</h1>
<img src=3D"cid:image001.png" alt=3D"cover">
</body></html>

--{boundary}
Content-Type: text/css
Content-Transfer-Encoding: quoted-printable
Content-ID: <style.css>
Content-Location: style.css

body{{color:red}}
.x{{background:url(cid:image001.png)}}

--{boundary}
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-ID: <image001.png>
Content-Location: image001.png

{b64}
--{boundary}--
"""
    path.write_bytes(body.encode("utf-8"))


def test_mhtml_import_writes_index_and_assets(tmp_path: Path) -> None:
    mhtml = tmp_path / "test.mhtml"
    _build_mhtml(mhtml)
    out = tmp_path / "offline"
    index, count, fmt = import_offline_snapshot(mhtml, out)
    assert fmt == "mhtml"
    assert index.is_file()
    assert count >= 1
    html = index.read_text(encoding="utf-8")
    assert "MHTML Nexus" in html
    assert "cid:" not in html.lower()
    assert "assets/" in html
    assert (out / "assets").is_dir()
    asset_files = [p for p in (out / "assets").rglob("*") if p.is_file()]
    assert any(p.suffix.lower() == ".png" for p in asset_files)
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["provider"] == PROVIDER_NEXUS_MANUAL_IMPORT
    assert meta["source_format"] == "mhtml"
    assert meta["cleaned"] is True
    assert meta["clean_version"] == CLEAN_VERSION


def test_mhtml_image_rewritten_to_assets(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mht"
    _build_mhtml(mhtml)
    out = tmp_path / "out"
    index, _count = store_mhtml_snapshot(mhtml, out)
    html = index.read_text(encoding="utf-8")
    assert "assets/" in html
    pngs = list((out / "assets").glob("*.png"))
    assert pngs
    assert any(p.name in html for p in pngs)


def test_html_import_still_works(tmp_path: Path) -> None:
    page = tmp_path / "test.html"
    page.write_text(
        "<!DOCTYPE html><html><body><h1>Plain HTML</h1></body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "offline"
    index, count, fmt = import_offline_snapshot(page, out)
    assert fmt == "html"
    assert "Plain HTML" in index.read_text(encoding="utf-8")
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source_format"] == "html"
    assert count >= 0


def test_unsupported_format_raises(tmp_path: Path) -> None:
    bad = tmp_path / "x.txt"
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(UnsupportedOfflineFormat):
        import_offline_snapshot(bad, tmp_path / "out")


def test_attach_nexus_offline_page_mhtml(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    mhtml = tmp_path / "mod.mhtml"
    _build_mhtml(mhtml)

    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="MhtmlMod",
        nexus_url="https://www.nexusmods.com/palworld/mods/910",
        nexus_id="910",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    attach = attach_nexus_offline_page(
        result.mod_id,
        mhtml,
        managed_path=result.managed_path,
        library_root=lib,
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert attach.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    index = Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.platform == PLATFORM_NEXUS
    assert row.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT


def test_rewrite_cid_helpers() -> None:
    html = '<img src="cid:foo.png"><style>x{background:url(cid:foo.png)}</style>'
    out = rewrite_cid_references(html, {"foo.png": "./assets/foo.png"})
    assert 'src="./assets/foo.png"' in out
    assert "url(./assets/foo.png)" in out
    assert "cid:" not in out.lower()


def test_dialog_filter_includes_mhtml(qapp, tmp_path: Path) -> None:
    from ui.mod_import_dialog import ModImportDialog

    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_steam.setChecked(True)
    dlg._on_platform_toggled()
    assert dlg.offline_html_row.isHidden()

    dlg.radio_nexus.setChecked(True)
    dlg._on_platform_toggled()
    assert not dlg.offline_html_row.isHidden()

    dlg.radio_github.setChecked(True)
    dlg._on_platform_toggled()
    assert dlg.offline_html_row.isHidden()
