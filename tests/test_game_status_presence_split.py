"""Header content counts follow content_status, not folder presence."""

from __future__ import annotations

from services.game_status import ModStatusHint, aggregate_from_hints, aggregate_game_status
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from ui.library_query import FILTER_CONTENT_MISSING, ModFilterIndex, matches_status_filter


def _summary(*, content_status: str, folder_absent: bool):
    return aggregate_game_status(
        "KingdomComeDeliverance2",
        content_statuses=[content_status],
        folder_absent_flags=[folder_absent],
    )


def test_absent_folder_with_healthy_content_is_not_content_missing() -> None:
    summary = _summary(content_status=CONTENT_HEALTHY, folder_absent=True)
    assert summary.content_missing_count == 0
    assert summary.healthy_count == 1


def test_present_folder_with_content_missing_counts() -> None:
    summary = _summary(
        content_status=CONTENT_CONTENT_MISSING, folder_absent=False
    )
    assert summary.content_missing_count == 1
    assert summary.healthy_count == 0


def test_absent_folder_with_content_missing_still_counts() -> None:
    summary = _summary(
        content_status=CONTENT_CONTENT_MISSING, folder_absent=True
    )
    assert summary.content_missing_count == 1


def test_header_count_matches_content_missing_filter() -> None:
    rows = (
        (CONTENT_HEALTHY, True),
        (CONTENT_CONTENT_MISSING, False),
        (CONTENT_HEALTHY, False),
        (CONTENT_CONTENT_MISSING, True),
    )
    hints = [
        ModStatusHint(
            game_folder="KingdomComeDeliverance2",
            content_status=content,
            folder_absent=absent,
        )
        for content, absent in rows
    ]
    summary = aggregate_from_hints("KingdomComeDeliverance2", hints)
    matched = 0
    for content, _absent in rows:
        index = ModFilterIndex(
            mod_id="1",
            display_name="M",
            steam_name="",
            notes="",
            game_name="KingdomComeDeliverance2",
            favorite=False,
            deployed=False,
            has_offline=False,
            mtime=0.0,
            sort_name="M",
            content_status=content,
        )
        if matches_status_filter(index, FILTER_CONTENT_MISSING):
            matched += 1
    assert summary.content_missing_count == matched == 2
    assert not (
        summary.content_missing_count > 0 and matched == 0
    )
