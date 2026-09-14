"""Deploy overwrite backups — original *game* files restored on undeploy.

This is not :mod:`services.metadata_backup` (metadata / cover / offline under
``data/mod_backup/``). Overwrite copies live under
``data/deploy_backup/<internal_id>/`` and must never be Mod payload inside
the Library folder or ``.info``.

Ownership:
- Missing target → no backup (first deploy write).
- Target claimed by this Mod's last successful deploy manifest → no backup
  (our previous payload, not an external original).
- Other existing target → copy as external/original for rollback/undeploy.

Security:
- New backup files must resolve under ``data/deploy_backup/<internal_id>/``.
- Legacy ``.info/backups/`` paths remain readable for old manifests.
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
from services.deployment_lifecycle import (
    DeploymentLifecycleState,
    PHASE_BACKUP_DONE,
    PHASE_BEGIN,
    PHASE_COMMITTED,
    PHASE_ROLLBACK,
    TXN_BACKUP_DONE,
    TXN_DEPLOYED,
    TXN_FAILED,
    TXN_PREPARED,
    persist_lifecycle_transaction,
    resolve_from_transaction,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
DEPLOY_BACKUP_DIR_NAME = "deploy_backup"
TRANSACTION_FILENAME = "deploy_transaction.json"


def _data_dir() -> Path:
    from core.paths import data_dir

    return data_dir()


def deploy_backup_root(internal_id: str | int) -> Path:
    """``data/deploy_backup/<internal_id>/`` — never under the Library Mod tree."""
    mid = str(internal_id or "").strip()
    if not mid:
        raise ValueError("deploy backup requires internal_id")
    return _data_dir() / DEPLOY_BACKUP_DIR_NAME / mid


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
    """Legacy Library-local overwrite dir (read/cleanup only; new writes go elsewhere)."""
    root = Path(managed)
    modern = root / INFO_DIR_NAME
    legacy = root / LEGACY_INFO_DIR_NAME
    if modern.is_dir() or not legacy.is_dir():
        return modern / BACKUPS_DIRNAME
    return legacy / BACKUPS_DIRNAME


def _infer_internal_id(managed: Path) -> str:
    """Return ``mods.mod_id`` PK for deploy-backup storage (never workspace_id).

    ``.info/internal_id`` is Entity UUID proof. Resolve it to the SQLite PK so
    ``data/deploy_backup/<mod_id>/`` stays aligned with Deploy's ``ctx.internal_id``.
    """
    try:
        from services.file_ops import read_info_metadata_dict
        from services.mod_identity import read_internal_id

        proof = str(read_internal_id(read_info_metadata_dict(managed) or {}) or "").strip()
        if not proof:
            return ""
        try:
            from core.db_manager import get_db

            db = get_db()
            found = db.find_mod_by_internal_id(proof)
            if found is not None and str(found).strip():
                return str(found).strip()
            if proof.isdigit() and db.get_mod(proof) is not None:
                return proof
        except Exception:  # noqa: BLE001
            logger.debug(
                "infer deploy-backup storage key failed for %s", managed, exc_info=True
            )
        return proof
    except Exception:  # noqa: BLE001
        return ""


def _orphan_storage_key(managed: Path) -> str:
    try:
        raw = str(Path(managed).resolve())
    except OSError:
        raw = str(managed)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
    return f"_orphan_{digest}"


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

    def __init__(self, managed: Path, *, internal_id: str = "") -> None:
        self.managed = Path(managed)
        self.internal_id = str(internal_id or "").strip()

    def storage_key(self) -> str:
        if self.internal_id:
            return self.internal_id
        inferred = _infer_internal_id(self.managed)
        if inferred:
            return inferred
        return _orphan_storage_key(self.managed)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def backups_root(self) -> Path:
        return _data_dir() / DEPLOY_BACKUP_DIR_NAME / self.storage_key()

    def _allowed_backup_roots(self) -> list[Path]:
        roots = [self.backups_root()]
        roots.append(self.managed / INFO_DIR_NAME / BACKUPS_DIRNAME)
        roots.append(self.managed / LEGACY_INFO_DIR_NAME / BACKUPS_DIRNAME)
        out: list[Path] = []
        for root in roots:
            try:
                out.append(root.resolve())
            except OSError:
                out.append(root)
        return out

    def resolve_backup_file(self, backup: ManifestBackupInfo) -> Path:
        """
        Resolve ``backup.path`` to an absolute overwrite-backup file.

        New writes: ``data/deploy_backup/<internal_id>/...``.
        Legacy manifests: ``.info/backups/...`` under the managed folder.
        """
        raw = str(backup.path or "").strip()
        if not raw:
            raise BackupIntegrityError("backup path is empty")

        posix = raw.replace("\\", "/")
        if ".." in Path(posix).parts or posix.startswith("../") or "/../" in posix:
            raise BackupIntegrityError(f"backup path traversal rejected: {raw}")

        candidate = Path(raw)
        if candidate.is_absolute():
            candidate = candidate.resolve()
        elif posix.startswith(DEPLOY_BACKUP_DIR_NAME + "/"):
            candidate = (_data_dir() / posix).resolve()
        elif posix.startswith(f"{INFO_DIR_NAME}/{BACKUPS_DIRNAME}/") or posix.startswith(
            f"{LEGACY_INFO_DIR_NAME}/{BACKUPS_DIRNAME}/"
        ):
            candidate = (self.managed / posix).resolve()
        else:
            candidate = (self.backups_root() / Path(posix).name).resolve()

        for root in self._allowed_backup_roots():
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise BackupIntegrityError(f"backup path escapes deploy backup store: {raw}")

    def relative_backup_path(self, absolute: Path) -> str:
        """Store backup path relative to ``data/`` when possible (posix)."""
        abs_path = Path(absolute).resolve()
        try:
            rel = abs_path.relative_to(_data_dir().resolve()).as_posix()
            if rel.startswith(DEPLOY_BACKUP_DIR_NAME + "/"):
                return rel
        except ValueError:
            pass
        try:
            return abs_path.relative_to(self.managed.resolve()).as_posix()
        except ValueError as exc:
            raise BackupIntegrityError(
                f"backup is outside deploy backup store: {absolute}"
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
        *,
        mod_id: str = "",
    ) -> OverwritePrep:
        """
        Snapshot *external* game files that a deploy is about to overwrite.

        Missing targets are not backed up (first write). Targets claimed by
        this Mod's last successful deploy manifest are not backed up either —
        those are our previous payload, not originals. Existing targets with
        no such claim are copied under ``data/deploy_backup/<internal_id>/``.

        Re-deploy reuses a valid prior manifest backup for the same target so
        the original game file is not replaced by a copy of our payload.

        On ``OSError`` / ``BackupIntegrityError`` / ``BackupRestoreError`` the
        transaction is marked ``failed`` (never left as ``prepared`` /
        ``backup_done``).
        """
        from services.deploy_txn import (
            log_txn_phase,
            register_active_deploy_transaction,
            unregister_active_deploy_transaction,
        )

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
        mid = str(mod_id or "").strip()
        lifecycle: DeploymentLifecycleState | None = None

        try:
            register_active_deploy_transaction(self.managed, internal_id=mid)
            lifecycle = persist_lifecycle_transaction(
                self,
                DeploymentLifecycleState.PREPARED,
                current=lifecycle,
                targets=target_keys,
                backups=[],
                mod_id=mid,
            )
            log_txn_phase(PHASE_BEGIN, internal_id=mid, managed=self.managed)

            prior_by_target = self._prior_valid_backups()
            owned_targets = self._owned_targets_from_last_success()
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

                if key in owned_targets:
                    # Last successful deploy already claimed this path. The
                    # file on disk is our payload, not an external original.
                    prep.by_target[key] = None
                    continue

                try:
                    exists = target.is_file()
                except OSError:
                    exists = False
                if not exists:
                    # Missing destination is a valid deploy state (first deploy /
                    # partial game tree). Skip backup — do not fail the stage.
                    prep.by_target[key] = None
                    continue

                try:
                    info = self._backup_one(target, backup_root)
                except FileNotFoundError:
                    # TOCTOU / vanished target between exists check and open —
                    # treat as missing original, not a deploy failure.
                    logger.info(
                        "backup skipped; destination missing target=%s", key
                    )
                    prep.by_target[key] = None
                    continue
                prep.by_target[key] = info
                recorded.append(
                    {
                        "target": key,
                        "path": info.path,
                        "hash": info.hash,
                        "created_at": info.created_at,
                    }
                )

            lifecycle = persist_lifecycle_transaction(
                self,
                DeploymentLifecycleState.BACKUP_DONE,
                current=lifecycle,
                targets=list(prep.by_target.keys()),
                backups=recorded,
                mod_id=mid,
            )
            log_txn_phase(PHASE_BACKUP_DONE, internal_id=mid, managed=self.managed)
            return prep
        except (OSError, BackupIntegrityError, BackupRestoreError):
            try:
                persist_lifecycle_transaction(
                    self,
                    DeploymentLifecycleState.FAILED,
                    current=lifecycle,
                    targets=target_keys or list(prep.by_target.keys()),
                    backups=recorded,
                    mod_id=mid,
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
            unregister_active_deploy_transaction(self.managed)
            # Apply never ran — drop transaction-only copies, keep still-referenced
            # backups from a prior successful deploy.
            try:
                self.prune_unreferenced_backups(set())
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to prune unreferenced backups after prepare_overwrite error"
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

    def _owned_targets_from_last_success(self) -> set[str]:
        """Targets written by this Mod's last successful deploy manifest."""
        existing = load_manifest(self.managed)
        if existing is None:
            return set()
        owned: set[str] = set()
        for entry in existing.files:
            key = _norm_target(entry.target)
            if key:
                owned.add(key)
        return owned

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

        cur = resolve_from_transaction(self.load_transaction())
        persist_lifecycle_transaction(
            self,
            DeploymentLifecycleState.FAILED,
            current=None if cur is DeploymentLifecycleState.CREATED else cur,
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
            cur = resolve_from_transaction(self.load_transaction())
            persist_lifecycle_transaction(
                self,
                DeploymentLifecycleState.FAILED,
                current=None if cur is DeploymentLifecycleState.CREATED else cur,
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

    def listed_backup_files(self) -> list[Path]:
        """Overwrite-backup files for this Mod (new store + leftover ``.info``)."""
        out: list[Path] = []
        for root in self._scan_backup_roots():
            if not root.is_dir():
                continue
            out.extend(sorted(p for p in root.iterdir() if p.is_file()))
        return out

    def _scan_backup_roots(self) -> list[Path]:
        return [
            self.backups_root(),
            self.managed / INFO_DIR_NAME / BACKUPS_DIRNAME,
            self.managed / LEGACY_INFO_DIR_NAME / BACKUPS_DIRNAME,
        ]

    def _drop_empty_backup_roots(self) -> None:
        for root in self._scan_backup_roots():
            if not root.is_dir():
                continue
            try:
                next(root.iterdir())
            except StopIteration:
                try:
                    root.rmdir()
                except OSError as exc:
                    logger.warning("Failed to remove empty backups dir %s: %s", root, exc)
            except OSError:
                continue

    def cleanup_backups(self) -> None:
        """Remove overwrite-backup trees and clear deploy transaction."""
        for root in self._scan_backup_roots():
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
        phase: str = "",
    ) -> Path:
        """Low-level wire sink. Prefer :func:`persist_lifecycle_transaction`."""
        path = transaction_path_for(self.managed)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "mod_id": str(mod_id or ""),
            "status": str(status or ""),
            "updated_at": _utc_now(),
            "targets": list(targets),
            "backups": [dict(item) for item in backups],
        }
        phase_s = str(phase or "").strip()
        if phase_s:
            payload["phase"] = phase_s
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
        """Commit transaction: mark deployed then clear txn file (success gate)."""
        from services.deploy_txn import log_txn_phase

        keep_paths = {
            str(b.path).replace("\\", "/")
            for b in prep.by_target.values()
            if b is not None and str(b.path or "").strip()
        }
        cur = resolve_from_transaction(self.load_transaction())
        persist_lifecycle_transaction(
            self,
            DeploymentLifecycleState.COMPLETED,
            current=None if cur is DeploymentLifecycleState.CREATED else cur,
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
        log_txn_phase(
            PHASE_COMMITTED, internal_id=str(mod_id or ""), managed=self.managed
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
        Delete overwrite-backup files not listed in *keep_relative*.

        Always unions paths still referenced by the active deploy manifest so a
        shared/stale keep set cannot drop a still-needed backup.
        """
        keep = {str(p).replace("\\", "/") for p in keep_relative}
        keep |= self.referenced_backup_paths()
        keep_resolved: set[str] = set()
        for raw in keep:
            try:
                resolved = self.resolve_backup_file(
                    ManifestBackupInfo(path=raw, hash="0")
                ).resolve()
            except (BackupIntegrityError, OSError):
                continue
            keep_resolved.add(str(resolved))
        for root in self._scan_backup_roots():
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if not path.is_file():
                    continue
                try:
                    rel = self.relative_backup_path(path)
                except BackupIntegrityError:
                    rel = ""
                try:
                    resolved = str(path.resolve())
                except OSError:
                    resolved = str(path)
                if (rel and rel in keep) or resolved in keep_resolved:
                    continue
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        "Failed to prune orphan backup %s: %s", path, exc
                    )
        self._drop_empty_backup_roots()

    def validate_manifest_backups(self, manifest: DeployManifest) -> None:
        """Ensure every backup path belongs to this Mod's overwrite-backup store."""
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
        try:
            state = resolve_from_transaction(txn, active_transaction=False)
        except Exception:  # noqa: BLE001 — keep compat for corrupt wire values
            return {
                "action": "needs_attention",
                "status": status or "unknown",
                "message": f"unrecognized transaction status: {status!r}",
                "transaction": txn,
            }

        if state is DeploymentLifecycleState.COMPLETED:
            self.clear_transaction()
            return {"action": "cleared_stale_deployed_marker", "status": status}

        if state is DeploymentLifecycleState.FAILED:
            return {
                "action": "needs_attention",
                "status": status,
                "message": "deploy_transaction.json marked failed — awaiting user",
                "transaction": txn,
            }

        if state is not DeploymentLifecycleState.ROLLBACK_REQUIRED:
            return {
                "action": "needs_attention",
                "status": status or "unknown",
                "message": f"unrecognized transaction status: {status!r}",
                "transaction": txn,
            }

        if not auto_rollback:
            persist_lifecycle_transaction(
                self,
                DeploymentLifecycleState.FAILED,
                current=state,
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
            from services.deploy_txn import (
                compose_recover_deploy_error,
                log_txn_phase,
                unregister_active_deploy_transaction,
            )

            log_txn_phase(
                PHASE_ROLLBACK,
                internal_id=str(txn.get("mod_id") or ""),
                managed=self.managed,
                extra=f"from_status={status} lifecycle={state.value}",
            )
            unregister_active_deploy_transaction(self.managed)
            return {
                "action": "rolled_back",
                "status": status,
                "lifecycle": DeploymentLifecycleState.ROLLED_BACK.value,
                "message": compose_recover_deploy_error(""),
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
    "DEPLOY_BACKUP_DIR_NAME",
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
    "deploy_backup_root",
    "transaction_path_for",
)
