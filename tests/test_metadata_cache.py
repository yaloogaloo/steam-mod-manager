"""mtime/size cache for .info/metadata.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.metadata_cache import (
    invalidate_metadata,
    load_metadata,
    metadata_cache_size,
    reset_metadata_cache,
)


def test_load_metadata_caches_until_mtime_changes(tmp_path: Path) -> None:
    reset_metadata_cache()
    folder = tmp_path / "Game" / "ModA"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {"published_file_id": "1", "title": "A"}
    meta = info / METADATA_FILENAME
    meta.write_text(json.dumps(payload), encoding="utf-8")

    first = load_metadata(folder)
    assert first == payload
    size_after_first = metadata_cache_size()
    assert size_after_first >= 1

    second = load_metadata(folder)
    assert second == payload
    assert metadata_cache_size() == size_after_first

    payload["title"] = "BBBB"
    meta.write_text(json.dumps(payload), encoding="utf-8")
    later = meta.stat().st_mtime + 2
    os.utime(meta, (later, later))
    third = load_metadata(folder)
    assert third is not None
    assert third["title"] == "BBBB"


def test_invalidate_metadata_drops_entry(tmp_path: Path) -> None:
    reset_metadata_cache()
    folder = tmp_path / "Game" / "ModB"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text("{}", encoding="utf-8")
    assert load_metadata(folder) == {}
    invalidate_metadata(folder)
    assert metadata_cache_size() == 0
