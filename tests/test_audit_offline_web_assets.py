"""Tests for tools/audit_offline_web_assets.py (readonly audit helpers)."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.audit_offline_web_assets import (
    classify_extension,
    collect_dependency_closure_paths,
    extract_css_imports,
    extract_css_urls,
    extract_html_refs,
    sha256_file,
)


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_extract_html_refs_img_link_script() -> None:
    html = """
    <html><head>
      <link rel="stylesheet" href="./assets/a.css">
      <link rel="icon" href="./assets/fav.ico">
      <script src="./assets/app.js"></script>
    </head><body>
      <img src="./assets/pic.png">
      <video src="./assets/v.mp4" poster="./assets/p.jpg"></video>
      <source src="./assets/s.webm">
      <img srcset="./assets/a.png 1x, ./assets/b.png 2x">
    </body></html>
    """
    refs = extract_html_refs(html)
    joined = " ".join(refs)
    assert "./assets/a.css" in joined
    assert "./assets/app.js" in joined
    assert "./assets/pic.png" in joined
    assert "./assets/v.mp4" in joined
    assert "./assets/p.jpg" in joined
    assert "./assets/s.webm" in joined
    assert "./assets/a.png" in joined


def test_extract_css_url() -> None:
    css = """
    .a { background: url("./fonts/x.woff2"); }
    .b { background: url('images/y.png'); }
    .c { background: url(z.svg); }
    """
    urls = extract_css_urls(css)
    assert "./fonts/x.woff2" in urls
    assert "images/y.png" in urls
    assert "z.svg" in urls


def test_extract_css_import_and_recursion(tmp_path: Path) -> None:
    css = '@import url("./nested.css");\nbody{background:url("./bg.png");}'
    imports = extract_css_imports(css)
    assert any("nested.css" in i for i in imports)

    root = tmp_path / "offline"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (assets / "bg.png").write_bytes(TINY_PNG)
    (assets / "nested.css").write_text(
        '.x{background:url("./deep.png");}', encoding="utf-8"
    )
    (assets / "deep.png").write_bytes(TINY_PNG)
    (assets / "main.css").write_text(
        '@import url("./nested.css");\nbody{color:red;}', encoding="utf-8"
    )
    (root / "index.html").write_text(
        '<link rel="stylesheet" href="./assets/main.css">',
        encoding="utf-8",
    )
    closure = collect_dependency_closure_paths(root / "index.html")
    names = {p.name for p in closure}
    assert "index.html" in names
    assert "main.css" in names
    assert "nested.css" in names
    # css url() is walked into closure by production collector
    assert "deep.png" in names


def test_missing_asset_does_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "index.html").write_text(
        '<img src="./assets/missing.png"><link href="./assets/no.css" rel="stylesheet">',
        encoding="utf-8",
    )
    closure = collect_dependency_closure_paths(root / "index.html")
    assert any(p.name == "index.html" for p in closure)
    # missing files simply absent — no exception
    assert not any(p.name == "missing.png" for p in closure)


def test_duplicate_hash_detection(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(TINY_PNG)
    b.write_bytes(TINY_PNG)
    ha = sha256_file(a)
    hb = sha256_file(b)
    assert ha is not None and ha == hb
    c = tmp_path / "c.png"
    c.write_bytes(TINY_PNG + b"\x00")
    assert sha256_file(c) != ha


def test_info_backup_duplicate_bytes(tmp_path: Path) -> None:
    """Same content under .info/assets and backup/offline/assets hashes equal."""
    info_assets = tmp_path / "mod" / ".info" / "assets"
    backup_assets = tmp_path / "backup" / "offline" / "assets"
    info_assets.mkdir(parents=True)
    backup_assets.mkdir(parents=True)
    payload = TINY_PNG + b"unique-payload"
    (info_assets / "abc123.png").write_bytes(payload)
    (backup_assets / "abc123.png").write_bytes(payload)
    assert sha256_file(info_assets / "abc123.png") == sha256_file(
        backup_assets / "abc123.png"
    )
    identical = 0
    ih = sha256_file(info_assets / "abc123.png")
    bh = sha256_file(backup_assets / "abc123.png")
    if ih and bh and ih == bh:
        identical += (backup_assets / "abc123.png").stat().st_size
    assert identical == len(payload)


def test_extension_type_classification() -> None:
    assert classify_extension("x.PNG") == "png"
    assert classify_extension("a.jpeg") == "jpg"
    assert classify_extension("a.jpg") == "jpg"
    assert classify_extension("f.woff2") == "woff2"
    assert classify_extension("s.mjs") == "js"
    assert classify_extension("x.bin") == "other"
    assert classify_extension("page.HTML") == "html"


def test_audit_is_readonly_on_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running audit helpers must not mutate fixture trees."""
    from tools import audit_offline_web_assets as audit

    library = tmp_path / "mod"
    data = tmp_path / "data"
    backup = data / "mod_backup"
    db_path = data / "mod_manager.db"
    library.mkdir()
    data.mkdir()
    backup.mkdir()

    game = library / "GameA" / "ModOne"
    info = game / ".info"
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "keep.png").write_bytes(TINY_PNG)
    (info / "index.html").write_text(
        '<img src="./assets/keep.png">', encoding="utf-8"
    )
    (info / "entity_key").write_text("entity-test-1", encoding="utf-8")

    b_off = backup / "1" / "offline" / "assets"
    b_off.mkdir(parents=True)
    (backup / "1" / "offline" / "index.html").write_text(
        '<img src="./assets/keep.png">', encoding="utf-8"
    )
    (b_off / "keep.png").write_bytes(TINY_PNG)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE mods ("
        "mod_id INTEGER PRIMARY KEY, internal_id TEXT, workspace_id TEXT, "
        "last_known_path TEXT, platform TEXT, title TEXT)"
    )
    conn.execute(
        "INSERT INTO mods VALUES (1, 'entity-test-1', 'ws1', ?, 'steam', 'ModOne')",
        (str(game),),
    )
    conn.commit()
    conn.close()

    before_info = {
        p.relative_to(info).as_posix(): (
            p.stat().st_mtime_ns,
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in info.rglob("*")
        if p.is_file()
    }
    before_backup = {
        p.relative_to(backup).as_posix(): (
            p.stat().st_mtime_ns,
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in backup.rglob("*")
        if p.is_file()
    }
    before_db = db_path.read_bytes()

    out_dir = tmp_path / "_tmp_out"
    out_dir.mkdir()
    monkeypatch.setattr(audit, "OUT_DIR", out_dir)
    monkeypatch.setattr(audit, "OUT_JSON", out_dir / "audit.json")
    monkeypatch.setattr(audit, "OUT_REPORT", out_dir / "audit.md")
    monkeypatch.setattr(audit, "OUT_TOP_ASSETS", out_dir / "top_assets.json")
    monkeypatch.setattr(audit, "OUT_TOP_DUPES", out_dir / "top_dupes.json")
    monkeypatch.setattr(audit, "OUT_TOP_MODS", out_dir / "top_mods.json")
    monkeypatch.setattr(audit, "_REPO", tmp_path)

    payload = audit.run_audit(
        library_root=library,
        data_root=data,
        hash_assets=True,
    )
    assert payload["status"] == "AUDIT COMPLETE"
    assert payload["production_safety"]["production_files_modified"] == "NO"
    assert payload["production_safety"]["production_db_modified"] == "NO"

    after_info = {
        p.relative_to(info).as_posix(): (
            p.stat().st_mtime_ns,
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in info.rglob("*")
        if p.is_file()
    }
    after_backup = {
        p.relative_to(backup).as_posix(): (
            p.stat().st_mtime_ns,
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in backup.rglob("*")
        if p.is_file()
    }
    assert before_info == after_info
    assert before_backup == after_backup
    assert before_db == db_path.read_bytes()
    assert (out_dir / "audit.json").is_file()
    assert not list(info.glob("*.audit*"))


def test_broken_html_does_not_crash_extract() -> None:
    refs = extract_html_refs("<html><img src='./a.png'><<<>>><link href='./b.css'")
    assert isinstance(refs, list)
