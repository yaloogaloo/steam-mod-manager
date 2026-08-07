"""Pre-deploy conflict detection (warn only — does not block or overwrite policy)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from services.deploy_rules.manifest import load_manifest
from services.file_ops import ModFileManager


@dataclass(frozen=True)
class ConflictFile:
    target: str
    existing_mod: str


@dataclass
class ConflictResult:
    conflict: bool
    files: list[ConflictFile] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "conflict": self.conflict,
            "files": [
                {"target": f.target, "existing_mod": f.existing_mod}
                for f in self.files
            ],
        }


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


def index_manifest_targets(
    library_root: str | Path,
    *,
    exclude_mod_id: str | None = None,
) -> dict[str, str]:
    """
    Map resolved target path → owning ``mod_id`` from all deploy manifests.

    Last writer wins if duplicates exist inside one library (should be rare).
    """
    root = Path(library_root)
    files = ModFileManager(root)
    owned: dict[str, str] = {}
    for folder in files.list_managed_mods():
        manifest = load_manifest(folder)
        if manifest is None:
            continue
        mid = str(manifest.mod_id or "").strip()
        if not mid:
            # Fall back to metadata folder id if present
            meta = files.load_metadata(folder)
            mid = str(meta.published_file_id or "") if meta else ""
        if not mid:
            continue
        if exclude_mod_id and mid == str(exclude_mod_id):
            continue
        for entry in manifest.files:
            if not entry.target:
                continue
            owned[_norm(entry.target)] = mid
    return owned


def detect_deploy_conflicts(
    library_root: str | Path,
    mod_id: int | str,
    planned_targets: Iterable[str | Path],
) -> ConflictResult:
    """
    Check whether planned deploy targets are already claimed by another Mod's
    manifest.

    Detection only — callers decide whether to warn or proceed.
    """
    mid = str(mod_id).strip()
    owned = index_manifest_targets(library_root, exclude_mod_id=mid)
    hits: list[ConflictFile] = []
    seen: set[str] = set()
    for raw in planned_targets:
        key = _norm(raw)
        if key in seen:
            continue
        seen.add(key)
        other = owned.get(key)
        if other and other != mid:
            hits.append(ConflictFile(target=key, existing_mod=other))
    return ConflictResult(conflict=bool(hits), files=hits)
