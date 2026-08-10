"""Nexus Offline Snapshot Cleaner — MHTML parse / clean / assets."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.offline.github import GithubOfflineProvider
from services.offline.manager import attach_nexus_offline_page
from services.offline.manual_import import import_offline_snapshot
from services.offline.nexus_cleaner import (
    CLEAN_VERSION,
    NexusCleaner,
    clean_mhtml_to_offline,
    parse_mhtml,
)
from services.offline.nexus_cleaner.html_cleaner import clean_html
from services.offline.nexus_cleaner.layout_optimizer import optimize_layout
from services.offline.steam import SteamOfflineProvider

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cleaner.db")
    yield manager
    DatabaseManager.reset_instance()


def _build_mhtml(path: Path, *, html_body: str | None = None) -> None:
    b64 = base64.encodebytes(_PNG).decode("ascii")
    boundary = "----MultipartBoundary_CleanerTest"
    if html_body is None:
        html_body = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="cid:style.css">
</head><body>
<h1 class="mod-title">Always Fast Travel</h1>
<div class="author">by Author</div>
<div class="mod-description">Teleport anywhere.</div>
<img src="cid:image001.png" alt="cover">
<section class="requirements">Requires UE4SS</section>
<section class="files">Main file</section>
<script>alert(1)</script>
<iframe src="https://ads.example"></iframe>
<div class="ads-container"></div>
<div class="login-modal"></div>
<div class="gallery"></div>
<div style="height:500px"></div>
</body></html>"""
    # quoted-printable: encode = as =3D for attribute-heavy HTML is optional;
    # use 7bit-safe ASCII without bare = in attrs for simplicity.
    html_qp = html_body.replace("=", "=3D")
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

{html_qp}

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


