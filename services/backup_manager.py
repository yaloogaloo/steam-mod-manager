"""Safe overwrite backups for deploy — original game files restored on undeploy.

Centralised so deploy_rules strategies keep using copy2/copytree unchanged.
Backups live under each Mod's ``.info/backups/``; metadata rides on the deploy
manifest ``files[].backup`` field (optional, backward compatible).

Security:
- Backup files must resolve under ``<mod>/.info/backups/`` (no arbitrary paths).
- ``backup.hash`` is verified after create and before restore.
- Partial restore failures are never silent (transaction left as ``failed``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestBackupInfo,
    load_manifest,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
TRANSACTION_FILENAME = "deploy_transaction.json"

TXN_PREPARED = "prepared"
TXN_BACKUP_DONE = "backup_done"
TXN_DEPLOYED = "deployed"
TXN_FAILED = "failed"


class BackupIntegrityError(Exception):
    """Backup file missing, path escape, or hash mismatch."""


class BackupRestoreError(Exception):
    """One or more restores failed; see ``.failures``."""

    def __init__(self, message: str, *, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = list(failures or [])


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _file_sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def backups_dir_for(managed: Path) -> Path:
    """Prefer existing ``.info`` / ``info``; default write under ``.info/backups``."""
    root = Path(managed)
    modern = root / INFO_DIR_NAME
    legacy = root / LEGACY_INFO_DIR_NAME
    if modern.is_dir() or not legacy.is_dir():
        return modern / BACKUPS_DIRNAME
    return legacy / BACKUPS_DIRNAME


def transaction_path_for(managed: Path) -> Path:
    root = Path(managed)
    modern = root / INFO_DIR_NAME / TRANSACTION_FILENAME
    if modern.is_file() or (root / INFO_DIR_NAME).is_dir():
        return modern
    legacy = root / LEGACY_INFO_DIR_NAME / TRANSACTION_FILENAME
    if legacy.is_file():
        return legacy
    return modern


@dataclass
class OverwritePrep:
    """Result of :meth:`BackupManager.prepare_overwrite`."""

    managed: Path
    # Resolved absolute target path → backup metadata (None = target did not exist)
    by_target: dict[str, ManifestBackupInfo | None] = field(default_factory=dict)

    def backup_for(self, target: str | Path) -> ManifestBackupInfo | None:
        key = _norm_target(target)
        if key in self.by_target:
            return self.by_target[key]
        want = Path(target)
        for stored, info in self.by_target.items():
            try:
                if Path(stored).resolve() == want.resolve():
                    return info
            except OSError:
                if Path(stored) == want:
                    return info
        return None


def _norm_target(target: str | Path) -> str:
    path = Path(target)
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


class BackupManager:
    """Prepare / restore / clean overwrite backups for one managed Mod folder."""

    def __init__(self, managed: Path) -> None:
        self.managed = Path(managed)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def backups_root(self) -> Path:
        return backups_dir_for(self.managed)

    def resolve_backup_file(self, backup: ManifestBackupInfo) -> Path:
        """
        Resolve ``backup.path`` to an absolute file under ``.info/backups/``.

        Accepts relative paths (preferred) or absolute paths that already sit
        inside this Mod's backups directory (legacy manifests from early builds).
        """
        raw = str(backup.path or "").strip()
        if not raw:
            raise BackupIntegrityError("backup path is empty")

        # Reject traversal before resolve (relative or absolute)
        if ".." in Path(raw).parts:
            raise BackupIntegrityError(f"backup path traversal rejected: {raw}")

        root = self.backups_root().resolve()
        candidate = Path(raw)
        if not candidate.is_absolute():
            # Relative to managed Mod root (``.info/backups/...``)
            candidate = (self.managed / candidate).resolve()
        else:
            candidate = candidate.resolve()

        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise BackupIntegrityError(
                f"backup path escapes .info/backups: {raw}"
            ) from exc
        return candidate

    def relative_backup_path(self, absolute: Path) -> str:
        """Store backup path relative to the managed Mod root (posix)."""
        abs_path = Path(absolute).resolve()
        try:
            return abs_path.relative_to(self.managed.resolve()).as_posix()
        except ValueError as exc:
            raise BackupIntegrityError(
                f"backup is outside managed mod root: {absolute}"
            ) from exc

    def verify_backup_hash(self, backup: ManifestBackupInfo) -> Path:
        """Ensure backup file exists under backups dir and matches stored hash."""
        src = self.resolve_backup_file(backup)
        if not src.is_file():
            raise BackupIntegrityError(f"backup missing: {src}")
        expected = str(backup.hash or "").strip()
        if not expected:
            # Legacy / incomplete metadata — refuse silent restore
            raise BackupIntegrityError(f"backup hash missing for {src}")
        actual = _file_sha256(src)
        if actual != expected:
            raise BackupIntegrityError(
                f"backup hash mismatch for {src}: expected={expected} actual={actual}"
            )
        return src

    # ------------------------------------------------------------------
    # Prepare (pre-deploy)
    # ------------------------------------------------------------------

    def prepare_overwrite(
        self,
        targets: Iterable[str | Path],
    ) -> OverwritePrep:
        """
        For each planned target that already exists as a file, copy it into
        ``.info/backups/`` with a unique name. Missing targets get ``backup=None``.

        Re-deploy while still deployed: reuse a valid prior manifest backup for
        the same target so the original game file is not replaced by a backup of
        the current Mod payload.

        On ``OSError`` / ``BackupIntegrityError`` / ``BackupRestoreError`` the
        transaction is marked ``failed`` (never left as ``prepared`` /
        ``backup_done``).
        """
        prep = OverwritePrep(managed=self.managed)
        unique_targets: list[Path] = []
        seen: set[str] = set()
        for raw in targets:
            key = _norm_target(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            unique_targets.append(Path(raw))

        target_keys = [_norm_target(t) for t in unique_targets]
        recorded: list[dict[str, Any]] = []

        try:
            self.write_transaction(
                status=TXN_PREPARED,
                targets=target_keys,
                backups=[],
            )

            prior_by_target = self._prior_valid_backups()
            backup_root = self.backups_root()

            for target in unique_targets:
                key = _norm_target(target)
                reused = prior_by_target.get(key)
                if reused is not None:
                    prep.by_target[key] = reused
                    recorded.append(
                        {
                            "target": key,
                            "path": reused.path,
                            "hash": reused.hash,
                            "created_at": reused.created_at,
                            "reused": True,
                        }
                    )
                    continue

                try:
                    exists = target.is_file()
                except OSError:
                    exists = False
                if not exists:
                    prep.by_target[key] = None
                    continue

                info = self._backup_one(target, backup_root)
                prep.by_target[key] = info
                recorded.append(
                    {
                        "target": key,
                        "path": info.path,
                        "hash": info.hash,
                        "created_at": info.created_at,
                    }
                )

            self.write_transaction(
                status=TXN_BACKUP_DONE,
                targets=list(prep.by_target.keys()),
                backups=recorded,
            )
            return prep
        except (OSError, BackupIntegrityError, BackupRestoreError):
            try:
                self.write_transaction(
                    status=TXN_FAILED,
                    targets=target_keys or list(prep.by_target.keys()),
                    backups=recorded,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to mark deploy transaction failed after prepare_overwrite error"
                )
                try:
                    self.clear_transaction()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to clear deploy transaction after prepare_overwrite error"
                    )
            raise

    def _prior_valid_backups(self) -> dict[str, ManifestBackupInfo]:
        """Map resolved target → reusable backup from the current manifest."""
        out: dict[str, ManifestBackupInfo] = {}
        existing = load_manifest(self.managed)
        if existing is None:
            return out
        for entry in existing.files:
            if entry.backup is None:
                continue
            key = _norm_target(entry.target)
            try:
                self.verify_backup_hash(entry.backup)
            except BackupIntegrityError:
                continue
            out[key] = ManifestBackupInfo(
                path=entry.backup.path,
                hash=entry.backup.hash,
                created_at=entry.backup.created_at,
            )
        return out

    def _backup_one(self, target: Path, backup_root: Path) -> ManifestBackupInfo:
        backup_root.mkdir(parents=True, exist_ok=True)
        content_hash = _file_sha256(target)
        short = content_hash[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        token = uuid.uuid4().hex[:8]
        safe_name = target.name.replace("\\", "_").replace("/", "_")
        backup_name = f"{safe_name}.{short}.{stamp}.{token}.original"
        dest = backup_root / backup_name
        # Never overwrite an existing backup file
        while dest.exists():
            token = uuid.uuid4().hex[:8]
            backup_name = f"{safe_name}.{short}.{stamp}.{token}.original"
            dest = backup_root / backup_name
        shutil.copy2(target, dest)

        # Integrity: hash the written backup (not only the source)
        written_hash = _file_sha256(dest)
        if written_hash != content_hash:
            try:
                dest.unlink()
            except OSError:
                pass
            raise BackupIntegrityError(
                f"backup write hash mismatch for {target}: "
                f"source={content_hash} backup={written_hash}"
            )

        rel = self.relative_backup_path(dest)
        return ManifestBackupInfo(
            path=rel,
            hash=written_hash,
            created_at=_utc_now(),
        )

    # ------------------------------------------------------------------
    # Manifest merge
    # ------------------------------------------------------------------

    def apply_to_manifest(
        self,
        manifest: DeployManifest,
        prep: OverwritePrep,
    ) -> DeployManifest:
        """Attach ``backup`` metadata onto matching ``manifest.files`` entries."""
        for entry in manifest.files:
            entry.backup = prep.backup_for(entry.target)
        return manifest

    # ------------------------------------------------------------------
    # Rollback / restore
    # ------------------------------------------------------------------

    def rollback(self, prep: OverwritePrep) -> None:
        """
        After a failed ``strategy.deploy``: restore originals that were backed up;
        remove targets that did not exist pre-deploy (partial new writes).

        Attempts every target; on any failure leaves ``failed`` transaction and
        does **not** delete backup files.
        """
        failures: list[str] = []
        for target_key, backup in prep.by_target.items():
            target = Path(target_key)
            if backup is not None:
                try:
                    self.restore_one(backup, target)
                except (OSError, BackupIntegrityError) as exc:
                    failures.append(f"{target}: {exc}")
                    logger.warning(
                        "rollback restore failed target=%s: %s", target, exc
                    )
            else:
                try:
                    if target.is_file():
                        target.unlink()
                except OSError as exc:
                    failures.append(f"{target} unlink: {exc}")
                    logger.warning(
                        "rollback unlink failed target=%s: %s", target, exc
                    )

        self.write_transaction(
            status=TXN_FAILED,
            targets=list(prep.by_target.keys()),
            backups=[
                {
                    "target": t,
                    "path": b.path,
                    "hash": b.hash,
                    "created_at": b.created_at,
                }
                for t, b in prep.by_target.items()
                if b is not None
            ],
        )
        if failures:
            # Keep backups + failed txn for diagnosis
            raise BackupRestoreError(
                "deploy rollback incomplete: " + "; ".join(failures),
                failures=failures,
            )
        # Clean success: originals restored — drop leftover copies + txn
        self.cleanup_backups()

    def restore_one(self, backup: ManifestBackupInfo, target: Path) -> None:
        """
        Restore *backup* onto *target*.

        Works even when *target* is missing (recreates parents + file).
        Verifies hash before copy; refuses path escape.
        """
        src = self.verify_backup_hash(backup)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() or (target.exists() and not target.is_dir()):
            target.unlink()
        shutil.copy2(src, target)
        # Post-condition: restored content matches backup hash
        restored_hash = _file_sha256(target)
        expected = str(backup.hash or "").strip()
        if restored_hash != expected:
            raise BackupIntegrityError(
                f"restored file hash mismatch for {target}: "
                f"expected={expected} actual={restored_hash}"
            )

    def restore_from_manifest(self, manifest: DeployManifest) -> int:
        """
        Restore every entry that carries backup metadata.

        Attempts all entries. On any failure: write ``failed`` transaction,
        keep backups, raise :class:`BackupRestoreError` (never silent).
        """
        restored = 0
        failures: list[str] = []
        for entry in manifest.files:
            if entry.backup is None:
                continue
            path = str(entry.backup.path or "").strip()
            if not path:
                failures.append(f"{entry.target}: empty backup path")
                continue
            try:
                self.restore_one(entry.backup, Path(entry.target))
                restored += 1
            except (OSError, BackupIntegrityError) as exc:
                failures.append(f"{entry.target}: {exc}")
                logger.warning(
                    "undeploy restore failed target=%s: %s", entry.target, exc
                )

        if failures:
            self.write_transaction(
                status=TXN_FAILED,
                targets=[e.target for e in manifest.files],
                backups=[
                    {
                        "target": e.target,
                        "path": e.backup.path,
                        "hash": e.backup.hash,
                        "created_at": e.backup.created_at,
                    }
                    for e in manifest.files
                    if e.backup is not None
                ],
                mod_id=str(manifest.mod_id or ""),
            )
            raise BackupRestoreError(
                "partial backup restore failed: " + "; ".join(failures),
                failures=failures,
            )
        return restored

    def cleanup_backups(self) -> None:
        """Remove ``.info/backups`` tree and clear deploy transaction."""
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            root = self.managed / info_name / BACKUPS_DIRNAME
            if not root.exists():
                continue
            try:
                shutil.rmtree(root)
            except OSError as exc:
                logger.warning("Failed to remove backups dir %s: %s", root, exc)
        self.clear_transaction()

    # ------------------------------------------------------------------
    # Transaction file
    # ------------------------------------------------------------------

    def write_transaction(
        self,
        *,
        status: str,
        targets: list[str],
        backups: list[Mapping[str, Any]],
        mod_id: str = "",
    ) -> Path:
        path = transaction_path_for(self.managed)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mod_id": str(mod_id or ""),
            "status": str(status or ""),
            "updated_at": _utc_now(),
            "targets": list(targets),
            "backups": [dict(item) for item in backups],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def clear_transaction(self) -> None:
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            path = self.managed / info_name / TRANSACTION_FILENAME
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove transaction %s: %s", path, exc)

    def load_transaction(self) -> dict[str, Any] | None:
        path = transaction_path_for(self.managed)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def mark_deployed(self, prep: OverwritePrep, *, mod_id: str = "") -> None:
        keep_paths = {
            str(b.path).replace("\\", "/")
            for b in prep.by_target.values()
            if b is not None and str(b.path or "").strip()
        }
        self.write_transaction(
            status=TXN_DEPLOYED,
            targets=list(prep.by_target.keys()),
            backups=[
                {
                    "target": t,
                    "path": b.path,
                    "hash": b.hash,
                    "created_at": b.created_at,
                }
                for t, b in prep.by_target.items()
                if b is not None
            ],
            mod_id=mod_id,
        )
        # Successful deploy keeps referenced backups for undeploy; drop txn + orphans.
        self.clear_transaction()
        self.prune_unreferenced_backups(keep_paths)

    def referenced_backup_paths(self) -> set[str]:
        """Relative backup paths still claimed by this Mod's deploy manifest."""
        keep: set[str] = set()
        man = load_manifest(self.managed)
        if man is None:
            return keep
        for entry in man.files:
            if entry.backup is None:
                continue
            raw = str(entry.backup.path or "").strip().replace("\\", "/")
            if raw:
                keep.add(raw)
        return keep

    def prune_unreferenced_backups(self, keep_relative: set[str]) -> None:
        """
        Delete backup files under ``.info/backups`` not listed in *keep_relative*.

        Always unions paths still referenced by the active deploy manifest so a
        shared/stale keep set cannot drop a still-needed backup.
        """
        keep = {str(p).replace("\\", "/") for p in keep_relative}
        keep |= self.referenced_backup_paths()
        root = self.backups_root()
        if not root.is_dir():
            return
        for path in root.iterdir():
            if not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(self.managed.resolve()).as_posix()
            except ValueError:
                continue
            if rel not in keep:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Failed to prune orphan backup %s: %s", path, exc)

    def validate_manifest_backups(self, manifest: DeployManifest) -> None:
        """Ensure every backup path belongs to this Mod's ``.info/backups``."""
        for entry in manifest.files:
            if entry.backup is None:
                continue
            self.resolve_backup_file(entry.backup)

    def recover_interrupted_transaction(
        self,
        *,
        auto_rollback: bool = True,
    ) -> dict[str, Any]:
        """
        Handle leftover ``deploy_transaction.json`` after a crash.

        - ``prepared`` / ``backup_done``: restore from txn backups (if
          *auto_rollback*) or mark ``failed`` for user attention.
        - ``failed``: leave in place; report ``needs_attention``.
        - ``deployed``: stale marker only → clear.
        - missing: ``none``.
        """
        txn = self.load_transaction()
        if not txn:
            return {"action": "none"}

        status = str(txn.get("status") or "").strip()
        if status == TXN_DEPLOYED:
            self.clear_transaction()
            return {"action": "cleared_stale_deployed_marker", "status": status}

        if status == TXN_FAILED:
            return {
                "action": "needs_attention",
                "status": status,
                "message": "deploy_transaction.json marked failed — awaiting user",
                "transaction": txn,
            }

        if status not in (TXN_PREPARED, TXN_BACKUP_DONE):
            return {
                "action": "needs_attention",
                "status": status or "unknown",
                "message": f"unrecognized transaction status: {status!r}",
                "transaction": txn,
            }

        if not auto_rollback:
            self.write_transaction(
                status=TXN_FAILED,
                targets=[str(t) for t in (txn.get("targets") or [])],
                backups=list(txn.get("backups") or []),
                mod_id=str(txn.get("mod_id") or ""),
            )
            return {
                "action": "marked_failed",
                "status": TXN_FAILED,
                "message": "interrupted deploy marked failed (auto_rollback=False)",
            }

        # Rebuild prep from transaction and roll back
        prep = OverwritePrep(managed=self.managed)
        for target_key in txn.get("targets") or []:
            prep.by_target[str(target_key)] = None
        for item in txn.get("backups") or []:
            if not isinstance(item, Mapping):
                continue
            target_key = str(item.get("target") or "").strip()
            path = str(item.get("path") or "").strip()
            if not target_key or not path:
                continue
            prep.by_target[target_key] = ManifestBackupInfo(
                path=path,
                hash=str(item.get("hash") or ""),
                created_at=str(item.get("created_at") or ""),
            )
        try:
            self.rollback(prep)
            return {
                "action": "rolled_back",
                "status": status,
                "message": "interrupted deploy rolled back from transaction",
            }
        except BackupRestoreError as exc:
            return {
                "action": "needs_attention",
                "status": TXN_FAILED,
                "message": str(exc),
                "failures": list(exc.failures),
                "transaction": self.load_transaction(),
            }


__all__ = (
    "BACKUPS_DIRNAME",
    "TRANSACTION_FILENAME",
    "TXN_BACKUP_DONE",
    "TXN_DEPLOYED",
    "TXN_FAILED",
    "TXN_PREPARED",
    "BackupIntegrityError",
    "BackupManager",
    "BackupRestoreError",
    "OverwritePrep",
    "backups_dir_for",
    "transaction_path_for",
)
