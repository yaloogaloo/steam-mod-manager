"""Game-level status aggregation from reduced Mod user status.

Aggregates only user-visible Mod signals::

  content_status ∈ {healthy, content_missing}
  (user conflict / invalid / abandoned are card-level — not game identity)

Identity facts are never aggregated into user-facing game status.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    GAME_STATUS_HEALTHY,
    GAME_STATUS_MISSING_FOLDER,
)
from services.status_authority import normalize_content_axis

OVERALL_HEALTHY = "healthy"
OVERALL_WARNING = "warning"
OVERALL_MISSING = "missing"
OVERALL_CONFLICT = "conflict"

SUPPORTED_OVERALL_STATUSES = (
    OVERALL_HEALTHY,
    OVERALL_WARNING,
    OVERALL_MISSING,
    OVERALL_CONFLICT,
)


@dataclass(frozen=True)
class ModStatusHint:
    """Lightweight Mod signal for aggregation (no disk / .info / backup I/O)."""

    game_folder: str
    content_status: str = CONTENT_HEALTHY
    identity_status: str = "ok"  # ignored for user-facing aggregates
    category: str = ""
    folder_absent: bool = False
    conflict_status: str = "none"
    invalid: bool = False


@dataclass
class GameStatusSummary:
    game_name: str
    total_mods: int = 0
    healthy_count: int = 0
    folder_missing_count: int = 0  # always 0 — deleted Mod-status dimension
    content_missing_count: int = 0
    metadata_missing_count: int = 0  # always 0 — deleted Mod-status dimension
    conflict_count: int = 0  # user conflict_status only (never identity)
    backup_only_count: int = 0  # always 0 — backup is not Mod status
    game_status: str = GAME_STATUS_HEALTHY
    overall_status: str = OVERALL_HEALTHY

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def anomaly_count(self) -> int:
        return int(self.content_missing_count + self.conflict_count)


def normalize_content_status(value: str | None) -> str:
    return normalize_content_axis(value)


def aggregate_game_status(
    game_name: str,
    *,
    content_statuses: Sequence[str] = (),
    identity_statuses: Sequence[str] = (),
    game_status: str = GAME_STATUS_HEALTHY,
    folder_absent_flags: Sequence[bool] | None = None,
    conflict_statuses: Sequence[str] = (),
) -> GameStatusSummary:
    """
    Aggregate Mod content (+ optional user conflict) into GameStatusSummary.

    ``identity_statuses`` is accepted for call-site compat but **ignored** —
    identity is not a user-facing Mod / game status.
    """
    del identity_statuses  # never user-facing
    name = str(game_name or "").strip()
    gstatus = str(game_status or "").strip() or GAME_STATUS_HEALTHY
    statuses = [normalize_content_status(s) for s in content_statuses]
    absent = list(folder_absent_flags or [])
    conflicts = [str(c or "").strip().lower() for c in conflict_statuses]

    healthy = 0
    content_missing = 0
    conflict = 0

    for i, cs in enumerate(statuses):
        is_absent = bool(absent[i]) if i < len(absent) else False
        if is_absent and cs == CONTENT_HEALTHY:
            cs = CONTENT_CONTENT_MISSING
        if i < len(conflicts) and conflicts[i] == "conflict":
            conflict += 1
        if cs == CONTENT_CONTENT_MISSING:
            content_missing += 1
        elif cs == CONTENT_HEALTHY:
            healthy += 1
        else:
            content_missing += 1

    for j in range(len(statuses), len(conflicts)):
        if conflicts[j] == "conflict":
            conflict += 1

    total = max(len(statuses), len(conflicts)) if (statuses or conflicts) else 0
    if not total:
        total = len(statuses)
    overall = _compute_overall(
        game_status=gstatus,
        conflict_count=conflict,
        content_missing_count=content_missing,
    )
    return GameStatusSummary(
        game_name=name,
        total_mods=total,
        healthy_count=healthy,
        folder_missing_count=0,
        content_missing_count=content_missing,
        metadata_missing_count=0,
        conflict_count=conflict,
        backup_only_count=0,
        game_status=gstatus,
        overall_status=overall,
    )


def aggregate_from_hints(
    game_name: str,
    hints: Iterable[ModStatusHint],
    *,
    game_status: str = GAME_STATUS_HEALTHY,
) -> GameStatusSummary:
    items = [
        h
        for h in hints
        if str(h.game_folder or "").strip() == str(game_name or "").strip()
    ]
    return aggregate_game_status(
        game_name,
        content_statuses=[h.content_status for h in items],
        game_status=game_status,
        folder_absent_flags=[bool(h.folder_absent) for h in items],
        conflict_statuses=[
            str(getattr(h, "conflict_status", "") or "none") for h in items
        ],
    )


def aggregate_category_status(
    category: str,
    hints: Iterable[ModStatusHint],
    *,
    game_folder: str = "",
) -> GameStatusSummary:
    """Aggregate mods whose primary category tag matches *category*."""
    label = str(category or "").strip()
    folder = str(game_folder or "").strip()
    matched: list[ModStatusHint] = []
    for h in hints:
        if folder and str(h.game_folder or "").strip() != folder:
            continue
        cat = str(h.category or "").strip()
        if cat == label or (cat.split()[:1] and cat.split()[0] == label):
            matched.append(h)
    return aggregate_game_status(
        label or folder,
        content_statuses=[h.content_status for h in matched],
        game_status=GAME_STATUS_HEALTHY,
        folder_absent_flags=[bool(h.folder_absent) for h in matched],
        conflict_statuses=[
            str(getattr(h, "conflict_status", "") or "none") for h in matched
        ],
    )


def _compute_overall(
    *,
    game_status: str,
    conflict_count: int,
    content_missing_count: int,
) -> str:
    if int(conflict_count) > 0:
        return OVERALL_CONFLICT
    if str(game_status or "").strip() == GAME_STATUS_MISSING_FOLDER:
        return OVERALL_MISSING
    if int(content_missing_count) > 0:
        return OVERALL_WARNING
    return OVERALL_HEALTHY


def format_status_tooltip(summary: GameStatusSummary | None) -> str:
    if summary is None:
        return ""
    lines = [f"{summary.total_mods} Mods"]
    if summary.game_status == GAME_STATUS_MISSING_FOLDER:
        lines.append("Mod目录不存在")
        lines.append("但备份数据仍存在")
    if summary.overall_status == OVERALL_HEALTHY and summary.total_mods > 0:
        lines.append("全部正常")
        return "\n".join(lines)
    if summary.overall_status == OVERALL_CONFLICT:
        lines.append("发现冲突标记")
    lines.append(f"正常: {summary.healthy_count}")
    if summary.content_missing_count:
        lines.append(f"内容缺失: {summary.content_missing_count}")
    if summary.conflict_count:
        lines.append(f"冲突: {summary.conflict_count}")
    return "\n".join(lines)


def leading_icon_for_overall(overall_status: str, *, kind: str = "game") -> str:
    """Compact tree leading mark. Healthy games stay 🎮 (no green noise)."""
    key = str(overall_status or "").strip() or OVERALL_HEALTHY
    if kind == "category":
        return "📁"
    if key == OVERALL_CONFLICT:
        return "❌"
    if key in {OVERALL_WARNING, OVERALL_MISSING}:
        return "⚠"
    return "🎮"


def header_status_line(summary: GameStatusSummary | None) -> str:
    """One-line / compact multi-part status for the Library page header."""
    if summary is None or summary.total_mods <= 0:
        return ""
    if summary.overall_status == OVERALL_HEALTHY:
        return "状态: 全部正常"
    if summary.overall_status == OVERALL_CONFLICT:
        return f"状态: ❌ {summary.conflict_count} 个冲突"
    parts: list[str] = [f"正常 {summary.healthy_count}"]
    if summary.content_missing_count:
        parts.append(f"内容缺失 {summary.content_missing_count}")
    if summary.overall_status == OVERALL_MISSING:
        return "状态: ⚠ 游戏目录缺失 · " + " · ".join(parts)
    return "状态: ⚠ " + " · ".join(parts)
