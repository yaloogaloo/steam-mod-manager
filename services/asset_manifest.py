"""Asset Manifest contract (Phase 1 — model only, not wired to production).

A manifest lists relative paths used by an Offline page and maps each to a
Durable Asset Store content SHA-256. It does **not** change Offline Snapshot,
Backup, or ``.info/assets`` writers in Phase 1.

Object identity remains ``sha256`` (content). ``path`` is only the relative
reference used inside HTML/CSS — never Mod / Workspace / Internal ID.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from services.asset_store import normalize_sha256

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_UNC_RE = re.compile(r"^[/\\]{2}")


class ManifestError(ValueError):
    """Invalid manifest payload or unsafe path."""


@dataclass(frozen=True)
class AssetReference:
    """One offline-relative path → durable content object."""

    path: str
    sha256: str
    size: int
    media_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }
        if self.media_type:
            out["media_type"] = self.media_type
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AssetReference:
        path = validate_manifest_path(str(data.get("path") or ""))
        digest = normalize_sha256(str(data.get("sha256") or ""))
        try:
            size = int(data.get("size"))
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"invalid size for {path!r}") from exc
        if size < 0:
            raise ManifestError(f"negative size for {path!r}")
        media = data.get("media_type")
        media_type = str(media).strip() if media else None
        if media_type == "":
            media_type = None
        return cls(path=path, sha256=digest, size=size, media_type=media_type)


@dataclass
class AssetManifest:
    """Mod-scoped asset reference list (schema only in Phase 1)."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    assets: list[AssetReference] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "assets": [a.to_dict() for a in self.assets],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AssetManifest:
        try:
            version = int(data.get("schema_version", MANIFEST_SCHEMA_VERSION))
        except (TypeError, ValueError) as exc:
            raise ManifestError("invalid schema_version") from exc
        if version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"unsupported schema_version: {version}")
        raw_assets = data.get("assets")
        if raw_assets is None:
            raw_assets = []
        if not isinstance(raw_assets, list):
            raise ManifestError("assets must be a list")
        assets = [AssetReference.from_dict(item) for item in raw_assets]
        # Duplicate relative paths are rejected (ambiguous mapping).
        seen: set[str] = set()
        for ref in assets:
            if ref.path in seen:
                raise ManifestError(f"duplicate path: {ref.path!r}")
            seen.add(ref.path)
        return cls(schema_version=version, assets=assets)

    @classmethod
    def from_json(cls, text: str) -> AssetManifest:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestError("manifest root must be an object")
        return cls.from_dict(data)

    @classmethod
    def from_path(cls, path: Path | str) -> AssetManifest:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def write_json(self, path: Path | str) -> None:
        """Write manifest JSON atomically (temp + fsync + replace)."""
        write_manifest_atomic(Path(path), self)

    def sha256_set(self) -> set[str]:
        return {a.sha256 for a in self.assets}

    def add(self, ref: AssetReference) -> None:
        validate_manifest_path(ref.path)
        normalize_sha256(ref.sha256)
        for existing in self.assets:
            if existing.path == ref.path:
                raise ManifestError(f"duplicate path: {ref.path!r}")
        self.assets.append(ref)


