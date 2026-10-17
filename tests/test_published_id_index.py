"""``.info/internal_id`` → path index is built once per manager.

``index_by_published_id`` is a deprecated alias of ``index_by_internal_id`` —
it indexes Entity ``internal_id``, never Steam ``published_file_id``.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager


def test_index_by_published_id_is_cached(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    for iid, name in (
        ("aaaaaaaa-bbbb-cccc-dddd-000000009101", "A"),
        ("aaaaaaaa-bbbb-cccc-dddd-000000009102", "B"),
    ):
        folder = lib / "Game" / name
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "internal_id": iid,
                    "published_file_id": "ws-" + name,
                    "title": name,
                }
            ),
            encoding="utf-8",
        )

    manager = ModFileManager(lib)
    first = manager.index_by_published_id()
    second = manager.index_by_published_id()
    assert first is second
    key_a = "aaaaaaaa-bbbb-cccc-dddd-000000009101"
    assert manager.find_by_published_id(key_a) == first[key_a]
    assert manager.find_by_internal_id(key_a).name == "A"
    assert (
        manager.find_by_internal_id("aaaaaaaa-bbbb-cccc-dddd-000000009102").name
        == "B"
    )