def test_mhtml_parser_returns_html_and_resources(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mhtml"
    _build_mhtml(mhtml)
    doc = parse_mhtml(mhtml)
    assert "Always Fast Travel" in doc.html
    assert doc.resources
    assert any(r.name.endswith(".png") for r in doc.resources)
    assert doc.cid_to_name


def test_html_cleaner_removes_ads() -> None:
    raw = '<div class="mod-description">ok</div><div class="ads">spam</div>'
    out = clean_html(raw)
    assert "ads" not in out.lower() or 'class="ads"' not in out
    assert "ok" in out
    assert "spam" not in out


def test_layout_optimizer_hides_tall_empty() -> None:
    raw = '<div class="mod-description">text</div><div style="height:500px"></div>'
    out = optimize_layout(raw)
    assert "display:none" in out.replace(" ", "").lower() or "display: none" in out.lower()
    assert "text" in out


def test_cleaner_rewrites_https_content_location(tmp_path: Path) -> None:
    """Chrome MHTML uses Content-Location URLs as img src, not cid:."""
    png_b64 = base64.encodebytes(_PNG).decode("ascii")
    boundary = "----MultipartBoundary_HttpsLoc"
    img_url = "https://staticdelivery.nexusmods.com/mods/1/images/cover.png"
    body = f"""From: <Saved by unit test>
MIME-Version: 1.0
Content-Type: multipart/related; type="text/html"; boundary="{boundary}"

--{boundary}
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: quoted-printable
Content-Location: https://www.nexusmods.com/palworld/mods/1

<!DOCTYPE html><html><body>
<img src=3D"{img_url}">
</body></html>

--{boundary}
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-Location: {img_url}

{png_b64}
--{boundary}--
"""
    mhtml = tmp_path / "https_loc.mhtml"
    mhtml.write_bytes(body.encode("utf-8"))
    out = tmp_path / "offline"
    index, count = clean_mhtml_to_offline(mhtml, out, clean=True)
    assert count >= 1
    html = index.read_text(encoding="utf-8")
    assert "cid:" not in html.lower()
    assert img_url not in html
    assert "assets/" in html
    assert list((out / "assets").glob("*.png"))


def test_cleaner_rewrites_cid_to_assets(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mhtml"
    _build_mhtml(mhtml)
    out = tmp_path / "offline"
    index, count = clean_mhtml_to_offline(mhtml, out, clean=True)
    assert count >= 1
    html = index.read_text(encoding="utf-8")
    assert "cid:" not in html.lower()
    assert "assets/" in html
    pngs = list((out / "assets").glob("*.png"))
    assert pngs
    assert any(p.name in html for p in pngs)
    # CSS should also lose cid refs
    css_files = list((out / "assets").glob("*.css"))
    if css_files:
        css = css_files[0].read_text(encoding="utf-8")
        assert "cid:" not in css.lower()


def test_cleaner_removes_chrome_keeps_core(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mhtml"
    _build_mhtml(mhtml)
    out = tmp_path / "offline"
    index, _ = NexusCleaner(clean=True).process_file(mhtml, out)
    html = index.read_text(encoding="utf-8").casefold()
    assert "always fast travel" in html
    assert "teleport anywhere" in html
    assert "requires ue4ss" in html
    assert "main file" in html
    assert "<script" not in html
    assert "<iframe" not in html
    assert "ads-container" not in html
    assert "login-modal" not in html
    # Tall empty placeholder is removed (empty-shell) or hidden (layout optimizer).
    assert "height:500px" not in html.replace(" ", "")


def test_import_offline_snapshot_metadata_cleaned(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mhtml"
    _build_mhtml(mhtml)
    out = tmp_path / "offline"
    index, _count, fmt = import_offline_snapshot(mhtml, out, clean=True)
    assert fmt == "mhtml"
    assert index.is_file()
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["provider"] == PROVIDER_NEXUS_MANUAL_IMPORT
    assert meta["source_format"] == "mhtml"
    assert meta["cleaned"] is True
    assert meta["clean_version"] == CLEAN_VERSION


def test_import_offline_snapshot_clean_false(tmp_path: Path) -> None:
    mhtml = tmp_path / "page.mhtml"
    _build_mhtml(mhtml)
    out = tmp_path / "offline"
    import_offline_snapshot(mhtml, out, clean=False)
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["cleaned"] is False
    assert "clean_version" not in meta


def test_nexus_attach_regression(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    mhtml = tmp_path / "mod.mhtml"
    _build_mhtml(mhtml)
    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="CleanMod",
        nexus_url="https://www.nexusmods.com/palworld/mods/911",
        nexus_id="911",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    attach = attach_nexus_offline_page(
        result.mod_id,
        mhtml,
        managed_path=result.managed_path,
        library_root=lib,
        clean=True,
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    index = Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    meta = json.loads(
        (index.parent / "metadata.json").read_text(encoding="utf-8")
    )
    assert meta["cleaned"] is True
    assert meta["clean_version"] == CLEAN_VERSION


def test_steam_github_providers_unchanged() -> None:
    """Cleaner must not alter Steam / GitHub offline provider contracts."""
    assert SteamOfflineProvider().get_provider_name()
    assert GithubOfflineProvider().get_provider_name()
    steam_update = getattr(SteamOfflineProvider, "update_offline_page", None)
    github_update = getattr(GithubOfflineProvider, "update_offline_page", None)
    assert callable(steam_update)
    assert callable(github_update)
    # Nexus-only entry points stay on manual import path.
    assert not hasattr(SteamOfflineProvider, "import_offline_page")
    assert not hasattr(GithubOfflineProvider, "import_offline_page")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_dialog_clean_checkbox_default_on(qapp, tmp_path: Path) -> None:
    from core.mod_platform import PLATFORM_NEXUS
    from ui.mod_import_dialog import ModImportDialog

    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_nexus.setChecked(True)
    dlg._on_platform_toggled()
    assert dlg.offline_clean_check.isChecked()
    params = dlg._collect_params(PLATFORM_NEXUS)
    # folder required — set a dummy path
    dlg.nexus_folder_edit.setText(str(tmp_path / "mod"))
    (tmp_path / "mod").mkdir(exist_ok=True)
    params = dlg._collect_params(PLATFORM_NEXUS)
    assert params is not None
    assert params["offline_clean"] is True
