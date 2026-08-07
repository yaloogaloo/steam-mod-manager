"""Deploy manifest read/write — records copied files for safe undeploy."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "deploy_manifest.json"


@dataclass
class ManifestFileEntry:
    source: str
    target: str
    # "pak" | "folder_copy" — empty for legacy manifests
    type: str = ""


@dataclass
class DeployManifest:
    mod_id: str
    deploy_time: str
    deploy_type: str
    files: list[ManifestFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        files_out: list[dict[str, Any]] = []
        for f in self.files:
            item = {"source": f.source, "target": f.target}
            if f.type:
                item["type"] = f.type
            files_out.append(item)
        return {
            "mod_id": self.mod_id,
            "deploy_time": self.deploy_time,
            "deploy_type": self.deploy_type,
            "files": files_out,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeployManifest:
        raw_files = data.get("files") or []
        files: list[ManifestFileEntry] = []
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            src = str(item.get("source") or "")
            tgt = str(item.get("target") or "")
            entry_type = str(item.get("type") or "")
            if src or tgt:
                files.append(
                    ManifestFileEntry(source=src, target=tgt, type=entry_type)
                )
        return cls(
            mod_id=str(data.get("mod_id") or ""),
            deploy_time=str(data.get("deploy_time") or ""),
            deploy_type=str(data.get("deploy_type") or ""),
            files=files,
        )


def manifest_path_for(managed_path: Path) -> Path:
    """Prefer existing ``.info`` / ``info``; default write target is ``.info``."""
    root = Path(managed_path)
    modern = root / INFO_DIR_NAME / MANIFEST_FILENAME
    if modern.is_file():
        return modern
    legacy = root / LEGACY_INFO_DIR_NAME / MANIFEST_FILENAME
    if legacy.is_file():
        return legacy
    return modern


def load_manifest(managed_path: Path) -> DeployManifest | None:
    path = manifest_path_for(managed_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read deploy manifest %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return DeployManifest.from_dict(data)


def save_manifest(managed_path: Path, manifest: DeployManifest) -> Path:
    path = manifest_path_for(managed_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def delete_manifest(managed_path: Path) -> None:
    for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
        path = Path(managed_path) / info_name / MANIFEST_FILENAME
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove manifest %s: %s", path, exc)


def remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    """
    Remove empty directories upward from *path*, stopping at *stop_at*
    (never deletes *stop_at* itself).
    """
    try:
        stop = stop_at.resolve()
    except OSError:
        return

    try:
        current = path.resolve()
    except OSError:
        current = Path(path)

    if current.is_file() or not current.exists():
        current = Path(path).parent

    while True:
        try:
            cur = current.resolve()
        except OSError:
            break
        if cur == stop:
            break
        try:
            if not cur.is_relative_to(stop):
                break
        except (ValueError, AttributeError):
            break
        try:
            if not cur.is_dir():
                break
            if any(cur.iterdir()):
                break
            parent = cur.parent
            cur.rmdir()
            current = parent
        except OSError:
            break
