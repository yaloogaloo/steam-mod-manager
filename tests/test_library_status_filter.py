"""Library combined status + platform filters."""

from __future__ import annotations

from ui.library_query import (
    FILTER_ALL,
    FILTER_CONFLICT,
    FILTER_DEPLOYED,
    FILTER_INVALID,
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    ModFilterIndex,
    filter_and_sort,
    matches_platform_filter,
    matches_status_filter,
)


def _idx(
    *,
    mod_id: str = "1",
    platform: str = "steam",
    is_invalid: bool = False,
    conflict_status: str = "none",
    deployed: bool = False,
    favorite: bool = False,
    has_offline: bool = True,
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="Game",
        favorite=favorite,
        deployed=deployed,
        has_offline=has_offline,
        mtime=1.0,
        sort_name=f"Mod {mod_id}",
        invalid=is_invalid,
        conflict=conflict_status in ("conflict", "warning"),
        platform=platform,
        is_invalid=is_invalid,
        conflict_status=conflict_status,
    )


def test_invalid_status_filter() -> None:
    bad = _idx(mod_id="1", is_invalid=True)
    ok = _idx(mod_id="2", is_invalid=False)
    assert matches_status_filter(bad, FILTER_INVALID)
    assert not matches_status_filter(ok, FILTER_INVALID)


def test_conflict_status_filter() -> None:
    c = _idx(mod_id="1", conflict_status="conflict")
    w = _idx(mod_id="2", conflict_status="warning")
    n = _idx(mod_id="3", conflict_status="none")
    assert matches_status_filter(c, FILTER_CONFLICT)
    assert matches_status_filter(w, FILTER_CONFLICT)
    assert not matches_status_filter(n, FILTER_CONFLICT)


def test_combined_nexus_and_conflict() -> None:
    entries = [
        (_idx(mod_id="1", platform="nexus", conflict_status="conflict"), "a"),
        (_idx(mod_id="2", platform="nexus", conflict_status="none"), "b"),
        (_idx(mod_id="3", platform="steam", conflict_status="conflict"), "c"),
    ]
    out = filter_and_sort(
        entries,
        filter_key=FILTER_CONFLICT,
        platform_key=FILTER_PLATFORM_NEXUS,
    )
    assert out == ["a"]


def test_combined_steam_and_invalid() -> None:
    entries = [
        (_idx(mod_id="1", platform="steam", is_invalid=True), "a"),
        (_idx(mod_id="2", platform="nexus", is_invalid=True), "b"),
        (_idx(mod_id="3", platform="steam", is_invalid=False), "c"),
    ]
    out = filter_and_sort(
        entries,
        filter_key=FILTER_INVALID,
        platform_key=FILTER_PLATFORM_STEAM,
    )
    assert out == ["a"]


def test_deployed_with_all_platforms() -> None:
    entries = [
        (_idx(mod_id="1", deployed=True, platform="nexus"), "a"),
        (_idx(mod_id="2", deployed=False, platform="nexus"), "b"),
    ]
    out = filter_and_sort(entries, filter_key=FILTER_DEPLOYED)
    assert out == ["a"]
    assert matches_platform_filter(entries[0][0], FILTER_ALL)
