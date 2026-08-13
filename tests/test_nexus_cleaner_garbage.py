"""Regression: strip browser-extension garbage from Nexus MHTML offline pages."""

from __future__ import annotations

from pathlib import Path

from services.offline.nexus_cleaner import NexusCleaner
from services.offline.nexus_cleaner.html_cleaner import clean_html

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _contaminated_html() -> str:
    # Real-world injectors observed in saved Nexus MHTML:
    # #dms-link-cleaner, #k-support-wrap, <remove-web-limits-iqxin>
    return """<!DOCTYPE html>
<html><head><title>City Variations</title></head>
<body id="bodyTop">
<div id="app">
  <h1 class="mod-title">City Variations</h1>
  <div class="mod-description">Beautiful city tiles for Anno 1800.</div>
  <img src="cid:cover.png" alt="cover">
  <section class="files">Main file — Download</section>
  <section class="requirements">Requires base game</section>
</div>
<remove-web-limits-iqxin id="rwl-iqxin" class="rwl-exempt">set 限制解除</remove-web-limits-iqxin>
<div id="dms-link-cleaner">
  <div id="dms-lc-button">︽</div>
  <div id="dms-lc-panel">
    <div id="dms-lc-panel-content">
      <div class="dms-lc-button" id="dmsCLButtonTitle">复制纯链接并附标题</div>
      <div class="dms-lc-button" id="dmsCLButtonPure">只复制纯链接</div>
      <div class="dms-lc-button" id="dmsCLButtonCopyTitle">复制当前链接及标题</div>
      <div class="dms-lc-button" id="dmsCLButtonCopyLink">仅复制当前链接</div>
      <div class="dms-lc-button" id="dmsCLButtonCleanAll">清除本页所有链接</div>
      <div class="dms-lc-button" id="dmsCLButtonLink">议题</div>
      <div class="dms-lc-button" id="dmsCLButtonCoffee">请请我喝杯咖啡</div>
      <div id="dms-lc-qrcode">
        链接地址洗白白，作者同学很可爱<br/>
        扫码请杯热咖啡，规则更新更勤快
      </div>
    </div>
  </div>
</div>
<div id="k-support-wrap" class="k-support-wrap">
  <div id="k-support-content" class="k-support-content">
    <div class="k-support-title">扫码打开小程序支持一下作者</div>
    <div class="k-support-close">关闭</div>
  </div>
</div>
</body></html>"""


def _build_contaminated_mhtml(path: Path) -> None:
    import base64

    boundary = "----GarbageExtBoundary"
    html_qp = _contaminated_html().replace("=", "=3D")
    b64 = base64.encodebytes(_PNG).decode("ascii")
    body = f"""From: <Saved by browser>
Snapshot-Content-Location: https://www.nexusmods.com/anno1800/mods/1
Subject: City Variations
MIME-Version: 1.0
Content-Type: multipart/related; type="text/html"; boundary="{boundary}"

--{boundary}
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: quoted-printable
Content-Location: https://www.nexusmods.com/anno1800/mods/1

{html_qp}

--{boundary}
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-ID: <cover.png>
Content-Location: cover.png

{b64}
--{boundary}--
"""
    path.write_bytes(body.encode("utf-8"))


def test_clean_html_removes_extension_overlays() -> None:
    cleaned = clean_html(_contaminated_html())
    assert "City Variations" in cleaned
    assert "Beautiful city tiles" in cleaned
    assert "Requires base game" in cleaned
    assert "dms-link-cleaner" not in cleaned
    assert "k-support-wrap" not in cleaned
    assert "rwl-iqxin" not in cleaned
    assert "复制纯链接并附标题" not in cleaned
    assert "请请我喝杯咖啡" not in cleaned
    assert "清除本页所有链接" not in cleaned


def test_nexus_cleaner_stages_and_final_drop_garbage(tmp_path: Path) -> None:
    mhtml = tmp_path / "contaminated.mhtml"
    _build_contaminated_mhtml(mhtml)
    out = tmp_path / "offline"
    debug = tmp_path / "stages"

    index, assets = NexusCleaner(clean=True, debug_dir=debug).process_file(mhtml, out)
    assert index.is_file()
    assert assets >= 1

    stage1 = (debug / "stage_1_extracted.html").read_text(encoding="utf-8")
    stage2 = (debug / "stage_2_cleaned.html").read_text(encoding="utf-8")
    stage4 = (debug / "stage_4_final.html").read_text(encoding="utf-8")
    final = index.read_text(encoding="utf-8")

    # A: garbage already present after parse_mhtml.
    assert "dms-link-cleaner" in stage1
    assert "复制纯链接并附标题" in stage1
    assert "请请我喝杯咖啡" in stage1

    # B: removed by clean_html (not introduced later).
    assert "dms-link-cleaner" not in stage2
    assert "复制纯链接并附标题" not in stage2
    assert "请请我喝杯咖啡" not in stage2
    assert "清除本页所有链接" not in stage2

    assert "dms-link-cleaner" not in stage4
    assert "复制纯链接并附标题" not in final
    assert "请请我喝杯咖啡" not in final
    assert "清除本页所有链接" not in final

    # Keep real Nexus content.
    assert "City Variations" in final
    assert "Beautiful city tiles" in final
    assert "Main file" in final or "Download" in final
    assert "cover" in final.lower() or "./assets/" in final