def validate_manifest_path(path: str) -> str:
    """
    Accept only relative POSIX-style paths under the offline root.

    Rejects ``..``, absolute, drive, and UNC paths.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise ManifestError("empty path")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ManifestError(f"absolute path rejected: {path!r}")
    if _ABS_DRIVE_RE.match(raw) or _UNC_RE.match(raw):
        raise ManifestError(f"drive/UNC path rejected: {path!r}")
    if ":" in raw.split("/")[0]:
        # e.g. C:foo without slash
        raise ManifestError(f"drive path rejected: {path!r}")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise ManifestError(f"path traversal rejected: {path!r}")
    # Normalize to posix relative without leading ./
    normalized = PurePosixPath(*parts).as_posix() if parts else ""
    if not normalized or normalized.startswith("../") or normalized == "..":
        raise ManifestError(f"path traversal rejected: {path!r}")
    if normalized.startswith("/"):
        raise ManifestError(f"absolute path rejected: {path!r}")
    return normalized


def build_manifest(refs: Iterable[AssetReference]) -> AssetManifest:
    """Construct a validated manifest from references."""
    manifest = AssetManifest()
    for ref in refs:
        manifest.add(
            AssetReference(
                path=validate_manifest_path(ref.path),
                sha256=normalize_sha256(ref.sha256),
                size=int(ref.size),
                media_type=ref.media_type,
            )
        )
    return manifest


def write_manifest_atomic(path: Path, manifest: AssetManifest) -> None:
    """
    Atomic manifest publish: temp → flush/fsync → replace.

    On failure the destination is left unchanged (no half-written JSON).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.to_json().encode("utf-8")
    # Validate before writing.
    AssetManifest.from_json(payload.decode("utf-8"))
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest_",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def materialize_manifest_to_dir(
    manifest: AssetManifest,
    store: Any,
    dest_root: Path,
    *,
    only_missing: bool = False,
) -> dict[str, int]:
    """
    Write each manifest asset from the Durable Store into *dest_root*.

    Paths are relative (``assets/...``). Never downloads from the network.
    Verifies each object before copy. Does not delete unrelated siblings.
    """
    from services.asset_store import AssetCorruption, AssetNotFound, sha256_file

    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    stats = {"written": 0, "skipped": 0, "bytes": 0}
    issues = verify_manifest_against_store(manifest, store)
    if issues:
        raise ManifestError("; ".join(issues))

    for ref in manifest.assets:
        target = dest_root / Path(ref.path)
        if only_missing and target.is_file():
            try:
                if int(target.stat().st_size) == int(ref.size):
                    if sha256_file(target) == ref.sha256:
                        stats["skipped"] += 1
                        continue
            except OSError:
                pass
        try:
            src = store.get_path(ref.sha256)
        except AssetNotFound as exc:
            raise ManifestError(f"missing asset object: {ref.sha256}") from exc
        try:
            store.verify(ref.sha256)
        except (AssetNotFound, AssetCorruption) as exc:
            raise ManifestError(f"asset object unusable: {ref.sha256} ({exc})") from exc

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.stem}_",
            suffix=".part",
            dir=str(target.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with open(fd, "wb") as out_fh, src.open("rb") as in_fh:
                while True:
                    block = in_fh.read(1024 * 1024)
                    if not block:
                        break
                    out_fh.write(block)
                out_fh.flush()
                os.fsync(out_fh.fileno())
            actual = sha256_file(tmp_path)
            if actual != ref.sha256:
                raise ManifestError(
                    f"materialize hash mismatch for {ref.path}: {actual}"
                )
            os.replace(tmp_path, target)
            stats["written"] += 1
            stats["bytes"] += int(ref.size)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return stats


def compare_manifests(
    left: AssetManifest, right: AssetManifest
) -> list[str]:
    """Return mismatch issues for shared paths (sha256/size)."""
    issues: list[str] = []
    right_by_path = {a.path: a for a in right.assets}
    for a in left.assets:
        b = right_by_path.get(a.path)
        if b is None:
            continue
        if a.sha256 != b.sha256:
            issues.append(
                f"path {a.path}: sha256 mismatch left={a.sha256} right={b.sha256}"
            )
        elif int(a.size) != int(b.size):
            issues.append(
                f"path {a.path}: size mismatch left={a.size} right={b.size}"
            )
    return issues


def verify_manifest_against_store(
    manifest: AssetManifest,
    store: Any,
) -> list[str]:
    """
    Return a list of issues. Empty list means OK.

    Checks schema/path/hash already validated by parse; additionally verifies
    each durable object exists, size matches, and content hash matches.
    """
    from services.asset_store import AssetCorruption, AssetNotFound

    issues: list[str] = []
    for ref in manifest.assets:
        try:
            validate_manifest_path(ref.path)
            normalize_sha256(ref.sha256)
        except (ManifestError, Exception) as exc:  # noqa: BLE001
            issues.append(f"{ref.path}: invalid ref ({exc})")
            continue
        try:
            obj = store.verify(ref.sha256)
        except AssetNotFound:
            issues.append(f"{ref.path}: object missing {ref.sha256}")
            continue
        except AssetCorruption as exc:
            issues.append(f"{ref.path}: object corrupt ({exc})")
            continue
        if int(obj.size) != int(ref.size):
            issues.append(
                f"{ref.path}: size mismatch manifest={ref.size} store={obj.size}"
            )
    return issues
