"""Canonical Identity Service — sole production identity create/bind/persist gate.

Internal ``mod_id``, platform identity, and workspace identity are distinct.
No lifecycle helper may mint an internal id or copy it into platform fields
except through this module.
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_provisional_external_id,
    normalize_platform,
    normalize_platform_if_known,
)
from core.models import is_unknown_mod_title
from services.mod_identity_authority import (
    ResolvedIdentity,
    create_mod_identity as _create_mod_identity,
    ensure_non_polluted_workspace,
    log_identity_mutation,
    resolve_mod_identity,
    safe_workspace_id_for_deploy,
    sanitize_platform_external_id,
    update_platform_identity,
)
from services.mod_identity_validator import (
    IdentityFinding,
    IdentityIssueCode,
    IdentitySeverity,
    validate_db_row_identity,
)

logger = logging.getLogger(__name__)

IDENTITY_COMPLETE = "IDENTITY_COMPLETE"
IDENTITY_PARTIAL = "IDENTITY_PARTIAL"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"

_ALLOW_INTERNAL_CREATE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "identity_internal_create", default=False
)


class IdentityCreateBypassError(RuntimeError):
    """Raised when an internal ``mod_id`` would be created outside IdentityService."""


class RepairMustNotAllocateError(RuntimeError):
    """Raised when ``allocate_mod_id`` is reached during identity repair."""


_REPAIR_NO_ALLOCATE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "identity_repair_no_allocate", default=False
)

_LIFECYCLE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "identity_lifecycle", default=""
)

_EMPTY_MOD_RE = re.compile(r"^Empty Mod [0-9a-fA-F]{8}$")

LIFECYCLE_IMPORT = "import"
LIFECYCLE_RECONCILE = "reconcile"
LIFECYCLE_REFRESH = "refresh"
LIFECYCLE_ARCHIVE = "archive"
LIFECYCLE_DEPLOY = "deploy"
LIFECYCLE_METADATA = "metadata"
LIFECYCLE_SIDECAR = "sidecar"
LIFECYCLE_REPAIR = "repair"

_ALLOCATE_FORBIDDEN = frozenset(
    {
        LIFECYCLE_REFRESH,
        LIFECYCLE_ARCHIVE,
        LIFECYCLE_DEPLOY,
        LIFECYCLE_METADATA,
        LIFECYCLE_SIDECAR,
        LIFECYCLE_REPAIR,
    }
)
_CREATE_FORBIDDEN = _ALLOCATE_FORBIDDEN


class IdentityState(str, Enum):
    COMPLETE = IDENTITY_COMPLETE
    PARTIAL = IDENTITY_PARTIAL
    UNRESOLVED = IDENTITY_UNRESOLVED
    CONFLICT = IDENTITY_CONFLICT


@dataclass
class IdentityInvariantReport:
    findings: list[IdentityFinding] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(
            1
            for f in self.findings
            if f.severity in (IdentitySeverity.CORRUPTED, IdentitySeverity.CONFLICT)
        )


def is_internal_create_allowed() -> bool:
    return bool(_ALLOW_INTERNAL_CREATE.get())


@contextmanager
def identity_create_scope() -> Iterator[None]:
    token = _ALLOW_INTERNAL_CREATE.set(True)
    try:
        yield
    finally:
        _ALLOW_INTERNAL_CREATE.reset(token)


@contextmanager
def repair_no_allocate_scope() -> Iterator[None]:
    """Repair must never mint identity. ``allocate_mod_id`` fails while active."""
    token = _REPAIR_NO_ALLOCATE.set(True)
    try:
        yield
    finally:
        _REPAIR_NO_ALLOCATE.reset(token)


def assert_repair_may_not_allocate() -> None:
    if _REPAIR_NO_ALLOCATE.get():
        raise RepairMustNotAllocateError("repair must not allocate identity")


def current_lifecycle() -> str:
    return str(_LIFECYCLE.get() or "").strip().lower()


@contextmanager
def lifecycle_scope(operation: str) -> Iterator[None]:
    """Mark the active business lifecycle. Illegal allocate/create fail loudly."""
    token = _LIFECYCLE.set(str(operation or "").strip().lower())
    try:
        yield
    finally:
        _LIFECYCLE.reset(token)


def is_empty_mod_placeholder(name: str) -> bool:
    """True for ``Empty Mod <8 hex>`` folders — never an identity-create reason."""
    return bool(_EMPTY_MOD_RE.match(str(name or "").strip()))


def has_official_platform_identity(
    *,
    platform: str = "",
    external_id: str = "",
    source_url: str = "",
    workshop_id: str = "",
) -> bool:
    """True when a create request carries a real platform identity (not stub/internal)."""
    plat = normalize_platform(platform)
    ext = str(external_id or "").strip()
    wid = str(workshop_id or "").strip()
    url = str(source_url or "").strip()
    if plat == PLATFORM_STEAM:
        cand = wid if wid.isdigit() else (ext if ext.isdigit() else "")
        if cand and not is_internal_mod_id(cand):
            return True
        return False
    if not plat:
        return False
    if ext.startswith("stub:") or ext.startswith("local/") or is_internal_mod_id(ext):
        return False
    if is_provisional_external_id(ext):
        return False
    if ext:
        return True
    if url.lower().startswith("http") and "id=900000000000" not in url.replace(" ", ""):
        return True
    return False


def assert_lifecycle_may_allocate(operation: str = "") -> None:
    assert_repair_may_not_allocate()
    op = (operation or current_lifecycle()).strip().lower()
    if op in _ALLOCATE_FORBIDDEN:
        logger.error(
            "[IDENTITY_GUARD] allocate_internal_id forbidden lifecycle=%s",
            op,
        )
        raise IdentityCreateBypassError(
            f"allocate_internal_id is forbidden in {op} scope"
        )


def assert_lifecycle_may_create(operation: str = "") -> None:
    op = (operation or current_lifecycle() or LIFECYCLE_IMPORT).strip().lower()
    if op in _CREATE_FORBIDDEN:
        logger.error("[IDENTITY_GUARD] create_mod_identity forbidden lifecycle=%s", op)
        raise IdentityCreateBypassError(
            f"create_mod_identity is forbidden in {op} scope"
        )


def refuse_unauthorized_mod_insert(mod_id: int | str) -> None:
    """Any missing-row INSERT INTO mods outside IdentityService must fail loudly."""
    mid = str(mod_id or "").strip()
    if is_internal_create_allowed():
        return
    op = current_lifecycle() or "unset"
    logger.error(
        "[IDENTITY_GUARD] unauthorized INSERT INTO mods mod_id=%s lifecycle=%s",
        mid,
        op,
    )
    raise IdentityCreateBypassError(
        f"mods row {mid} cannot be created outside IdentityService (lifecycle={op})"
    )


def assert_steam_identity_not_internal(
    *,
    mod_id: int | str = "",
    platform: str = "",
    workspace_id: str = "",
    published_file_id: str = "",
    external_id: str = "",
    source_url: str = "",
) -> None:
    """Runtime guard: internal ids must not be written as Steam identity."""
    mid = str(mod_id or "").strip()
    plat = normalize_platform(platform)
    ws = str(workspace_id or "").strip()
    pub = str(published_file_id or "").strip()
    ext = str(external_id or "").strip()
    url = str(source_url or "").strip()
    problems: list[str] = []
    if plat == PLATFORM_STEAM and is_internal_mod_id(mid):
        problems.append("steam_platform_with_internal_mod_id")
    if is_internal_mod_id(ws) and (plat == PLATFORM_STEAM or ws == mid):
        problems.append(f"workspace_id={ws}")
    if plat == PLATFORM_STEAM and is_internal_mod_id(pub):
        problems.append(f"published_file_id={pub}")
    if plat == PLATFORM_STEAM and is_internal_mod_id(ext):
        problems.append(f"external_id={ext}")
    if plat != PLATFORM_STEAM and is_internal_mod_id(ext) and ext == mid:
        problems.append("internal_written_as_steam_external")
    if url and is_internal_mod_id(mid) and f"id={mid}" in url.replace(" ", ""):
        problems.append("source_url_embeds_internal_id")
    if not problems:
        return
    logger.error(
        "[IDENTITY_GUARD] steam/internal pollution mod_id=%s problems=%s",
        mid,
        ",".join(problems),
    )
    raise IdentityCreateBypassError(
        f"illegal Steam/internal identity mix for {mid}: {', '.join(problems)}"
    )


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def classify_identity_state(
    *,
    mod_id: str = "",
    platform: str = "",
    external_id: str = "",
    workspace_id: str = "",
    source_url: str = "",
    findings: list[IdentityFinding] | None = None,
) -> str:
    """Return IDENTITY_* for a row. Unresolved is a legal state — not a guess."""
    issues = list(findings or [])
    if any(
        f.severity in (IdentitySeverity.CORRUPTED, IdentitySeverity.CONFLICT, IdentitySeverity.DUPLICATE)
        for f in issues
    ):
        return IDENTITY_CONFLICT
    mid = str(mod_id or "").strip()
    if not mid:
        return IDENTITY_UNRESOLVED
    plat = normalize_platform_if_known(platform)
    ext = sanitize_platform_external_id(plat or platform, external_id, mod_id=mid)
    url = str(source_url or "").strip()
    ws = str(workspace_id or "").strip()
    if plat == PLATFORM_STEAM:
        if is_internal_mod_id(mid):
            return IDENTITY_CONFLICT
        if ext == mid and (not url or "steamcommunity.com" in url.lower()):
            return IDENTITY_COMPLETE
        if ext or url:
            return IDENTITY_PARTIAL
        return IDENTITY_PARTIAL
    if not plat:
        return IDENTITY_UNRESOLVED if not ext and not url else IDENTITY_PARTIAL
    if ext and not is_provisional_external_id(ext):
        return IDENTITY_COMPLETE if (url or ws) else IDENTITY_PARTIAL
    if url:
        return IDENTITY_PARTIAL
    return IDENTITY_UNRESOLVED


def sidecar_published_file_id(
    *,
    mod_id: int | str,
    platform: str = "",
    external_id: str = "",
) -> str:
    """Sidecar ``published_file_id`` — never a Steam-polluted internal id."""
    mid = str(mod_id or "").strip()
    plat = normalize_platform_if_known(platform)
    ext = sanitize_platform_external_id(plat or platform, external_id, mod_id=mid)
    if is_internal_mod_id(mid) and plat == PLATFORM_STEAM:
        if ext and not is_internal_mod_id(ext):
            return ext
        return ""
    return mid


def persist_workspace_id(
    *,
    platform: str,
    mod_id: int | str,
    workspace_id: str = "",
    source_url: str = "",
    external_id: str = "",
) -> str:
    """Workspace for persistence: NULL when unresolved; never internal fallback."""
    return safe_workspace_id_for_deploy(
        platform=platform,
        workspace_id=workspace_id,
        mod_id=mod_id,
        source_url=source_url,
        external_id=external_id,
    )


def allocate_internal_id(db: Any) -> int:
    """Sole production wrapper around ``DatabaseManager.allocate_mod_id``."""
    assert_lifecycle_may_allocate()
    with identity_create_scope():
        mid = int(db.allocate_mod_id())
    log_identity_mutation(
        db,
        mod_id=str(mid),
        field_name="mod_id",
        old_value="",
        new_value=str(mid),
        source="identity_service",
        reason="allocate_internal",
    )
    return mid


def resolve_existing(
    db: Any,
    *,
    platform: str = "",
    external_id: str = "",
    source_url: str = "",
    workshop_id: str = "",
    app_id: int = 0,
    mod_id: str = "",
) -> ResolvedIdentity:
    return resolve_mod_identity(
        db,
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        workshop_id=workshop_id,
        app_id=app_id,
        mod_id=mod_id,
    )


def create_mod_identity(
    db: Any,
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
    title: str = "",
    app_id: int = 0,
    game_name: str = "",
    workshop_id: str = "",
    mod_files: Any = None,
    operation: str = "",
) -> ResolvedIdentity:
    """Create or reuse a Mod. The only supported identity-create entry."""
    op = (operation or current_lifecycle() or LIFECYCLE_IMPORT).strip().lower()
    assert_lifecycle_may_create(op)
    display = str(title or "").strip()
    official = has_official_platform_identity(
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        workshop_id=workshop_id,
    )
    if is_empty_mod_placeholder(display):
        if not official:
            logger.error(
                "[IDENTITY_GUARD] Empty Mod placeholder cannot mint identity title=%s",
                display,
            )
            raise IdentityCreateBypassError(
                f"Empty Mod placeholder cannot mint identity: {display}"
            )
        logger.warning(
            "[IDENTITY_GUARD] stripping Empty Mod placeholder title; official identity present"
        )
        title = ""
        display = ""
    if is_unknown_mod_title(display) and not official:
        logger.error(
            "[IDENTITY_GUARD] Unknown Mod cannot mint identity title=%s",
            display,
        )
        raise IdentityCreateBypassError(
            f"Unknown Mod cannot mint identity: {display}"
        )
    if not official:
        logger.error(
            "[IDENTITY_GUARD] create refused: no official platform identity "
            "platform=%s ext=%s url=%s workshop=%s",
            platform,
            external_id,
            source_url,
            workshop_id,
        )
        raise IdentityCreateBypassError(
            "create_mod_identity requires a legal official platform identity"
        )
    with identity_create_scope():
        out = _create_mod_identity(
            db,
            platform=platform,
            external_id=external_id,
            source_url=source_url,
            title=title,
            app_id=app_id,
            game_name=game_name,
            workshop_id=workshop_id,
        )
        if mod_files is not None and out.mod_id:
            db.set_mod_files(out.mod_id, mod_files)
    return out


def persist_identity(
    db: Any,
    mod_id: int | str,
    *,
    source: str = "identity_service",
    reason: str = "persist",
    correlation_id: str = "",
    **fields: Any,
) -> None:
    """Validate + sanitize + persist identity fields. Never invents a Mod."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return
    info = db.get_mod_display_info(mid)
    if info is None:
        logger.warning("persist_identity refused: no row for %s", mid)
        return

    cid = correlation_id or new_correlation_id()
    plat_in = fields.get("platform")
    plat = normalize_platform_if_known(
        plat_in if plat_in is not None else info.platform
    )
    ext_in = fields.get("external_id")
    ext = (
        sanitize_platform_external_id(
            plat or str(info.platform or ""),
            ext_in if ext_in is not None else info.external_id,
            mod_id=mid,
        )
        if ext_in is not None
        else None
    )
    url_in = fields.get("source_url")
    url = str(url_in).strip() if url_in is not None else None
    if url and is_internal_mod_id(mid) and f"id={mid}" in url.replace(" ", ""):
        url = ""
        fields = dict(fields)
        fields["source_url"] = ""

    ws_in = fields.get("workspace_id")
    if ws_in is not None:
        fields = dict(fields)
        fields["workspace_id"] = persist_workspace_id(
            platform=plat or str(info.platform or ""),
            mod_id=mid,
            workspace_id=str(ws_in or ""),
            source_url=url if url is not None else (info.source_url or ""),
            external_id=ext if ext is not None else (info.external_id or ""),
        )

    if ext is not None:
        fields = dict(fields)
        fields["external_id"] = ext

    before = {
        "platform": info.platform,
        "external_id": info.external_id,
        "source_url": info.source_url,
        "workspace_id": info.workspace_id,
        "app_id": str(info.app_id),
    }
    allowed = {
        "internal_id",
        "library_status",
        "source_type",
        "content_status",
        "last_known_path",
        "folder_present",
        "game_name",
        "title",
        "platform",
        "source_url",
        "external_id",
        "workspace_id",
        "app_id",
        "sticky_source",
    }
    patch = {k: v for k, v in fields.items() if k in allowed}
    if patch:
        db.update_mod_identity_fields(mid, **patch)
    after = db.get_mod_display_info(mid)
    if after is None:
        return
    pairs = (
        ("platform", before["platform"], after.platform),
        ("external_id", before["external_id"], after.external_id),
        ("source_url", before["source_url"], after.source_url),
        ("workspace_id", before["workspace_id"], after.workspace_id),
        ("app_id", before["app_id"], str(after.app_id)),
    )
    for field_name, old, new in pairs:
        if str(old or "") != str(new or ""):
            log_identity_mutation(
                db,
                mod_id=mid,
                field_name=field_name,
                old_value=str(old or ""),
                new_value=str(new or ""),
                source=source,
                reason=f"{reason}:{cid}",
            )
    try:
        ensure_non_polluted_workspace(db, mid)
    except Exception:  # noqa: BLE001
        logger.debug("workspace scrub after persist failed", exc_info=True)


