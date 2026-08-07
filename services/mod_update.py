"""Mod update detection framework (platform-pluggable)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import normalize_platform
from services.update_sources import get_update_source


@dataclass
class UpdateCheckReport:
    mod_id: str
    has_update: bool = False
    current: str = ""
    latest: str = ""
    supported: bool = True
    source: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mod_id": int(self.mod_id) if str(self.mod_id).isdigit() else self.mod_id,
            "has_update": bool(self.has_update),
            "current": self.current or "",
            "latest": self.latest or "",
            "supported": bool(self.supported),
            "source": self.source or "",
            "error": self.error or "",
        }


class ModUpdateChecker:
    """Check author versions via platform UpdateSource plugins."""

    def __init__(self, *, db: DatabaseManager | None = None) -> None:
        self._db = db

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def check_mod(
        self,
        mod_id: int | str,
        *,
        persist: bool = True,
    ) -> UpdateCheckReport:
        mid = str(mod_id).strip()
        db = self._database()
        info = db.get_mod_display_info(mid) if mid.isdigit() else None
        platform = normalize_platform(info.platform if info else "steam")
        source_url = info.source_url if info else ""
        external_id = info.external_id if info else ""
        installed = db.get_mod_version(mid).installed_version if mid.isdigit() else ""

        src = get_update_source(platform)
        result = src.check_version(
            mod_id=mid,
            source_url=source_url,
            external_id=external_id,
        )
        latest = (result.latest or "").strip()
        if persist and mid.isdigit() and result.supported and latest:
            db.update_mod_version(
                mid,
                mod_version=latest,
                version_source=result.source or platform,
                touch_checked_at=True,
            )
            # never overwrite installed_version here
            installed = db.get_mod_version(mid).installed_version
        elif persist and mid.isdigit() and not result.supported:
            db.update_mod_version(
                mid,
                version_source=result.source or platform,
                touch_checked_at=True,
            )
            installed = db.get_mod_version(mid).installed_version
        else:
            # Prefer stored author version when probe unsupported
            stored = db.get_mod_version(mid) if mid.isdigit() else None
            if stored and not latest:
                latest = stored.mod_version
            if stored:
                installed = stored.installed_version

        has_update = bool(latest and installed and latest != installed)
        return UpdateCheckReport(
            mod_id=mid,
            has_update=has_update,
            current=installed or "",
            latest=latest or "",
            supported=bool(result.supported),
            source=result.source or platform,
            error=result.error or "",
        )

    def check_all_mods(self, *, persist: bool = True) -> list[UpdateCheckReport]:
        db = self._database()
        with db._lock:
            rows = db._conn.execute("SELECT mod_id FROM mods ORDER BY mod_id").fetchall()
        reports: list[UpdateCheckReport] = []
        for row in rows:
            reports.append(self.check_mod(row["mod_id"], persist=persist))
        return reports
