"""Pure, side-effect-free Mod identity validation.

Detects pollution, mismatches, and duplicate identity signals across DB rows,
metadata sidecars, and filesystem paths. Never mutates data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_modio_api_mod_id,
    is_modio_external_id_pollution,
    is_provisional_external_id,
    normalize_platform,
)
from services.importers.duplicate_check import normalize_source_url


class IdentitySeverity(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CONFLICT = "conflict"
    CORRUPTED = "corrupted"
    DUPLICATE = "duplicate"
    ORPHAN = "orphan"


class IdentityIssueCode(str, Enum):
    INTERNAL_ID_AS_EXTERNAL_ID = "INTERNAL_ID_AS_EXTERNAL_ID"
    INTERNAL_ID_AS_PUBLISHED_FILE_ID_POLLUTION = "INTERNAL_ID_AS_PUBLISHED_FILE_ID_POLLUTION"
    INVALID_PLATFORM_ID = "INVALID_PLATFORM_ID"
    INVALID_APP_ID = "INVALID_APP_ID"
    MODIO_ID_POLLUTION = "MODIO_ID_POLLUTION"
    STEAM_ID_POLLUTION = "STEAM_ID_POLLUTION"
    PROVISIONAL_EXTERNAL_ID = "PROVISIONAL_EXTERNAL_ID"
    MISSING_PLATFORM_ID = "MISSING_PLATFORM_ID"
    URL_ID_MISMATCH = "URL_ID_MISMATCH"
    DB_METADATA_ID_MISMATCH = "DB_METADATA_ID_MISMATCH"
    DUPLICATE_PLATFORM_ID = "DUPLICATE_PLATFORM_ID"
    DUPLICATE_SOURCE_URL = "DUPLICATE_SOURCE_URL"
    DUPLICATE_INTERNAL_ID = "DUPLICATE_INTERNAL_ID"
    DUPLICATE_DIRECTORY_IDENTITY = "DUPLICATE_DIRECTORY_IDENTITY"
    PLATFORM_URL_MISMATCH = "PLATFORM_URL_MISMATCH"
    WORKSPACE_ID_POLLUTION = "WORKSPACE_ID_POLLUTION"


@dataclass(frozen=True)
class IdentityFinding:
    code: IdentityIssueCode
    severity: IdentitySeverity
    message: str
    mod_id: str = ""
    folder: str = ""
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    expected: str = ""
    actual: str = ""


@dataclass
class ModIdentityReport:
    mod_id: str = ""
    folder: str = ""
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    app_id: int = 0
    findings: list[IdentityFinding] = field(default_factory=list)

    @property
    def worst_severity(self) -> IdentitySeverity:
        order = (
            IdentitySeverity.HEALTHY,
            IdentitySeverity.WARNING,
            IdentitySeverity.ORPHAN,
            IdentitySeverity.DUPLICATE,
            IdentitySeverity.CONFLICT,
            IdentitySeverity.CORRUPTED,
        )
        worst = IdentitySeverity.HEALTHY
        for f in self.findings:
            if order.index(f.severity) > order.index(worst):
                worst = f.severity
        return worst


def _text(value: Any) -> str:
    return str(value or "").strip()


def _row_get(row: Mapping[str, Any] | Any, key: str) -> str:
    if row is None:
        return ""
    if isinstance(row, Mapping):
        return _text(row.get(key))
    return _text(getattr(row, key, ""))


def validate_db_row_identity(
    *,
    mod_id: int | str,
    platform: str = "",
    external_id: str = "",
    workspace_id: str = "",
    source_url: str = "",
    app_id: int = 0,
) -> list[IdentityFinding]:
    """Validate one SQLite ``mods`` row (read-only)."""
    findings: list[IdentityFinding] = []
    mid = _text(mod_id)
    plat = normalize_platform(platform)
    ext = _text(external_id)
    ws = _text(workspace_id)
    url = normalize_source_url(source_url)
    aid = int(app_id or 0)

    if mid and ext and ext == mid and is_internal_mod_id(mid):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.INTERNAL_ID_AS_EXTERNAL_ID,
                severity=IdentitySeverity.CORRUPTED,
                message="external_id equals internal mod_id",
                mod_id=mid,
                platform=plat,
                external_id=ext,
                source_url=url,
                expected="platform external id",
                actual=ext,
            )
        )

    if ws and mid and ws == mid and is_internal_mod_id(mid) and plat != PLATFORM_STEAM:
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.WORKSPACE_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="workspace_id equals internal mod_id on non-Steam mod",
                mod_id=mid,
                platform=plat,
                external_id=ext,
                actual=ws,
            )
        )

    if ext.startswith("stub:"):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.PROVISIONAL_EXTERNAL_ID,
                severity=IdentitySeverity.WARNING,
                message="provisional stub external_id",
                mod_id=mid,
                platform=plat,
                external_id=ext,
            )
        )

    if plat == PLATFORM_MODIO and ext and is_modio_external_id_pollution(ext, mod_id=mid):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.MODIO_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="Mod.io external_id polluted with internal id",
                mod_id=mid,
                platform=plat,
                external_id=ext,
            )
        )

    if plat == PLATFORM_STEAM and mid and is_internal_mod_id(mid):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.STEAM_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="Steam platform on internal mod_id range",
                mod_id=mid,
                platform=plat,
            )
        )

    if plat == PLATFORM_STEAM and ext and is_internal_mod_id(ext):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.STEAM_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="Steam external_id in internal id range",
                mod_id=mid,
                platform=plat,
                external_id=ext,
            )
        )

    if plat == PLATFORM_MODIO and url and "mod.io" in url.lower():
        if aid == 0:
            findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.INVALID_APP_ID,
                    severity=IdentitySeverity.WARNING,
                    message="Mod.io row has app_id=0 but mod.io source_url present",
                    mod_id=mid,
                    platform=plat,
                    source_url=url,
                    actual="0",
                )
            )
        if not ext or is_provisional_external_id(ext):
            if not is_modio_api_mod_id(ext):
                findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.MISSING_PLATFORM_ID,
                        severity=IdentitySeverity.WARNING,
                        message="Mod.io mod missing real external_id",
                        mod_id=mid,
                        platform=plat,
                        source_url=url,
                    )
                )

    if plat == PLATFORM_STEAM and mid and not is_internal_mod_id(mid):
        if url and "steamcommunity.com" not in url.lower() and url:
            if "mod.io" in url.lower() or "nexusmods.com" in url.lower():
                findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.PLATFORM_URL_MISMATCH,
                        severity=IdentitySeverity.CONFLICT,
                        message="Steam platform with non-Steam source_url",
                        mod_id=mid,
                        platform=plat,
                        source_url=url,
                    )
                )

    return findings


def validate_metadata_identity(
    *,
    mod_id: int | str,
    metadata: Mapping[str, Any] | None,
    db_platform: str = "",
    db_external_id: str = "",
    db_source_url: str = "",
) -> list[IdentityFinding]:
    """Cross-check ``.info/metadata.json`` against expected DB identity."""
    findings: list[IdentityFinding] = []
    mid = _text(mod_id)
    meta = dict(metadata or {})
    pub = _text(meta.get("published_file_id"))
    plat = normalize_platform(
        _text(meta.get("source_type") or meta.get("platform") or db_platform)
    )
    ext = _text(meta.get("external_id"))
    url = normalize_source_url(
        _text(meta.get("url") or meta.get("source_url") or db_source_url)
    )
    modio_mod = _text(meta.get("modio_mod_id"))
    modio_game = _text(meta.get("modio_game_id"))

    if pub and mid and pub != mid:
        if is_internal_mod_id(mid) and pub.isdigit() and int(pub) != int(mid):
            # published_file_id should match internal id for non-Steam
            if is_internal_mod_id(pub):
                pass  # both internal — unusual but not auto-fail
            else:
                findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.DB_METADATA_ID_MISMATCH,
                        severity=IdentitySeverity.CONFLICT,
                        message="metadata published_file_id differs from mod_id",
                        mod_id=mid,
                        platform=plat,
                        expected=mid,
                        actual=pub,
                    )
                )

    if ext and is_modio_external_id_pollution(ext, mod_id=mid):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.MODIO_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="metadata external_id polluted",
                mod_id=mid,
                platform=plat,
                external_id=ext,
            )
        )

    if modio_mod and is_internal_mod_id(modio_mod):
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.MODIO_ID_POLLUTION,
                severity=IdentitySeverity.CORRUPTED,
                message="metadata modio_mod_id is internal id",
                mod_id=mid,
                platform=plat,
                external_id=modio_mod,
            )
        )

    db_ext = _text(db_external_id)
    if db_ext and modio_mod and not is_provisional_external_id(db_ext):
        if is_modio_api_mod_id(db_ext) and is_modio_api_mod_id(modio_mod):
            if db_ext != modio_mod:
                findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.DB_METADATA_ID_MISMATCH,
                        severity=IdentitySeverity.CONFLICT,
                        message="DB external_id vs metadata modio_mod_id mismatch",
                        mod_id=mid,
                        platform=plat,
                        expected=db_ext,
                        actual=modio_mod,
                    )
                )

    if plat == PLATFORM_MODIO and url and modio_mod:
        if is_modio_api_mod_id(modio_mod) and "mod.io" in url:
            # Slug URLs don't embed numeric id — skip strict URL/id match
            pass

    if db_ext and ext and db_ext != ext:
        if not is_provisional_external_id(db_ext) and not is_provisional_external_id(ext):
            findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.DB_METADATA_ID_MISMATCH,
                    severity=IdentitySeverity.CONFLICT,
                    message="DB external_id vs metadata external_id mismatch",
                    mod_id=mid,
                    platform=plat,
                    expected=db_ext,
                    actual=ext,
                )
            )

    meta_url = normalize_source_url(_text(meta.get("url") or meta.get("source_url")))
    db_url = normalize_source_url(db_source_url)
    if meta_url and db_url and meta_url != db_url:
        findings.append(
            IdentityFinding(
                code=IdentityIssueCode.DB_METADATA_ID_MISMATCH,
                severity=IdentitySeverity.WARNING,
                message="DB source_url vs metadata source_url mismatch",
                mod_id=mid,
                source_url=db_url,
                expected=db_url,
                actual=meta_url,
            )
        )

    _ = modio_game  # reserved for future game_id cross-check
    return findings


def validate_mod_identity(
    *,
    mod_id: int | str,
    folder: str | Path | None = None,
    db_row: Mapping[str, Any] | Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ModIdentityReport:
    """Aggregate validation for one Mod (DB + optional metadata + folder path)."""
    mid = _text(mod_id)
    report = ModIdentityReport(mod_id=mid, folder=_text(folder))

    platform = _row_get(db_row, "platform") or _text(
        (metadata or {}).get("source_type") or (metadata or {}).get("platform")
    )
    external_id = _row_get(db_row, "external_id") or _text((metadata or {}).get("external_id"))
    source_url = _row_get(db_row, "source_url") or _text(
        (metadata or {}).get("url") or (metadata or {}).get("source_url")
    )
    try:
        app_id = int(_row_get(db_row, "app_id") or (metadata or {}).get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0

    report.platform = normalize_platform(platform)
    report.external_id = external_id
    report.source_url = source_url
    report.app_id = app_id

    ws = _row_get(db_row, "workspace_id")
    report.findings.extend(
        validate_db_row_identity(
            mod_id=mid,
            platform=platform,
            external_id=external_id,
            workspace_id=ws,
            source_url=source_url,
            app_id=app_id,
        )
    )
    report.findings.extend(
        validate_metadata_identity(
            mod_id=mid,
            metadata=metadata,
            db_platform=platform,
            db_external_id=external_id,
            db_source_url=source_url,
        )
    )
    return report


def classify_identity_confidence(
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
    mod_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """
    Return identity match confidence: ``strong``, ``weak``, or ``conflict``.

    Used to gate auto-recovery vs manual review.
    """
    plat = normalize_platform(platform)
    ext = _text(external_id)
    url = normalize_source_url(source_url)
    meta = dict(metadata or {})

    if is_provisional_external_id(ext) or is_modio_external_id_pollution(ext, mod_id=mod_id):
        if url and plat:
            return "strong" if url else "weak"
        return "weak"

    if plat == PLATFORM_MODIO:
        if is_modio_api_mod_id(ext) or _text(meta.get("modio_mod_id")):
            return "strong"
        if url and "mod.io" in url.lower():
            return "strong"
        return "weak"

    if plat == PLATFORM_STEAM and ext.isdigit() and not is_internal_mod_id(ext):
        return "strong"

    if url and plat:
        return "strong"

    if ext and plat:
        return "weak"

    return "conflict"
