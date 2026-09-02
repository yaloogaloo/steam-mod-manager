"""Deploy manifest read/write — records copied files for safe undeploy."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "deploy_manifest.json"

_prune_tls = threading.local()


@contextmanager
def prune_protection(protected: Iterable[Path] | None) -> Iterator[None]:
    """Push extra roots that ``remove_empty_parents`` must never delete."""
    prev = getattr(_prune_tls, "protected", None)
    merged: list[Path] = list(prev or [])
    for raw in protected or ():
        merged.append(Path(raw))
    _prune_tls.protected = merged
    try:
        yield
    finally:
        _prune_tls.protected = prev


@dataclass
class ManifestBackupInfo:
    """Pre-overwrite original file saved under ``.info/backups/``."""

    path: str
    hash: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "hash": self.hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ManifestBackupInfo | None:
        if not isinstance(data, Mapping):
            return None
        path = str(data.get("path") or "").strip()
        if not path:
            return None
        return cls(
            path=path,
            hash=str(data.get("hash") or ""),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass
class ManifestFileEntry:
    source: str
    target: str
    # "pak" | "folder_copy" — empty for legacy manifests
    type: str = ""
    # None = target did not exist before deploy (or legacy manifest)
    backup: ManifestBackupInfo | None = None
    # Optional audit field (backward compatible — omitted when empty)
    source_hash: str = ""


@dataclass
class DeployManifest:
    mod_id: str
    deploy_time: str
    deploy_type: str
    files: list[ManifestFileEntry] = field(default_factory=list)
    # Phase 8 optional fields (backward compatible)
    content_fingerprint: str = ""
    source_path: str = ""
    internal_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        files_out: list[dict[str, Any]] = []
        for f in self.files:
            item: dict[str, Any] = {"source": f.source, "target": f.target}
            if f.type:
                item["type"] = f.type
            if f.source_hash:
                item["source_hash"] = f.source_hash
            # Explicit null when no pre-existing file (new schema); omit for empty legacy
            if f.backup is not None:
                item["backup"] = f.backup.to_dict()
            else:
                item["backup"] = None
            files_out.append(item)
        out: dict[str, Any] = {
            "mod_id": self.mod_id,
            "deploy_time": self.deploy_time,
            "deploy_type": self.deploy_type,
            "files": files_out,
        }
        if self.content_fingerprint:
            out["content_fingerprint"] = self.content_fingerprint
        if self.source_path:
            out["source_path"] = self.source_path
        if self.internal_id:
            out["internal_id"] = self.internal_id
        return out

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
            backup = ManifestBackupInfo.from_dict(item.get("backup"))
            if src or tgt:
                files.append(
                    ManifestFileEntry(
                        source=src,
                        target=tgt,
                        type=entry_type,
                        backup=backup,
                        source_hash=str(item.get("source_hash") or ""),
                    )
                )
        return cls(
            mod_id=str(data.get("mod_id") or ""),
            deploy_time=str(data.get("deploy_time") or ""),
            deploy_type=str(data.get("deploy_type") or ""),
            files=files,
            content_fingerprint=str(data.get("content_fingerprint") or ""),
            source_path=str(data.get("source_path") or ""),
            internal_id=str(data.get("internal_id") or ""),
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


def load_manifest(
    managed_path: Path,
    *,
    expected_mod_id: str | None = None,
) -> DeployManifest | None:
    """
    Load ``deploy_manifest.json`` for a managed Mod folder.

    When *expected_mod_id* is set, refuse manifests that claim a different
    ``mod_id`` (returns ``None`` after logging — caller must not undeploy).
    """
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
    manifest = DeployManifest.from_dict(data)
    if expected_mod_id is not None:
        mid = str(expected_mod_id or "").strip()
        claimed = str(manifest.mod_id or "").strip()
        if mid and claimed and claimed != mid:
            logger.error(
                "manifest mod_id pollution refused path=%s expected=%s claimed=%s",
                path,
                mid,
                claimed,
            )
            return None
    return manifest


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


def remove_empty_parents(
    path: Path,
    *,
    stop_at: Path,
    protected: Iterable[Path] | None = None,
) -> None:
    """
    Remove empty directories upward from *path*, stopping at *stop_at*
    (never deletes *stop_at* itself or any *protected* root).
    """
    try:
        stop = stop_at.resolve()
    except OSError:
        return

    protected_resolved: set[Path] = {stop}
    tls_protected = getattr(_prune_tls, "protected", None) or ()
    for raw in list(protected or ()) + list(tls_protected):
        try:
            protected_resolved.add(Path(raw).resolve())
        except OSError:
            continue
    # Never prune filesystem / drive roots
    try:
        if stop.anchor:
            protected_resolved.add(Path(stop.anchor))
    except Exception:  # noqa: BLE001
        pass

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
        if cur in protected_resolved:
            break
        if cur == stop:
            break
        try:
            if cur.anchor and cur == Path(cur.anchor):
                break
        except Exception:  # noqa: BLE001
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
