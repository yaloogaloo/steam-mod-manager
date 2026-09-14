"""Durable Content-Addressed Asset Store (Phase 1 foundation).

Object identity = SHA-256 of **content bytes** (not URL, not Mod IDs).

Layout::

    <root>/
      sha256/
        ab/
          <64-hex-sha256>
      .tmp/
        put_<uuid>.part

This module is **dormant infrastructure**: nothing in the production archive /
Backup / Offline / MISS pipeline imports or calls it yet.

``cache/asset_cache`` remains a separate URL-keyed regenerable cache and must
not be used by this store.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from core.paths import asset_store_dir

HASH_CHUNK = 1024 * 1024
SHA256_HEX_LEN = 64
_HEX = frozenset("0123456789abcdef")


class AssetStoreError(Exception):
    """Base error for the durable asset store."""


class AssetNotFound(AssetStoreError):
    """Requested object is not in the store (no silent download)."""


class AssetCorruption(AssetStoreError):
    """Object exists but fails integrity checks."""


class AssetStoreValueError(AssetStoreError, ValueError):
    """Invalid argument (hash format, empty bytes, etc.)."""


@dataclass(frozen=True)
class AssetObject:
    """Published durable object."""

    sha256: str
    path: Path
    size: int
    created: bool
    """True if this call wrote a new object; False if already present."""


def normalize_sha256(value: str) -> str:
    """Return lowercase 64-hex SHA-256 or raise ``AssetStoreValueError``."""
    raw = str(value or "").strip().lower()
    if len(raw) != SHA256_HEX_LEN or any(c not in _HEX for c in raw):
        raise AssetStoreValueError(f"invalid sha256: {value!r}")
    return raw


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk: int = HASH_CHUNK) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def object_rel_path(sha256: str) -> Path:
    digest = normalize_sha256(sha256)
    return Path("sha256") / digest[:2] / digest


class AssetStore:
    """Filesystem durable CAS. Not an HTTP cache; not ``asset_cache``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else asset_store_dir()
        self._objects = self.root / "sha256"
        self._tmp = self.root / ".tmp"

    def ensure_layout(self) -> None:
        """Create store root / tmp dirs. Does not populate objects."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._objects.mkdir(parents=True, exist_ok=True)

    def object_path(self, sha256: str) -> Path:
        return self.root / object_rel_path(sha256)

    def has(self, sha256: str) -> bool:
        path = self.object_path(sha256)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def get_path(self, sha256: str) -> Path:
        """Return filesystem path or raise ``AssetNotFound`` (never downloads)."""
        digest = normalize_sha256(sha256)
        path = self.object_path(digest)
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path.resolve()
        except OSError as exc:
            raise AssetNotFound(digest) from exc
        raise AssetNotFound(digest)

    def open(self, sha256: str, mode: str = "rb") -> BinaryIO:
        if "b" not in mode:
            raise AssetStoreValueError("open() requires binary mode")
        if any(c in mode for c in "wxa+"):
            raise AssetStoreValueError("durable objects are read-only; use put_*")
        return self.get_path(sha256).open(mode)

    def verify(self, sha256: str) -> AssetObject:
        """
        Confirm object exists, size > 0, and content SHA-256 matches identity.

        Raises ``AssetNotFound`` or ``AssetCorruption``. Never auto-repairs.
        """
        digest = normalize_sha256(sha256)
        path = self.object_path(digest)
        try:
            if not path.is_file():
                raise AssetNotFound(digest)
            size = int(path.stat().st_size)
        except OSError as exc:
            raise AssetNotFound(digest) from exc
        if size <= 0:
            raise AssetCorruption(f"zero-size object: {digest}")
        try:
            actual = sha256_file(path)
        except OSError as exc:
            raise AssetCorruption(f"unreadable object: {digest}") from exc
        if actual != digest:
            raise AssetCorruption(
                f"hash mismatch for {digest}: actual={actual}"
            )
        return AssetObject(sha256=digest, path=path.resolve(), size=size, created=False)

    def put_bytes(self, data: bytes, *, expected_sha256: str | None = None) -> AssetObject:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise AssetStoreValueError("put_bytes requires bytes-like input")
        payload = bytes(data)
        if not payload:
            raise AssetStoreValueError("refusing to store empty object")
        digest = sha256_bytes(payload)
        if expected_sha256 is not None:
            want = normalize_sha256(expected_sha256)
            if want != digest:
                raise AssetStoreValueError(
                    f"expected sha256 {want} but bytes hash to {digest}"
                )
        return self._publish(digest, payload)

    def put_file(self, source: Path | str, *, expected_sha256: str | None = None) -> AssetObject:
        src = Path(source)
        if not src.is_file():
            raise AssetStoreValueError(f"not a file: {src}")
        try:
            size = int(src.stat().st_size)
        except OSError as exc:
            raise AssetStoreValueError(f"unreadable file: {src}") from exc
        if size <= 0:
            raise AssetStoreValueError("refusing to store empty file")
        digest = sha256_file(src)
        if expected_sha256 is not None:
            want = normalize_sha256(expected_sha256)
            if want != digest:
                raise AssetStoreValueError(
                    f"expected sha256 {want} but file hashes to {digest}"
                )
        return self._publish_from_path(digest, src, size=size)

    def delete(self, sha256: str) -> bool:
        """
        Remove an object if present. Returns True if removed.

        Phase 1: no GC / reachability. Intended for tests and explicit admin use.
        """
        path = self.object_path(sha256)
        try:
            if not path.is_file():
                return False
            path.unlink()
            return True
        except OSError:
            return False

    def iter_objects(self) -> Iterator[AssetObject]:
        """Yield published objects (no verify). Skips temps and non-hash names."""
        if not self._objects.is_dir():
            return
        for shard in sorted(self._objects.iterdir()):
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            if any(c not in _HEX for c in shard.name):
                continue
            try:
                entries = list(shard.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name.lower()
                if len(name) != SHA256_HEX_LEN or any(c not in _HEX for c in name):
                    continue
                if not name.startswith(shard.name):
                    continue
                try:
                    size = int(entry.stat().st_size)
                except OSError:
                    continue
                yield AssetObject(
                    sha256=name, path=entry.resolve(), size=size, created=False
                )

    def cleanup_temps(self) -> int:
        """Delete orphan ``.tmp/put_*.part`` files. Does not touch published objects."""
        if not self._tmp.is_dir():
            return 0
        removed = 0
        try:
            entries = list(self._tmp.iterdir())
        except OSError:
            return 0
        for entry in entries:
            name = entry.name
            if not (name.startswith("put_") and name.endswith(".part")):
                continue
            try:
                if entry.is_file():
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------------
    # Internal publish
    # ------------------------------------------------------------------

    def _existing_or_none(self, digest: str) -> AssetObject | None:
        path = self.object_path(digest)
        try:
            if not path.is_file():
                return None
            size = int(path.stat().st_size)
        except OSError:
            return None
        if size <= 0:
            # Invalid leftover — not a durable object; treat as absent for put,
            # but verify() would report corruption if called explicitly.
            return None
        # Fast path: size-known; integrity check before trusting existing.
        try:
            actual = sha256_file(path)
        except OSError:
            return None
        if actual != digest:
            raise AssetCorruption(
                f"existing object corrupted: {digest} actual={actual}"
            )
        return AssetObject(
            sha256=digest, path=path.resolve(), size=size, created=False
        )

    def _publish(self, digest: str, payload: bytes) -> AssetObject:
        existing = self._existing_or_none(digest)
        if existing is not None:
            return existing

        self.ensure_layout()
        final = self.object_path(digest)
        final.parent.mkdir(parents=True, exist_ok=True)

        tmp = self._tmp / f"put_{uuid.uuid4().hex}.part"
        try:
            with tmp.open("wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            actual = sha256_file(tmp)
            if actual != digest:
                raise AssetCorruption(
                    f"temp hash mismatch before publish: want={digest} got={actual}"
                )
            return self._atomic_publish(tmp, final, digest, size=len(payload))
        except Exception:
            self._unlink_quiet(tmp)
            raise

    def _publish_from_path(self, digest: str, src: Path, *, size: int) -> AssetObject:
        existing = self._existing_or_none(digest)
        if existing is not None:
            return existing

        self.ensure_layout()
        final = self.object_path(digest)
        final.parent.mkdir(parents=True, exist_ok=True)

        tmp = self._tmp / f"put_{uuid.uuid4().hex}.part"
        try:
            with src.open("rb") as in_fh, tmp.open("wb") as out_fh:
                while True:
                    block = in_fh.read(HASH_CHUNK)
                    if not block:
                        break
                    out_fh.write(block)
                out_fh.flush()
                os.fsync(out_fh.fileno())
            actual = sha256_file(tmp)
            if actual != digest:
                raise AssetCorruption(
                    f"temp hash mismatch before publish: want={digest} got={actual}"
                )
            return self._atomic_publish(tmp, final, digest, size=size)
        except Exception:
            self._unlink_quiet(tmp)
            raise

    def _atomic_publish(
        self, tmp: Path, final: Path, digest: str, *, size: int
    ) -> AssetObject:
        """
        Publish via ``os.rename`` (fails if destination exists on Windows/POSIX).

        Concurrent writers of the same hash: one wins rename; loser verifies
        the winner and discards its temp. Never uses ``os.replace`` on an
        existing durable object (immutability).
        """
        try:
            os.rename(tmp, final)
        except FileExistsError:
            self._unlink_quiet(tmp)
            existing = self._existing_or_none(digest)
            if existing is not None:
                return existing
            raise AssetCorruption(
                f"publish race left unreadable object at {final}"
            )
        except OSError:
            # Another process may have created final between our check and rename.
            if final.is_file():
                self._unlink_quiet(tmp)
                existing = self._existing_or_none(digest)
                if existing is not None:
                    return existing
            self._unlink_quiet(tmp)
            raise

        # Confirm published object
        try:
            actual = sha256_file(final)
            if actual != digest:
                # Do not leave a bad object under the durable name.
                self._unlink_quiet(final)
                raise AssetCorruption(
                    f"post-publish hash mismatch: want={digest} got={actual}"
                )
            published_size = int(final.stat().st_size)
        except OSError as exc:
            self._unlink_quiet(final)
            raise AssetCorruption(f"post-publish unreadable: {digest}") from exc

        return AssetObject(
            sha256=digest,
            path=final.resolve(),
            size=published_size,
            created=True,
        )

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
