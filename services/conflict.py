"""Library-wide deploy-target conflict detection."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
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
)
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME, ModFileManager

logger = logging.getLogger(__name__)

CONFLICT_TRACE_FILENAME = "conflict_trace.json"


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


class ConflictClass(str, Enum):
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    FILE_OVERWRITE = "FILE_OVERWRITE"
    PATH_OVERLAP = "PATH_OVERLAP"
    DEPLOYMENT_CONFLICT = "DEPLOYMENT_CONFLICT"
    DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
    GAME_RULE_CONFLICT = "GAME_RULE_CONFLICT"
    UNKNOWN_CONFLICT = "UNKNOWN_CONFLICT"


_TYPE_TO_CLASS = {
    ConflictType.FILE_OVERWRITE.value: ConflictClass.FILE_OVERWRITE.value,
    ConflictType.PAK_OVERLAP.value: ConflictClass.PATH_OVERLAP.value,
    ConflictType.RELATIONSHIP.value: ConflictClass.GAME_RULE_CONFLICT.value,
    ConflictType.UNKNOWN.value: ConflictClass.UNKNOWN_CONFLICT.value,
}


@dataclass
class ConflictDecisionTrace:
    conflict_type: str = ConflictClass.FILE_OVERWRITE.value
    mod_a: str = ""
    mod_b: str = ""
    workspace_a: str = ""
    workspace_b: str = ""
    source_a: str = ""
    source_b: str = ""
    target_a: str = ""
    target_b: str = ""
    overlap_count: int = 0
    sample_paths: list[str] = field(default_factory=list)
    rule_id: str = ""
    severity: str = "warning"
    decision: str = "warn"
    timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictEntry:
    file: str
    mods: list[str] = field(default_factory=list)
    conflict_type: str = ConflictType.FILE_OVERWRITE.value
    workspace_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "file": self.file,
            "mods": list(self.mods),
            "type": self.conflict_type,
        }
        if self.workspace_ids:
            payload["workspace_ids"] = list(self.workspace_ids)
        return payload


@dataclass
class ConflictReport:
    status: str = CONFLICT_STATUS_NONE
    conflicts: list[ConflictEntry] = field(default_factory=list)
    mod_id: str = ""
    traces: list[ConflictDecisionTrace] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "mod_id": self.mod_id,
            "traces": [t.as_dict() for t in self.traces],
        }


class ConflictDetector:
    """
    Detect deploy *target* path issues via ``.info/deploy_manifest.json``.

    Scheme B — two layers:

    Layer A (diagnostic, deterministic, Path.resolve() equality only):
    - FILE_OVERWRITE / PATH_OVERLAP: identical normalized target claimed by
      multiple enabled Mods. This is a filesystem fact, not a conflict
      relationship. No filename heuristic and no last-deploy winner guess.

    Layer B (conflict relationship):
    - Only a user-declared ``mod_relationships`` row of type conflict, or an
      explicit user ``conflict_status`` write, is a conflict relationship.
    - FILE_OVERWRITE must not auto-write ``conflict_status=conflict``.
    - ``persist=True`` must not restore conflict_status from path overlap after
      the user resolved / cleared it.

    ``ConflictType.PAK_OVERLAP`` is retained for compatibility but is **not**
    generated (same-dir distinct ``.pak`` files are legal).

    Disabled Mods are excluded from path detection.
    Detector never creates Mods, identities, workspace_ids, or relationships.
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

    def _known_mod_ids(self) -> set[str]:
        """Live SQLite identity set. Missing rows are not conflict owners."""
        try:
            db = self._database()
            with db._lock:  # noqa: SLF001
                rows = db._conn.execute("SELECT mod_id FROM mods").fetchall()  # noqa: SLF001
            return {str(row["mod_id"]) for row in rows}
        except Exception:  # noqa: BLE001
            logger.debug("conflict known-id load failed", exc_info=True)
            return set()

    def _is_enabled(self, mid: str) -> bool:
        if not mid.isdigit():
            return True
        try:
            if self._database().get_mod(mid) is None:
                return False
            return bool(self._database().is_mod_enabled(mid))
        except Exception:  # noqa: BLE001
            return False

    def _iter_manifest_targets(self) -> list[tuple[str, str]]:
        """List of (mod_id, normalized_target) for enabled Mods that exist in DB."""
        known = self._known_mod_ids()
        pairs: list[tuple[str, str]] = []
        for folder in self.files.list_managed_mods():
            manifest = load_manifest(folder)
            if manifest is None:
                continue
            mid = str(manifest.mod_id or "").strip()
            if not mid:
                meta = self.files.load_metadata(folder)
                mid = str(meta.published_file_id or "") if meta else ""
            if not mid:
                continue
            if mid.isdigit() and mid not in known:
                logger.info(
                    "[CONFLICT_SKIP] missing identity mod_id=%s folder=%s "
                    "(will not persist or mint)",
                    mid,
                    folder,
                )
                continue
            if not self._is_enabled(mid):
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

    def _workspace_id(self, mid: str) -> str:
        """Read existing Workspace ID only. Never mint or rewrite identity."""
        try:
            info = self._database().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            return ""
        if info is None:
            return ""
        return str(info.workspace_id or "").strip()

    def _workspace_ids_for(self, mids: list[str]) -> list[str]:
        return [self._workspace_id(m) for m in mids]

    @staticmethod
    def _relationship_entries(conflicts: list[ConflictEntry]) -> list[ConflictEntry]:
        return [
            c
            for c in conflicts
            if c.conflict_type == ConflictType.RELATIONSHIP.value
        ]

    def _relationship_status(
        self, conflicts: list[ConflictEntry]
    ) -> tuple[str, str]:
        """Layer B only: RELATIONSHIP is a conflict; FILE_OVERWRITE is not."""
        rel = self._relationship_entries(conflicts)
        if rel:
            return CONFLICT_STATUS_CONFLICT, self._summarize(rel)
        return CONFLICT_STATUS_NONE, ""

    def _persist_allowed(
        self,
        mid: str,
        *,
        relationship: list[ConflictEntry],
    ) -> None:
        """Persist allowed facts only. Never promote path overlap to conflict."""
        if not mid.isdigit():
            return
        if self._database().get_mod(mid) is None:
            logger.info(
                "[CONFLICT_SKIP] persist skipped missing mod_id=%s",
                mid,
            )
            return
        if relationship:
            self._database().update_mod_status(
                mid,
                conflict_status=CONFLICT_STATUS_CONFLICT,
                conflict_note=self._summarize(relationship),
                touch_check_time=True,
            )
            return
        # Diagnostic last_check_time only — leave user conflict_status unchanged.
        self._database().update_mod_status(mid, touch_check_time=True)

    def check_all_mods(self, *, persist: bool = True) -> dict[str, ConflictReport]:
        owners = self._collect_target_owners()
        per_mod: dict[str, list[ConflictEntry]] = {}
        all_manifest_mods: set[str] = set()

        # Layer A — exact target overwrite only (diagnostic, not a relationship)
        for target, mods in owners.items():
            for mid in mods:
                all_manifest_mods.add(mid)
            if len(mods) < 2:
                continue
            entry = ConflictEntry(
                file=target,
                mods=list(mods),
                conflict_type=ConflictType.FILE_OVERWRITE.value,
                workspace_ids=self._workspace_ids_for(mods),
            )
            for mid in mods:
                per_mod.setdefault(mid, []).append(entry)

        # Layer B — user-declared conflict relationships (never auto-inserted)
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
            pair = [src, tgt]
            entry = ConflictEntry(
                file=f"relationship:{src}->{tgt}",
                mods=pair,
                conflict_type=ConflictType.RELATIONSHIP.value,
                workspace_ids=self._workspace_ids_for(pair),
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
        traces_by_mod = self._build_decision_traces(per_mod)
        for mid in sorted(all_manifest_mods):
            conflicts = per_mod.get(mid) or []
            status, note = self._relationship_status(conflicts)
            traces = traces_by_mod.get(mid) or []
            reports[mid] = ConflictReport(
                status=status, conflicts=conflicts, mod_id=mid, traces=traces
            )
            if persist:
                self._persist_allowed(
                    mid,
                    relationship=self._relationship_entries(conflicts),
                )
        if persist:
            self._write_conflict_traces(reports)
        return reports

    def check_mod(self, mod_id: int | str, *, persist: bool = True) -> ConflictReport:
        mid = str(mod_id).strip()
        if mid.isdigit() and not self._is_enabled(mid):
            report = ConflictReport(
                status=CONFLICT_STATUS_NONE, conflicts=[], mod_id=mid
            )
            if persist:
                self._persist_allowed(mid, relationship=[])
            return report
        all_reports = self.check_all_mods(persist=persist)
        if mid in all_reports:
            return all_reports[mid]
        report = ConflictReport(status=CONFLICT_STATUS_NONE, conflicts=[], mod_id=mid)
        if persist:
            self._persist_allowed(mid, relationship=[])
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
            owners_list = [mid, *others]
            conflicts.append(
                ConflictEntry(
                    file=key,
                    mods=owners_list,
                    conflict_type=ConflictType.FILE_OVERWRITE.value,
                    workspace_ids=self._workspace_ids_for(owners_list),
                )
            )
        # Preview is diagnostic-only; overlap does not mint a conflict relationship.
        status, _note = self._relationship_status(conflicts)
        traces: list[ConflictDecisionTrace] = []
        if conflicts:
            paths = [c.file for c in conflicts]
            others: list[str] = []
            for c in conflicts:
                for m in c.mods:
                    if m != mid and m not in others:
                        others.append(m)
            partner = others[0] if others else ""
            traces.append(
                ConflictDecisionTrace(
                    conflict_type=ConflictClass.FILE_OVERWRITE.value,
                    mod_a=mid,
                    mod_b=partner,
                    workspace_a=self._workspace_id(mid),
                    workspace_b=self._workspace_id(partner) if partner else "",
                    source_a=mid,
                    source_b=partner,
                    target_a=paths[0] if paths else "",
                    target_b=paths[0] if paths else "",
                    overlap_count=len(paths),
                    sample_paths=paths[:8],
                    rule_id="FILE_OVERWRITE.identical_resolved_target",
                    severity="warning",
                    decision="warn",
                    timestamp=_utc_now(),
                )
            )
        return ConflictReport(
            status=status, conflicts=conflicts, mod_id=mid, traces=traces
        )

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

    def _build_decision_traces(
        self, per_mod: dict[str, list[ConflictEntry]]
    ) -> dict[str, list[ConflictDecisionTrace]]:
        now = _utc_now()
        seen: set[tuple[str, str, str]] = set()
        by_mod: dict[str, list[ConflictDecisionTrace]] = {}
        for mid, entries in per_mod.items():
            overwrite = [
                e for e in entries if e.conflict_type == ConflictType.FILE_OVERWRITE.value
            ]
            partners: dict[str, list[str]] = {}
            for entry in overwrite:
                for other in entry.mods:
                    if other == mid:
                        continue
                    partners.setdefault(other, []).append(entry.file)
            for other, paths in partners.items():
                key = tuple(sorted((mid, other))) + (ConflictClass.FILE_OVERWRITE.value,)
                if key in seen:
                    continue
                seen.add(key)
                sample = paths[:8]
                trace = ConflictDecisionTrace(
                    conflict_type=ConflictClass.FILE_OVERWRITE.value,
                    mod_a=mid,
                    mod_b=other,
                    workspace_a=self._workspace_id(mid),
                    workspace_b=self._workspace_id(other),
                    source_a=mid,
                    source_b=other,
                    target_a=sample[0] if sample else "",
                    target_b=sample[0] if sample else "",
                    overlap_count=len(paths),
                    sample_paths=sample,
                    rule_id="FILE_OVERWRITE.identical_resolved_target",
                    severity="warning",
                    decision="warn",
                    timestamp=now,
                )
                by_mod.setdefault(mid, []).append(trace)
                by_mod.setdefault(other, []).append(trace)
            for entry in entries:
                if entry.conflict_type != ConflictType.RELATIONSHIP.value:
                    continue
                others = [m for m in entry.mods if m != mid]
                if not others:
                    continue
                other = others[0]
                key = tuple(sorted((mid, other))) + (ConflictClass.GAME_RULE_CONFLICT.value,)
                if key in seen:
                    continue
                seen.add(key)
                trace = ConflictDecisionTrace(
                    conflict_type=ConflictClass.GAME_RULE_CONFLICT.value,
                    mod_a=mid,
                    mod_b=other,
                    workspace_a=self._workspace_id(mid),
                    workspace_b=self._workspace_id(other),
                    source_a=mid,
                    source_b=other,
                    overlap_count=1,
                    sample_paths=[entry.file],
                    rule_id="GAME_RULE.user_declared_conflict",
                    severity="warning",
                    decision="warn",
                    timestamp=now,
                )
                by_mod.setdefault(mid, []).append(trace)
                by_mod.setdefault(other, []).append(trace)
        return by_mod

    def _folder_for_mod(self, mod_id: str) -> Path | None:
        for folder in self.files.list_managed_mods():
            manifest = load_manifest(folder)
            if manifest is not None and str(manifest.mod_id or "").strip() == mod_id:
                return folder
            meta = self.files.load_metadata(folder)
            if meta and str(meta.published_file_id or "").strip() == mod_id:
                return folder
        return None

    def _write_conflict_traces(self, reports: dict[str, ConflictReport]) -> None:
        for mid, report in reports.items():
            if not report.traces:
                continue
            folder = self._folder_for_mod(mid)
            if folder is None:
                continue
            path = folder / INFO_DIR_NAME / CONFLICT_TRACE_FILENAME
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        [t.as_dict() for t in report.traces],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                logger.debug("conflict trace write failed for %s", mid, exc_info=True)
