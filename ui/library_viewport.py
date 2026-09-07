"""Viewport window math for Library card pooling (no full-game widget trees).

ARCHITECTURE RULE
-----------------
Column estimates must match ``FlowLayout`` packing of ``ModCardWidget`` only.
Vertical virtualization pads belong **outside** the card FlowLayout (VBox),
never as peer flow items — otherwise the first row loses one card slot.
"""

from __future__ import annotations

from dataclasses import dataclass

# Must stay aligned with ui.mod_card.CARD_WIDTH + ui.library_view flow spacing.
CARD_WIDTH = 200
CARD_H_SPACING = 8
FLOW_MARGIN = 2
CARD_SLOT_HEIGHT = 220
VIEWPORT_ROW_BUFFER = 2


@dataclass(frozen=True)
class ViewportWindow:
    first_index: int
    last_index: int  # exclusive
    top_pad: int
    bottom_pad: int
    columns: int
    total_height: int


def estimate_columns(
    viewport_width: int,
    *,
    card_width: int = CARD_WIDTH,
    h_spacing: int = CARD_H_SPACING,
    margin: int = FLOW_MARGIN,
) -> int:
    """Same packing rule as FlowLayout: how many fixed-width cards fit per row."""
    usable = max(0, int(viewport_width) - 2 * max(0, int(margin)))
    slot = max(1, int(card_width) + int(h_spacing))
    # N cards need N*card_width + (N-1)*spacing ≤ usable
    # ⇒ N ≤ (usable + spacing) / (card_width + spacing)
    return max(1, (usable + int(h_spacing)) // slot)


def estimate_total_height(
    item_count: int,
    viewport_width: int,
    *,
    card_width: int = CARD_WIDTH,
    h_spacing: int = CARD_H_SPACING,
    margin: int = FLOW_MARGIN,
    slot_height: int = CARD_SLOT_HEIGHT,
) -> int:
    cols = estimate_columns(
        viewport_width,
        card_width=card_width,
        h_spacing=h_spacing,
        margin=margin,
    )
    rows = (max(0, int(item_count)) + cols - 1) // cols if item_count else 0
    return int(rows * slot_height)


def max_scroll_y(
    item_count: int,
    viewport_width: int,
    viewport_height: int,
    *,
    card_width: int = CARD_WIDTH,
    h_spacing: int = CARD_H_SPACING,
    margin: int = FLOW_MARGIN,
    slot_height: int = CARD_SLOT_HEIGHT,
) -> int:
    """Largest legal ``scroll_y`` for ``item_count`` in the current viewport."""
    total = estimate_total_height(
        item_count,
        viewport_width,
        card_width=card_width,
        h_spacing=h_spacing,
        margin=margin,
        slot_height=slot_height,
    )
    return max(0, int(total) - max(1, int(viewport_height)))


def clamp_scroll_y(
    scroll_y: int,
    item_count: int,
    viewport_width: int,
    viewport_height: int,
    *,
    card_width: int = CARD_WIDTH,
    h_spacing: int = CARD_H_SPACING,
    margin: int = FLOW_MARGIN,
    slot_height: int = CARD_SLOT_HEIGHT,
) -> int:
    """
    Clamp a (possibly stale) scroll offset onto the new filtered list.

    A leftover offset from a longer list must not be fed into
    ``compute_viewport_window`` — that binds the tail (e.g. last 2 of 7)
    while ``count_label`` still reports the full filtered count.
    """
    ceiling = max_scroll_y(
        item_count,
        viewport_width,
        viewport_height,
        card_width=card_width,
        h_spacing=h_spacing,
        margin=margin,
        slot_height=slot_height,
    )
    return max(0, min(int(scroll_y), ceiling))


def compute_viewport_window(
    *,
    item_count: int,
    scroll_y: int,
    viewport_width: int,
    viewport_height: int,
    card_width: int = CARD_WIDTH,
    h_spacing: int = CARD_H_SPACING,
    margin: int = FLOW_MARGIN,
    slot_height: int = CARD_SLOT_HEIGHT,
    row_buffer: int = VIEWPORT_ROW_BUFFER,
) -> ViewportWindow:
    """Return index window for card widgets that must exist in the card FlowLayout.

    ``scroll_y`` is clamped to the legal range for ``item_count`` before the
    window is computed. A leftover offset from a longer list must not bind
    the tail of a short filtered set.
    """
    n = max(0, int(item_count))
    cols = estimate_columns(
        viewport_width,
        card_width=card_width,
        h_spacing=h_spacing,
        margin=margin,
    )
    total_rows = (n + cols - 1) // cols if n else 0
    total_height = total_rows * slot_height
    if n == 0:
        return ViewportWindow(0, 0, 0, 0, cols, 0)

    scroll_y = clamp_scroll_y(
        scroll_y,
        n,
        viewport_width,
        viewport_height,
        card_width=card_width,
        h_spacing=h_spacing,
        margin=margin,
        slot_height=slot_height,
    )
    first_row = max(0, int(scroll_y) // max(1, slot_height) - row_buffer)
    last_row = min(
        total_rows,
        (int(scroll_y) + max(1, int(viewport_height))) // max(1, slot_height)
        + 1
        + row_buffer,
    )
    first_index = min(n, first_row * cols)
    last_index = min(n, last_row * cols)
    top_pad = first_row * slot_height
    bottom_pad = max(0, total_height - last_row * slot_height)
    return ViewportWindow(
        first_index=first_index,
        last_index=last_index,
        top_pad=top_pad,
        bottom_pad=bottom_pad,
        columns=cols,
        total_height=total_height,
    )
