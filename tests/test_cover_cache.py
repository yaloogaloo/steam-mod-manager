"""Cover LRU stores copies and evicts past 300 entries."""

from __future__ import annotations

from pathlib import Path

from services.cover_cache import (
    MAX_COVER_CACHE,
    cover_cache_size,
    get_cover_image,
    put_cover_image,
    reset_cover_cache,
)


class _FakeImage:
    def __init__(self, token: int) -> None:
        self.token = token

    def isNull(self) -> bool:
        return False

    def copy(self) -> "_FakeImage":
        return _FakeImage(self.token)


def test_cover_cache_hit_and_lru_eviction(tmp_path: Path) -> None:
    reset_cover_cache()
    files = []
    for i in range(MAX_COVER_CACHE + 5):
        path = tmp_path / f"c{i}.png"
        path.write_bytes(b"x")
        files.append(path)
        put_cover_image(path, 160, 90, _FakeImage(i))

    assert cover_cache_size() == MAX_COVER_CACHE
    assert get_cover_image(files[0], 160, 90) is None
    hit = get_cover_image(files[-1], 160, 90)
    assert hit is not None
    assert hit.token == MAX_COVER_CACHE + 4
