"""Directory size skips offline/assets/.cache and caches by root mtime."""

from __future__ import annotations

from pathlib import Path

from services.dir_size import directory_size, reset_directory_size_cache


def test_directory_size_skips_offline_assets_and_cache(tmp_path: Path) -> None:
    reset_directory_size_cache()
    root = tmp_path / "Mod"
    info = root / ".info"
    (info / "offline" / "assets").mkdir(parents=True)
    (info / "assets").mkdir(parents=True)
    (root / ".cache").mkdir()
    (root / "payload.bin").write_bytes(b"x" * 100)
    (info / "metadata.json").write_bytes(b"y" * 20)
    (info / "offline" / "index.html").write_bytes(b"z" * 5000)
    (info / "offline" / "assets" / "big.png").write_bytes(b"z" * 8000)
    (info / "assets" / "cover.png").write_bytes(b"z" * 3000)
    (root / ".cache" / "tmp").write_bytes(b"z" * 2000)

    total = directory_size(root)
    assert total == 120
    assert directory_size(root) == 120
