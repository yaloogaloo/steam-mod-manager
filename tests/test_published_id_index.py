"""published_id → path index is built once per manager."""

from __future__ import annotations

import json
from pathlib import Path

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager


def test_index_by_published_id_is_cached(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    for mid, name in (("91001", "A"), ("91002", "B")):
        folder = lib / "Game" / name
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps({"published_file_id": mid, "title": name}),
            encoding="utf-8",
        )

    manager = ModFileManager(lib)
    first = manager.index_by_published_id()
    second = manager.index_by_published_id()
    assert first is second
    assert manager.find_by_published_id("91001") == first["91001"]
    assert manager.find_by_published_id("91002").name == "B"
