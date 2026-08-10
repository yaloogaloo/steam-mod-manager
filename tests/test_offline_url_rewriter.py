"""Offline HTML URL rewriter for file:// viewing."""

from __future__ import annotations

from services.offline.url_rewriter import rewrite_external_urls, rewrite_url


BASE = "https://github.com/UE4SS-RE/RE-UE4SS"


def test_root_relative_anchor() -> None:
    html = '<html><body><a href="/abc">x</a></body></html>'
    out = rewrite_external_urls(html, BASE)
    assert 'href="https://github.com/abc"' in out


def test_dot_relative_issues() -> None:
    html = '<html><body><a href="./issues">Issues</a></body></html>'
    out = rewrite_external_urls(html, BASE)
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/issues"' in out


def test_absolute_https_kept() -> None:
    html = '<html><body><a href="https://github.com/test">t</a></body></html>'
    out = rewrite_external_urls(html, BASE)
    assert 'href="https://github.com/test"' in out


def test_javascript_kept() -> None:
    html = '<html><body><a href="javascript:void(0)">x</a></body></html>'
    out = rewrite_external_urls(html, BASE)
    assert 'href="javascript:void(0)"' in out


def test_root_relative_img() -> None:
    html = '<html><body><img src="/avatar.png"></body></html>'
    out = rewrite_external_urls(html, BASE)
    assert 'src="https://github.com/avatar.png"' in out


def test_releases_path_becomes_absolute() -> None:
    html = (
        '<html><body>'
        '<a href="/UE4SS-RE/RE-UE4SS/releases">Releases</a>'
        '<a href="/UE4SS-RE/RE-UE4SS/issues">Issues</a>'
        '<a href="/UE4SS-RE/RE-UE4SS/pulls">Pull requests</a>'
        '<a href="/UE4SS-RE/RE-UE4SS/actions">Actions</a>'
        "</body></html>"
    )
    out = rewrite_external_urls(html, BASE)
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/releases"' in out
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/issues"' in out
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/pulls"' in out
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/actions"' in out
    assert "file://" not in out


def test_local_assets_kept() -> None:
    html = (
        '<html><head>'
        '<link rel="stylesheet" href="./assets/style.css">'
        "</head><body>"
        '<img src="assets/images/a.png">'
        "</body></html>"
    )
    out = rewrite_external_urls(html, BASE)
    assert 'href="./assets/style.css"' in out
    assert 'src="assets/images/a.png"' in out


def test_mailto_and_https_stylesheet_kept() -> None:
    html = (
        '<html><head>'
        '<link rel="stylesheet" href="https://github.githubassets.com/x.css">'
        "</head><body>"
        '<a href="mailto:a@b.com">mail</a>'
        '<form action="/search"></form>'
        "</body></html>"
    )
    out = rewrite_external_urls(html, BASE)
    assert "https://github.githubassets.com/x.css" in out
    assert 'href="mailto:a@b.com"' in out
    assert 'action="https://github.com/search"' in out


def test_rewrite_url_unit() -> None:
    assert rewrite_url("/abc", BASE) == "https://github.com/abc"
    assert rewrite_url("./issues", BASE) == "https://github.com/UE4SS-RE/RE-UE4SS/issues"
    assert rewrite_url("https://github.com/test", BASE) == "https://github.com/test"
    assert rewrite_url("javascript:void(0)", BASE) == "javascript:void(0)"
