"""Read-only audit of Mod deploy status vs filesystem / manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager, get_db
from services.deploy_rules.manifest import load_manifest
from services.file_ops import ModFileManager

STATUS_CONSISTENT = "consistent"
STATUS_MISSING = "missing"
STATUS_BROKEN = "broken"


@dataclass(frozen=True)
class DeployAuditResult:
    """Outcome of :func:`audit_deploy_state` (never mutates disk/DB)."""

    mod_id: str
    status: str  # consistent | missing | broken
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "mod_id": self.mod_id,
            "status": self.status,
            "reason": self.reason,
        }


def audit_deploy_state(
    mod_id: int | str,
    *,
    library_root: str | Path | None = None,
    db: DatabaseManager | None = None,
    managed_path: Path | None = None,
) -> DeployAuditResult:
    """
    Compare SQLite deploy_status with source folder, manifest, and targets.

    Does **not** repair, delete, or redeploy.
    """
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return DeployAuditResult(mid, STATUS_BROKEN, "无效的 Mod ID")

    database = db if db is not None else get_db()
    info = database.get_mod_deploy_info(mid)
    status = (info.deploy_status if info else "") or ""

    if status != DEPLOY_STATUS_DEPLOYED:
        return DeployAuditResult(
            mid,
            STATUS_CONSISTENT,
            "未标记为已部署，跳过一致性检查",
        )

    root = Path(library_root) if library_root else None
    files = ModFileManager(root) if root is not None else None
    source = managed_path
    if source is None and files is not None:
        source = files.find_by_published_id(mid)

    if source is None or not Path(source).is_dir():
        return DeployAuditResult(
            mid,
            STATUS_MISSING,
            "源 Mod 目录不存在",
        )

    source = Path(source)
    manifest = load_manifest(source)
    if manifest is None:
        return DeployAuditResult(
            mid,
            STATUS_BROKEN,
            "已部署但缺少 deploy_manifest.json",
        )
    if not manifest.files:
        return DeployAuditResult(
            mid,
            STATUS_BROKEN,
            "deploy_manifest.json 为空",
        )

    missing_targets: list[str] = []
    for entry in manifest.files:
        target = Path(entry.target)
        try:
            if not target.is_file():
                missing_targets.append(str(target))
        except OSError:
            missing_targets.append(str(target))

    if missing_targets:
        sample = missing_targets[0]
        extra = f" 等 {len(missing_targets)} 个" if len(missing_targets) > 1 else ""
        return DeployAuditResult(
            mid,
            STATUS_BROKEN,
            f"清单中的目标文件缺失：{sample}{extra}",
        )

    return DeployAuditResult(mid, STATUS_CONSISTENT, "")


def scan_deployed_mods(
    library_root: str | Path,
    *,
    db: DatabaseManager | None = None,
    mod_ids: Iterable[int | str] | None = None,
) -> list[DeployAuditResult]:
    """
    Lightweight startup scan: only mods marked ``deployed`` in SQLite.

    Existence checks only — does not read file contents.
    """
    database = db if db is not None else get_db()
    root = Path(library_root)
    if mod_ids is None:
        ids = database.list_deployed_mod_ids()
    else:
        ids = [str(m).strip() for m in mod_ids if str(m).strip().isdigit()]

    results: list[DeployAuditResult] = []
    for mid in ids:
        results.append(
            audit_deploy_state(mid, library_root=root, db=database)
        )
    return results


def anomalies_only(results: Iterable[DeployAuditResult]) -> list[DeployAuditResult]:
    return [r for r in results if r.status in (STATUS_MISSING, STATUS_BROKEN)]
