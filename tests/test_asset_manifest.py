"""Asset Manifest contract tests (Phase 1 — not production-wired)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.asset_manifest import (
    AssetManifest,
    AssetReference,
    ManifestError,
    build_manifest,
    validate_manifest_path,
)
from services.asset_store import sha256_bytes


DIGEST = sha256_bytes(b"manifest-fixture")


def test_validate_relative_path_ok() -> None:
    assert validate_manifest_path("assets/foo.png") == "assets/foo.png"
    assert validate_manifest_path("assets\\bar.jpg") == "assets/bar.jpg"


@pytest.mark.parametrize(
    "bad",
    [
        "../secret",
        "assets/../../etc/passwd",
        "/etc/passwd",
        "C:\\secret",
        "C:/secret",
        "\\\\server\\share",
        "//server/share",
        "",
        "..",
    ],
)
def test_validate_path_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ManifestError):
        validate_manifest_path(bad)


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = build_manifest(
        [
            AssetReference(path="assets/a.png", sha256=DIGEST, size=12),
            AssetReference(
                path="assets/b.css",
                sha256=DIGEST,
                size=34,
                media_type="text/css",
            ),
        ]
    )
    path = tmp_path / "manifest.json"
    manifest.write_json(path)
    loaded = AssetManifest.from_path(path)
    assert loaded.schema_version == 1
    assert len(loaded.assets) == 2
    assert loaded.assets[0].path == "assets/a.png"
    assert loaded.assets[1].media_type == "text/css"
    assert DIGEST in loaded.sha256_set()


def test_manifest_rejects_duplicate_path() -> None:
    with pytest.raises(ManifestError):
        build_manifest(
            [
                AssetReference(path="assets/a.png", sha256=DIGEST, size=1),
                AssetReference(path="assets/a.png", sha256=DIGEST, size=2),
            ]
        )


def test_manifest_rejects_bad_sha() -> None:
    with pytest.raises(Exception):
        AssetReference.from_dict(
            {"path": "assets/x.png", "sha256": "nope", "size": 1}
        )


def test_manifest_json_shape() -> None:
    m = AssetManifest(
        assets=[AssetReference(path="assets/x.png", sha256=DIGEST, size=9)]
    )
    data = json.loads(m.to_json())
    assert data["schema_version"] == 1
    assert data["assets"][0]["path"] == "assets/x.png"
    assert "mod_id" not in data
    assert "workspace_id" not in data
    assert "internal_id" not in data


def test_manifest_not_coupled_to_production_writers() -> None:
    """Guard: manifest module must not call archive/backup/offline writers."""
    import services.asset_manifest as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "services.archive",
        "asset_cache",
        "backup_closure",
        "metadata_backup",
        "OfflinePageArchiver",
    ):
        assert forbidden not in src