def bind_platform(
    db: Any,
    mod_id: int | str,
    *,
    platform: str | None = None,
    external_id: str | None = None,
    source_url: str | None = None,
    app_id: int | None = None,
    title: str | None = None,
    source: str = "identity_service",
    reason: str = "bind_platform",
) -> Any:
    return update_platform_identity(
        db,
        mod_id,
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        app_id=app_id,
        title=title,
        source=source,
        reason=reason,
    )


def validate_binding(
    *,
    mod_id: int | str,
    platform: str = "",
    external_id: str = "",
    workspace_id: str = "",
    source_url: str = "",
    app_id: int = 0,
) -> list[IdentityFinding]:
    return validate_db_row_identity(
        mod_id=mod_id,
        platform=platform,
        external_id=external_id,
        workspace_id=workspace_id,
        source_url=source_url,
        app_id=app_id,
    )


def validate_identity_invariants(
    db: Any,
    *,
    library_root: str | None = None,
) -> IdentityInvariantReport:
    """
    INV-01..INV-10 snapshot against the live DB (and optional library scan).

    Read-only. Does not repair.
    """
    from services.mod_library_integrity_audit import audit_mod_library_integrity
    from core.paths import default_mod_library

    report = IdentityInvariantReport()
    root = library_root or str(default_mod_library())
    lib = audit_mod_library_integrity(root, db=db)
    report.findings.extend(lib.global_findings)
    for mod_report in lib.mod_reports:
        report.findings.extend(mod_report.findings)

    try:
        with db._lock:  # noqa: SLF001
            rows = db._conn.execute(  # noqa: SLF001
                """
                SELECT mod_id, platform, external_id, workspace_id, source_url, app_id
                FROM mods
                """
            ).fetchall()
    except Exception:  # noqa: BLE001
        return report

    for row in rows:
        mid = str(row["mod_id"] or "").strip()
        plat = str(row["platform"] or "").strip()
        ext = str(row["external_id"] or "").strip()
        ws = str(row["workspace_id"] or "").strip()
        url = str(row["source_url"] or "").strip()
        if is_internal_mod_id(mid) and ext == mid:
            report.findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.INTERNAL_ID_AS_EXTERNAL_ID,
                    severity=IdentitySeverity.CORRUPTED,
                    message="INV-02 external_id equals internal mod_id",
                    mod_id=mid,
                    platform=plat,
                    external_id=ext,
                )
            )
        if is_internal_mod_id(mid) and ws == mid:
            report.findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.WORKSPACE_ID_POLLUTION,
                    severity=IdentitySeverity.CORRUPTED,
                    message="INV-03 workspace_id equals internal mod_id",
                    mod_id=mid,
                    platform=plat,
                    actual=ws,
                )
            )
        if plat == PLATFORM_STEAM and is_internal_mod_id(mid):
            report.findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.STEAM_ID_POLLUTION,
                    severity=IdentitySeverity.CORRUPTED,
                    message="INV-01 Steam platform bound to internal mod_id",
                    mod_id=mid,
                    platform=plat,
                    source_url=url,
                )
            )
        if url and is_internal_mod_id(mid) and f"id={mid}" in url:
            report.findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.STEAM_ID_POLLUTION,
                    severity=IdentitySeverity.CORRUPTED,
                    message="source_url embeds internal mod_id as Workshop id",
                    mod_id=mid,
                    source_url=url,
                )
            )
    return report


def refuse_internal_create(mod_id: int | str) -> None:
    """Call from DB stub insert when an internal id would be minted off-gate."""
    mid = str(mod_id or "").strip()
    if not is_internal_mod_id(mid):
        return
    refuse_unauthorized_mod_insert(mid)
