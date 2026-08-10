"""Library-wide deploy-target conflict detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.db_manager import (
    RELATIONSHIP_CONFLICT,
    DatabaseManager,
    get_db,
)
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
)
from services.deploy_rules.manifest import load_manifest
from services.file_ops import ModFileManager


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


class ConflictType(str, Enum):
    FILE_OVERWRITE = "FILE_OVERWRITE"
    PAK_OVERLAP = "PAK_OVERLAP"
    RELATIONSHIP = "RELATIONSHIP"
    UNKNOWN = "UNKNOWN"


@dataclass
class ConflictEntry:
    file: str
    mods: list[str] = field(default_factory=list)
    conflict_type: str = ConflictType.FILE_OVERWRITE.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "mods": list(self.mods),
            "type": self.conflict_type,
        }


@dataclass
class ConflictReport:
    status: str = CONFLICT_STATUS_NONE
    conflicts: list[ConflictEntry] = field(default_factory=list)
    mod_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "mod_id": self.mod_id,
        }


class ConflictDetector:
    """
    Detect deploy *target* path issues via ``.info/deploy_manifest.json``.

    - FILE_OVERWRITE: identical target claimed by multiple enabled Mods → conflict
    - PAK_OVERLAP: different ``.pak`` files in the same directory → warning
    Disabled Mods are excluded from detection.
    """

    def __init__(
        self,
        library_root: str | Path,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        self.library_root = Path(library_root).expanduser().resolve()
        self._db = db
        self.files = ModFileManager(self.library_root)

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def _is_enabled(self, mid: str) -> bool:
        if not mid.isdigit():
            return True
        try:
            return bool(self._database().is_mod_enabled(mid))
        except Exception:  # noqa: BLE001
            return True

    def _iter_manifest_targets(self) -> list[tuple[str, str]]:
        """List of (mod_id, normalized_target) for enabled Mods only."""
        pairs: list[tuple[str, str]] = []
        for folder in self.files.list_managed_mods():
            manifest = load_manifest(folder)
            if manifest is None:
                continue
            mid = str(manifest.mod_id or "").strip()
            if not mid:
                meta = self.files.load_metadata(folder)
                mid = str(meta.published_file_id or "") if meta else ""
            if not mid or not self._is_enabled(mid):
                continue
            for entry in manifest.files:
                if not entry.target:
                    continue
                pairs.append((mid, _norm(entry.target)))
        return pairs

    def _collect_target_owners(self) -> dict[str, list[str]]:
        """Map normalized target path → list of mod_ids (order preserved)."""
        owners: dict[str, list[str]] = {}
        for mid, key in self._iter_manifest_targets():
            bucket = owners.setdefault(key, [])
            if mid not in bucket:
                bucket.append(mid)
        return owners

    def check_all_mods(self, *, persist: bool = True) -> dict[str, ConflictReport]:
        owners = self._collect_target_owners()
        per_mod: dict[str, list[ConflictEntry]] = {}
        all_manifest_mods: set[str] = set()

        # Type 1 — exact target overwrite
        for target, mods in owners.items():
            for mid in mods:
                all_manifest_mods.add(mid)
            if len(mods) < 2:
                continue
            entry = ConflictEntry(
                file=target,
                mods=list(mods),
                conflict_type=ConflictType.FILE_OVERWRITE.value,
            )
            for mid in mods:
                per_mod.setdefault(mid, []).append(entry)

        # Type 2 — same directory, different .pak files (warning)
        dir_paks: dict[str, dict[str, set[str]]] = {}
        for mid, target in self._iter_manifest_targets():
            path = Path(target)
            if path.suffix.lower() != ".pak":
                continue
            parent = _norm(path.parent)
            bucket = dir_paks.setdefault(parent, {})
            bucket.setdefault(path.name.lower(), set()).add(mid)

        for parent, name_map in dir_paks.items():
            if len(name_map) < 2:
                continue
            # Multiple distinct pak names in one folder → warning (not hard conflict)
            mods_in_dir: list[str] = []
            for mids in name_map.values():
                for m in sorted(mids):
                    if m not in mods_in_dir:
                        mods_in_dir.append(m)
            if len(mods_in_dir) < 2:
                continue
            entry = ConflictEntry(
                file=parent,
                mods=mods_in_dir,
                conflict_type=ConflictType.PAK_OVERLAP.value,
            )
            for mid in mods_in_dir:
                all_manifest_mods.add(mid)
                # Avoid duplicating if already has FILE_OVERWRITE on same parent
                existing = per_mod.setdefault(mid, [])
                if any(
                    e.conflict_type == ConflictType.PAK_OVERLAP.value
                    and e.file == parent
                    for e in existing
                ):
                    continue
                existing.append(entry)

        # Type 3 — user-declared conflict relationships (warn / conflict note)
        try:
            with self._database()._lock:
                rel_rows = self._database()._conn.execute(
                    """
                    SELECT source_mod_id, target_mod_id
                    FROM mod_relationships
                    WHERE relationship_type = ?
                    """,
                    (RELATIONSHIP_CONFLICT,),
                ).fetchall()
        except Exception:  # noqa: BLE001
            rel_rows = []
        for row in rel_rows:
            src = str(row["source_mod_id"])
            tgt = str(row["target_mod_id"])
            if not self._is_enabled(src):
                continue
            entry = ConflictEntry(
                file=f"relationship:{src}->{tgt}",
                mods=[src, tgt],
                conflict_type=ConflictType.RELATIONSHIP.value,
            )
            all_manifest_mods.add(src)
            existing = per_mod.setdefault(src, [])
            if not any(
                e.conflict_type == ConflictType.RELATIONSHIP.value
                and e.file == entry.file
                for e in existing
            ):
                existing.append(entry)

        reports: dict[str, ConflictReport] = {}
        for mid in sorted(all_manifest_mods):
            conflicts = per_mod.get(mid) or []
            hard = [
                c
                for c in conflicts
                if c.conflict_type
                in (
                    ConflictType.FILE_OVERWRITE.value,
                    ConflictType.RELATIONSHIP.value,
                )
            ]
            soft = [
                c
                for c in conflicts
                if c.conflict_type == ConflictType.PAK_OVERLAP.value
            ]
            if hard:
                status = CONFLICT_STATUS_CONFLICT
                note = self._summarize(hard)
            elif soft:
                status = CONFLICT_STATUS_WARNING
                note = self._summarize(soft, label="同目录 pak")
            else:
                status = CONFLICT_STATUS_NONE
                note = ""
            reports[mid] = ConflictReport(
                status=status, conflicts=conflicts, mod_id=mid
            )
            if persist and mid.isdigit():
                self._database().update_mod_status(
                    mid,
                    conflict_status=status,
                    conflict_note=note,
                    touch_check_time=True,
                )
        return reports

    def check_mod(self, mod_id: int | str, *, persist: bool = True) -> ConflictReport:
        mid = str(mod_id).strip()
        if mid.isdigit() and not self._is_enabled(mid):
            report = ConflictReport(
                status=CONFLICT_STATUS_NONE, conflicts=[], mod_id=mid
            )
            if persist:
                self._database().update_mod_status(
                    mid,
                    conflict_status=CONFLICT_STATUS_NONE,
                    conflict_note="",
                    touch_check_time=True,
                )
            return report
        all_reports = self.check_all_mods(persist=persist)
        if mid in all_reports:
            return all_reports[mid]
        report = ConflictReport(status=CONFLICT_STATUS_NONE, conflicts=[], mod_id=mid)
        if persist and mid.isdigit():
            self._database().update_mod_status(
                mid,
                conflict_status=CONFLICT_STATUS_NONE,
                conflict_note="",
                touch_check_time=True,
            )
        return report

    def preview_targets(
        self,
        mod_id: int | str,
        planned_targets: list[str | Path],
    ) -> ConflictReport:
        mid = str(mod_id).strip()
        owners = self._collect_target_owners()
        conflicts: list[ConflictEntry] = []
        for raw in planned_targets:
            key = _norm(raw)
            others = [m for m in (owners.get(key) or []) if m != mid]
            if not others:
                continue
            conflicts.append(
                ConflictEntry(
                    file=key,
                    mods=[mid, *others],
                    conflict_type=ConflictType.FILE_OVERWRITE.value,
                )
            )
        status = CONFLICT_STATUS_WARNING if conflicts else CONFLICT_STATUS_NONE
        return ConflictReport(status=status, conflicts=conflicts, mod_id=mid)

    @staticmethod
    def _summarize(
        conflicts: list[ConflictEntry],
        *,
        label: str = "",
    ) -> str:
        if not conflicts:
            return ""
        sample = conflicts[0]
        names = ", ".join(sample.mods[:4])
        base = Path(sample.file).name or sample.file
        prefix = f"{label} " if label else ""
        extra = f" 等 {len(conflicts)} 处" if len(conflicts) > 1 else ""
        return f"{prefix}{base} ← {names}{extra}"
