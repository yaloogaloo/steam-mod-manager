"""Shared offline HTML generator for Nexus / GitHub (and local stubs)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    ModFileEntry,
    ModFilesBundle,
    normalize_platform,
)

_PLATFORM_LABELS = {
    PLATFORM_STEAM: "Steam Workshop",
    PLATFORM_NEXUS: "Nexus Mods",
    PLATFORM_GITHUB: "GitHub",
}


def _platform_label(platform: str) -> str:
    key = normalize_platform(platform)
    return _PLATFORM_LABELS.get(key, key)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _cover_href(info_dir: Path, cover: str | Path | None) -> str:
    if cover is None:
        return ""
    path = Path(cover)
    if not path.is_file():
        return ""
    try:
        rel = path.resolve().relative_to(info_dir.resolve())
        return rel.as_posix()
    except ValueError:
        try:
            return path.resolve().as_uri()
        except OSError:
            return ""


def _files_from(files: ModFilesBundle | Sequence[ModFileEntry] | Mapping[str, Any] | None) -> list[ModFileEntry]:
    if files is None:
        return []
    if isinstance(files, ModFilesBundle):
        return list(files.files)
    if isinstance(files, Mapping):
        return list(ModFilesBundle.from_dict(files).files)
    return list(files)


def _file_mark(enabled: bool) -> str:
    return "✓" if enabled else "□"


def generate_offline_html(
    *,
    title: str,
    platform: str,
    metadata: Mapping[str, Any] | None = None,
    files: ModFilesBundle | Sequence[ModFileEntry] | Mapping[str, Any] | None = None,
    cover: str | Path | None = None,
    description: str = "",
    info_dir: str | Path | None = None,
) -> str:
    """
    Build a unified local offline info page (Steam-ish dark theme).

    Does not fetch the network. Used by Nexus / GitHub generators.
    """
    meta = dict(metadata or {})
    plat = normalize_platform(platform)
    platform_label = _platform_label(plat)
    title_text = (title or "").strip() or "Untitled Mod"
    external_id = str(meta.get("external_id") or "").strip()
    source_url = str(meta.get("source_url") or "").strip()
    author = str(meta.get("author") or "").strip()
    repository = str(meta.get("repository") or external_id).strip()
    readme = str(meta.get("readme") or "").strip()
    desc = (description or str(meta.get("description") or "")).strip()

    info_path = Path(info_dir) if info_dir else None
    cover_href = _cover_href(info_path, cover) if info_path else ""
    if not cover_href and cover and Path(cover).is_file():
        try:
            cover_href = Path(cover).resolve().as_uri()
        except OSError:
            cover_href = ""

    file_rows = _files_from(files)
    files_html: list[str] = []
    for entry in file_rows:
        label = (entry.name or entry.filename or entry.path or "file").strip()
        type_label = (entry.type or "main").strip()
        files_html.append(
            "<li>"
            f"<span class=\"mark\">{_esc(_file_mark(bool(entry.enabled)))}</span> "
            f"<span class=\"fname\">{_esc(label)}</span> "
            f"<span class=\"ftype\">({_esc(type_label)})</span>"
            "</li>"
        )
    files_block = (
        "<ul class=\"files\">\n"
        + "\n".join(files_html)
        + "\n</ul>"
        if files_html
        else "<p class=\"muted\">（无文件记录）</p>"
    )

    author_block = (
        f'<div class="row"><span class="k">作者</span>'
        f'<span class="v">{_esc(author)}</span></div>\n'
        if author
        else ""
    )
    repo_block = ""
    if plat == "github" and repository:
        repo_block = (
            f'<div class="row"><span class="k">Repository</span>'
            f'<span class="v"><code>{_esc(repository)}</code></span></div>\n'
        )
    id_label = {
        "steam": "Workshop ID",
        "nexus": "Nexus ID",
        "github": "Repository",
    }.get(plat, "External ID")
    id_value = repository if plat == "github" and repository else external_id
    id_block = (
        f'<div class="row"><span class="k">{_esc(id_label)}</span>'
        f'<span class="v"><code>{_esc(id_value)}</code></span></div>\n'
        if id_value
        else ""
    )
    source_block = (
        f'<div class="row"><span class="k">来源</span>'
        f'<span class="v"><a href="{_esc(source_url)}">{_esc(source_url)}</a></span></div>\n'
        if source_url
        else ""
    )
    cover_block = (
        f'<div class="cover"><img src="{_esc(cover_href)}" alt="cover"></div>\n'
        if cover_href
        else ""
    )
    desc_block = (
        f'<section class="section"><h2>说明</h2>'
        f'<p class="desc">{_esc(desc)}</p></section>\n'
        if desc
        else ""
    )
    readme_block = ""
    if readme:
        snippet = readme if len(readme) <= 4000 else readme[:4000] + "\n…"
        readme_block = (
            '<section class="section"><h2>README</h2>'
            f'<pre class="readme">{_esc(snippet)}</pre></section>\n'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="smm-offline-provider" content="{_esc(plat)}">
<title>{_esc(title_text)} — Offline</title>
<style>
  body {{
    margin: 0;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    background: #1b2838;
    color: #c7d5e0;
    padding: 32px 20px 48px;
    line-height: 1.55;
  }}
  .page {{
    max-width: 720px;
    margin: 0 auto;
    background: #171a21;
    border: 1px solid #2c4054;
    border-radius: 8px;
    padding: 24px 28px 28px;
  }}
  .header {{
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 20px;
  }}
  .cover img {{
    width: 160px;
    height: 120px;
    object-fit: cover;
    border-radius: 4px;
    border: 1px solid #2c4054;
    background: #0e141b;
  }}
  .titles {{ flex: 1; min-width: 200px; }}
  h1 {{
    color: #66c0f4;
    font-size: 22px;
    margin: 0 0 10px;
    font-weight: 600;
  }}
  .badge {{
    display: inline-block;
    background: #2a475e;
    color: #66c0f4;
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 3px;
    letter-spacing: 0.02em;
  }}
  .row {{
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 8px;
    margin: 6px 0;
    font-size: 14px;
  }}
  .k {{ color: #8f98a0; }}
  .v {{ color: #c7d5e0; word-break: break-word; }}
  a {{ color: #66c0f4; }}
  code {{ color: #acb2b8; font-size: 13px; }}
  .section {{ margin-top: 22px; }}
  .section h2 {{
    margin: 0 0 10px;
    font-size: 15px;
    color: #66c0f4;
    font-weight: 600;
    border-bottom: 1px solid #2c4054;
    padding-bottom: 6px;
  }}
  .files {{ list-style: none; margin: 0; padding: 0; }}
  .files li {{
    padding: 6px 0;
    border-bottom: 1px solid #1f2d3a;
    font-size: 14px;
  }}
  .mark {{ color: #66c0f4; margin-right: 4px; }}
  .ftype {{ color: #8f98a0; font-size: 12px; }}
  .muted {{ color: #8f98a0; font-size: 13px; }}
  .desc {{ margin: 0; white-space: pre-wrap; font-size: 14px; color: #acb2b8; }}
  .readme {{
    margin: 0;
    white-space: pre-wrap;
    font-size: 12px;
    color: #acb2b8;
    background: #0e141b;
    border: 1px solid #2c4054;
    border-radius: 4px;
    padding: 12px;
    max-height: 320px;
    overflow: auto;
  }}
  .banner {{
    font-size: 12px;
    color: #8f98a0;
    margin-bottom: 14px;
  }}
</style>
</head>
<body>
  <div class="page">
    <div class="banner">本地离线信息页 · 不访问远程站点</div>
    <div class="header">
      {cover_block}<div class="titles">
        <h1>{_esc(title_text)}</h1>
        <span class="badge">{_esc(platform_label)}</span>
      </div>
    </div>
    <section class="section">
      <h2>基础信息</h2>
      <div class="row"><span class="k">平台</span>
        <span class="v">{_esc(platform_label)}</span></div>
      {id_block}{repo_block}{source_block}{author_block}</section>
    {desc_block}<section class="section">
      <h2>文件</h2>
      {files_block}
    </section>
    {readme_block}</div>
</body>
</html>
"""


def write_offline_html(
    info_dir: str | Path,
    *,
    title: str,
    platform: str,
    metadata: Mapping[str, Any] | None = None,
    files: ModFilesBundle | Sequence[ModFileEntry] | Mapping[str, Any] | None = None,
    cover: str | Path | None = None,
    description: str = "",
    index_name: str = "index.html",
) -> Path:
    """Write ``index.html`` under *info_dir* and return its path."""
    dest = Path(info_dir)
    dest.mkdir(parents=True, exist_ok=True)
    html_text = generate_offline_html(
        title=title,
        platform=platform,
        metadata=metadata,
        files=files,
        cover=cover,
        description=description,
        info_dir=dest,
    )
    index = dest / index_name
    tmp = dest / f".{index_name}.tmp"
    tmp.write_text(html_text, encoding="utf-8")
    tmp.replace(index)
    return index
