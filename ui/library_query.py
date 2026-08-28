"""Pure Mod Library search / filter / sort helpers (no network, no archive)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
)
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    normalize_conflict_status,
)

FILTER_ALL = "all"
FILTER_FAVORITE = "favorite"
FILTER_DEPLOYED = "deployed"
FILTER_OFFLINE_PRESENT = "offline_present"
FILTER_OFFLINE_MISSING = "offline_missing"
FILTER_INVALID = "invalid"
FILTER_CONFLICT = "conflict"
FILTER_DISABLED = "disabled"
# Lifecycle content_status filters (Phase 7)
FILTER_CONTENT_MISSING = "content_missing"
FILTER_FOLDER_MISSING = "folder_missing"
FILTER_BACKUP_INVALID = "backup_invalid"
FILTER_IDENTITY_CONFLICT = "identity_conflict"
# User-facing aggregate (UI only — does not change content_status)
FILTER_ANOMALY = "anomaly"
# Same rank as 全部/收藏/… — not a separate “record mode”
FILTER_DEPLOYMENT_RECORD = "deployment_record"
FILTER_PLATFORM_ALL = "platform_all"
FILTER_PLATFORM_STEAM = "platform_steam"
FILTER_PLATFORM_NEXUS = "platform_nexus"
FILTER_PLATFORM_GITHUB = "platform_github"
FILTER_PLATFORM_MODIO = "platform_modio"
FILTER_PLATFORM_LOCAL = "platform_local"
FILTER_PLATFORM_OTHER = "platform_other"
FILTER_CATEGORY_ALL = "category_all"

SORT_MTIME = "mtime"
SORT_NAME = "name"


def resolve_mod_library_title(
    *,
    metadata_display_name: str = "",
    metadata_title: str = "",
    db_display_name: str = "",
    db_steam_name: str = "",
    folder_name: str = "",
) -> str:
    """
    Library card / filter title priority (UI read only)::

        metadata.display_name
            > DB display_name
            > DB steam_name / metadata.title
            > folder name
    """
    for candidate in (
        metadata_display_name,
        db_display_name,
        db_steam_name,
        metadata_title,
        folder_name,
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return "—"


# User-facing status chips (Library toolbar). Diagnostic keys stay matchable.
STATUS_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    (FILTER_ALL, "全部"),
    (FILTER_CONTENT_MISSING, "内容缺失"),
    (FILTER_ANOMALY, "异常"),
    (FILTER_DEPLOYED, "已部署"),
    (FILTER_FAVORITE, "收藏"),
)

# Platform chips — mods.platform / store platform only (never source_type provenance).
PLATFORM_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    (FILTER_PLATFORM_ALL, "全部"),
    (FILTER_PLATFORM_STEAM, "Steam"),
    (FILTER_PLATFORM_NEXUS, "Nexus"),
    (FILTER_PLATFORM_MODIO, "Mod.io"),
    (FILTER_PLATFORM_GITHUB, "GitHub"),
    (FILTER_PLATFORM_LOCAL, "Local"),
)

# content_status values rolled into the user-facing 「异常」 chip.
# content_missing stays a dedicated chip and is excluded here.
ANOMALY_CONTENT_STATUSES: frozenset[str] = frozenset(
    {
        FILTER_IDENTITY_CONFLICT,
        FILTER_BACKUP_INVALID,
        FILTER_FOLDER_MISSING,
        "metadata_missing",
    }
)

_SOURCE_TOKEN_TO_KEY: dict[str, str] = {
    "steam": FILTER_PLATFORM_STEAM,
    "nexus": FILTER_PLATFORM_NEXUS,
    "modio": FILTER_PLATFORM_MODIO,
    "mod.io": FILTER_PLATFORM_MODIO,
    "mod_io": FILTER_PLATFORM_MODIO,
    "github": FILTER_PLATFORM_GITHUB,
    "local": FILTER_PLATFORM_LOCAL,
    "other": FILTER_PLATFORM_LOCAL,
    "manual": FILTER_PLATFORM_LOCAL,
}

# Back-compat flat list (status then platform) for older callers.
FILTER_LABELS: tuple[tuple[str, str], ...] = (
    *STATUS_FILTER_LABELS,
    *PLATFORM_FILTER_LABELS,
)

SORT_LABELS: tuple[tuple[str, str], ...] = (
    (SORT_MTIME, "最近修改"),
    (SORT_NAME, "名称"),
)


@dataclass(frozen=True)
class ModFilterIndex:
    """Cached fields used for library search / filter / sort."""

    mod_id: str
    display_name: str
    steam_name: str
    notes: str
    game_name: str
    favorite: bool
    deployed: bool
    has_offline: bool
    mtime: float
    sort_name: str
    invalid: bool = False
    conflict: bool = False
    tag_values: str = ""
    platform: str = PLATFORM_STEAM
    source_url: str = ""
    external_id: str = ""
    is_invalid: bool = False
    conflict_status: str = "none"
    enabled: bool = True
    category_tags: str = ""
    content_status: str = ""
    source_type: str = ""


def normalize_record_mod_id(raw: object) -> str:
    """Canonical mod_id for Record Context (\"001\" → \"1\")."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


@dataclass(frozen=True)
class DeploymentRecordFilterContext:
    """
    Snapshot ids for FILTER_DEPLOYMENT_RECORD (data only — not a UI mode).

    Visibility: ``recorded_mod_ids ∪ currently-deployed``.
    Relative badges are runtime-only; never written to DB.
    """

    record_id: int
    recorded_mod_ids: frozenset[str]

    @classmethod
    def create(
        cls,
        record_id: int | str,
        recorded_mod_ids,
    ) -> DeploymentRecordFilterContext:
        ids: set[str] = set()
        for raw in recorded_mod_ids or ():
            key = normalize_record_mod_id(raw)
            if key:
                ids.add(key)
        return cls(
            record_id=int(str(record_id).strip()),
            recorded_mod_ids=frozenset(ids),
        )

    def signature(self) -> tuple[int, tuple[str, ...]]:
        return (self.record_id, tuple(sorted(self.recorded_mod_ids)))


@dataclass(frozen=True)
class RecordRelativeStatus:
    """
    Record-vs-deploy comparison — only while FILTER_DEPLOYMENT_RECORD is active.

    Architecture: runtime-only. Never persist to mods / items / snapshot
    (no extra_deployed, record_missing, or relative_status columns).
    """

    recorded: bool
    deployed: bool

    @property
    def recorded_and_deployed(self) -> bool:
        return self.recorded and self.deployed

    @property
    def recorded_not_deployed(self) -> bool:
        return self.recorded and not self.deployed

    @property
    def not_recorded_deployed(self) -> bool:
        return (not self.recorded) and self.deployed


RECORD_STATUS_LABEL_MISSING = "记录缺失"
RECORD_STATUS_LABEL_EXTRA = "额外部署"


def record_relative_badge_label(status: RecordRelativeStatus | None) -> str | None:
    if status is None or status.recorded_and_deployed:
        return None
    if status.recorded_not_deployed:
        return RECORD_STATUS_LABEL_MISSING
    if status.not_recorded_deployed:
        return RECORD_STATUS_LABEL_EXTRA
    return None


def matches_record_visibility(
    index: ModFilterIndex,
    recorded_mod_ids: frozenset[str] | None,
) -> bool:
    """Visible under deployment-record filter: recorded ∪ deployed."""
    if recorded_mod_ids is None:
        return False
    mid = normalize_record_mod_id(index.mod_id)
    if mid and mid in recorded_mod_ids:
        return True
    return bool(index.deployed)


def compute_record_relative_status(
    index: ModFilterIndex,
    recorded_mod_ids: frozenset[str] | None,
) -> RecordRelativeStatus | None:
    """Overlay flags only when a record filter set is active; never load/store in DB."""
    if recorded_mod_ids is None:
        return None
    mid = normalize_record_mod_id(index.mod_id)
    return RecordRelativeStatus(
        recorded=bool(mid) and mid in recorded_mod_ids,
        deployed=bool(index.deployed),
    )


def offline_page_exists(
    managed_path: Path, *, mod_id: str | int | None = None
) -> bool:
    """True when resolver finds an offline page (``.info`` or backup)."""
    from services.mod_metadata_resolver import resolve_offline_page

    return resolve_offline_page(mod_id, managed_path) is not None


def folder_mtime(managed_path: Path) -> float:
    try:
        return float(Path(managed_path).stat().st_mtime)
    except OSError:
        return 0.0


def matches_search(index: ModFilterIndex, query: str) -> bool:
    q = (query or "").strip().casefold()
    if not q:
        return True
    haystacks = (
        index.display_name,
        index.steam_name,
        index.notes,
        index.mod_id,
        index.game_name,
        index.tag_values,
        index.category_tags,
        index.source_url,
        index.external_id,
        index.platform,
    )
    return any(q in (h or "").casefold() for h in haystacks)


def matches_status_filter(index: ModFilterIndex, filter_key: str) -> bool:
    key = filter_key or FILTER_ALL
    if key == FILTER_ALL:
        return True
    if key == FILTER_FAVORITE:
        return bool(index.favorite)
    if key == FILTER_DEPLOYED:
        return bool(index.deployed)
    if key == FILTER_OFFLINE_PRESENT:
        return bool(index.has_offline)
    if key == FILTER_OFFLINE_MISSING:
        return not bool(index.has_offline)
    if key == FILTER_INVALID:
        return bool(index.is_invalid or index.invalid)
    if key == FILTER_CONTENT_MISSING:
        return str(index.content_status or "").strip() == FILTER_CONTENT_MISSING
    if key == FILTER_FOLDER_MISSING:
        return str(index.content_status or "").strip() == FILTER_FOLDER_MISSING
    if key == FILTER_BACKUP_INVALID:
        return str(index.content_status or "").strip() == FILTER_BACKUP_INVALID
    if key == FILTER_IDENTITY_CONFLICT:
        return str(index.content_status or "").strip() == FILTER_IDENTITY_CONFLICT
    if key == FILTER_ANOMALY:
        return index_is_anomaly(index)
    if key == FILTER_CONFLICT:
        status = normalize_conflict_status(index.conflict_status)
        return bool(index.conflict) or status == CONFLICT_STATUS_CONFLICT or str(
            index.content_status or ""
        ).strip() == FILTER_IDENTITY_CONFLICT
    if key == FILTER_DISABLED:
        return not bool(index.enabled)
    # Platform keys accidentally passed as status → defer to platform matcher
    if key in (
        FILTER_PLATFORM_ALL,
        FILTER_PLATFORM_STEAM,
        FILTER_PLATFORM_NEXUS,
        FILTER_PLATFORM_GITHUB,
        FILTER_PLATFORM_MODIO,
        FILTER_PLATFORM_LOCAL,
        FILTER_PLATFORM_OTHER,
    ):
        return matches_platform_filter(index, key)
    return True


def effective_source_token(index: ModFilterIndex) -> str:
    """Store platform token for filters/chips — never sticky ``source_type``."""
    from services.platform_identity import normalize_platform, normalize_platform_if_known

    plat = str(index.platform or "").strip().lower()
    known = normalize_platform_if_known(plat)
    token = known or (normalize_platform(plat) if plat and plat != "external" else "")
    if not token:
        token = "steam"
    if token in {"mod.io", "mod_io"}:
        return "modio"
    if token in {"other", "manual"}:
        return "local"
    return token


def index_is_anomaly(index: ModFilterIndex) -> bool:
    """User-facing 「异常」: conflicts, invalid, backup/folder/metadata issues."""
    cs = str(index.content_status or "").strip()
    if cs in ANOMALY_CONTENT_STATUSES:
        return True
    if bool(index.is_invalid or index.invalid):
        return True
    status = normalize_conflict_status(index.conflict_status)
    # File conflict only — soft ``warning`` is not an anomaly badge
    if bool(index.conflict) or status == CONFLICT_STATUS_CONFLICT:
        return True
    if not bool(index.enabled):
        return True
    return False


def collect_source_keys(indexes: list[ModFilterIndex]) -> list[str]:
    """Distinct source chips for the given Mod set (stable order, no scan)."""
    seen: set[str] = set()
    extras: set[str] = set()
    known = set(_SOURCE_TOKEN_TO_KEY.values())
    for index in indexes:
        token = effective_source_token(index)
        if not token or token == "unknown":
            continue
        key = _SOURCE_TOKEN_TO_KEY.get(token)
        if key:
            seen.add(key)
        elif token not in known:
            extras.add(token)
    ordered = [
        key
        for key, _label in PLATFORM_FILTER_LABELS
        if key != FILTER_PLATFORM_ALL and key in seen
    ]
    ordered.extend(sorted(extras, key=str.casefold))
    return ordered


def collect_category_labels(indexes: list[ModFilterIndex]) -> list[str]:
    """Distinct category tags for the given Mod set (no DB / disk scan)."""
    tags: set[str] = set()
    for index in indexes:
        for part in str(index.category_tags or "").split():
            label = part.strip()
            if label:
                tags.add(label)
    return sorted(tags, key=str.casefold)


def merge_category_labels(*groups: list[str]) -> list[str]:
    """Stable unique union of game-defined types and used Mod tags."""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for raw in group or []:
            label = str(raw or "").strip()
            key = label.casefold()
            if not label or key in seen:
                continue
            seen.add(key)
            out.append(label)
    return sorted(out, key=str.casefold)


def coerce_filter_selection(current: str, available: list[str], *, all_key: str) -> str:
    """Reset to ``all_key`` when the previous selection is not in the new context."""
    key = str(current or "").strip() or all_key
    if key in (all_key, FILTER_ALL, FILTER_CATEGORY_ALL, "全部标签", "全部分类", ""):
        return all_key
    if key in available:
        return key
    return all_key


def matches_platform_filter(index: ModFilterIndex, platform_key: str) -> bool:
    key = platform_key or FILTER_PLATFORM_ALL
    if key in (FILTER_ALL, FILTER_PLATFORM_ALL, ""):
        return True
    effective = effective_source_token(index)
    legacy = {
        FILTER_PLATFORM_STEAM: "steam",
        FILTER_PLATFORM_NEXUS: "nexus",
        FILTER_PLATFORM_GITHUB: "github",
        FILTER_PLATFORM_MODIO: "modio",
        FILTER_PLATFORM_LOCAL: "local",
        FILTER_PLATFORM_OTHER: "other",
    }
    if key in legacy:
        want = legacy[key]
        if want == "modio":
            return effective in {"modio", "mod.io", "mod_io"}
        if want == "local":
            return effective in {"local", "other", "manual"}
        return effective == want
    return effective == str(key).strip().lower()


def matches_category_filter(index: ModFilterIndex, category_key: str) -> bool:
    key = (category_key or FILTER_CATEGORY_ALL).strip()
    if key in ("", FILTER_ALL, FILTER_CATEGORY_ALL, "全部标签", "全部分类"):
        return True
    tags = (index.category_tags or "").casefold().split()
    needle = key.casefold()
    return needle in tags or needle in (index.category_tags or "").casefold()


def sort_key(index: ModFilterIndex, sort_mode: str):
    mode = sort_mode or SORT_MTIME
    if mode == SORT_NAME:
        return (index.sort_name.casefold(), index.mod_id)
    return (-index.mtime, index.sort_name.casefold(), index.mod_id)


def filter_and_sort(
    entries: list[tuple[ModFilterIndex, object]],
    *,
    query: str = "",
    filter_key: str = FILTER_ALL,
    platform_key: str = FILTER_PLATFORM_ALL,
    category_key: str = FILTER_CATEGORY_ALL,
    sort_mode: str = SORT_MTIME,
    record_mod_ids: frozenset[str] | None = None,
) -> list[object]:
    """
    Filter ``(index, payload)`` pairs and return payloads in sort order.

    ``FILTER_DEPLOYMENT_RECORD`` is mutually exclusive with other status chips:
    visibility is ``recorded ∪ deployed`` (via ``record_mod_ids``). Other status
    keys use ``matches_status_filter`` only — never AND with a record set.
    Platform / category / search still AND on top.
    """
    matched: list[tuple[ModFilterIndex, object]] = []
    use_record = filter_key == FILTER_DEPLOYMENT_RECORD
    for index, payload in entries:
        if use_record:
            if not matches_record_visibility(index, record_mod_ids):
                continue
        elif not matches_status_filter(index, filter_key):
            continue
        if not matches_search(index, query):
            continue
        if not matches_platform_filter(index, platform_key):
            continue
        if not matches_category_filter(index, category_key):
            continue
        matched.append((index, payload))
    matched.sort(key=lambda pair: sort_key(pair[0], sort_mode))
    return [payload for _, payload in matched]
