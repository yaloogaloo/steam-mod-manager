"""Deploy managed Mods into a game's configured paths (strategy-based)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
    get_db,
)
from core.paths import default_mod_library
from services.conflict import ConflictDetector
from services.deploy_rules import (
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    PALWORLD_APP_ID,
    DeployContext,
    delete_manifest,
    get_strategy,
    load_manifest,
    resolve_deploy_type,
    save_manifest,
    supported_deploy_types,
)
from services.file_ops import ModFileManager

logger = logging.getLogger(__name__)


def resolve_deploy_sources(
    mod_id: int | str,
    source: Path,
    *,
    db: DatabaseManager | None = None,
) -> frozenset[str] | None:
    """
    Resolve which relative files may be deployed for *mod_id*.

    - ``mod_files`` empty → ``None`` (legacy: deploy whole Mod / strategy scan)
    - otherwise → relative paths of ``enabled=true`` entries only
    """
    database = db if db is not None else get_db()
    bundle = database.get_mod_files(mod_id)
    if not bundle.files:
        return None
    root = Path(source)
    allowed: set[str] = set()
    for entry in bundle.enabled_files():
        rel = (entry.path or entry.filename or "").replace("\\", "/").strip().lstrip("./")
        if not rel:
            continue
        allowed.add(rel)
        name = Path(rel).name
        if name:
            allowed.add(name)
        candidate = root / rel
        if candidate.is_file():
            try:
                allowed.add(candidate.resolve().relative_to(root.resolve()).as_posix())
            except ValueError:
                pass
    return frozenset(allowed)


def _normalize_deploy_error(error: str) -> str:
    """Map common failures to stable, user-facing messages."""
    text = (error or "").strip()
    if not text:
        return "Unknown deploy error"
    low = text.lower()
    if "请先配置游戏部署目录" in text or (
        "mod" in low and "directory" in low and "not" in low
    ):
        if "不存在" in text or "does not exist" in low:
            return "Target mod directory does not exist"
        return text
    if "游戏安装目录不存在" in text:
        return f"Target mod directory does not exist — {text}"
    if "permission denied" in low or "拒绝" in text and "访问" in text:
        if text.startswith("Permission denied"):
            return text
        return f"Permission denied — {text}"
    return text


class ModDeployer:
    """
    Facade: resolve Mod + game config, pick a :class:`DeployStrategy`,
    write ``deploy_manifest.json``, and update SQLite deploy columns.
    """

    def __init__(
        self,
        library_root: str | Path | None = None,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        root = Path(library_root) if library_root else default_mod_library()
        self.library_root = root.expanduser().resolve()
        self._db = db
        self.files = ModFileManager(self.library_root)

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def _resolve_context(
        self,
        mod_id: int | str,
        *,
        require_target_exists: bool = False,
    ) -> tuple[DeployContext | None, dict[str, Any] | None]:
        mid = str(mod_id).strip()
        if not mid.isdigit():
            return None, {
                "success": False,
                "error": f"无效的 Mod ID：{mod_id}",
            }

        db = self._database()
        db_meta = db.get_mod(mid)
        source = self.files.find_by_published_id(mid)

        if source is None or not source.is_dir():
            return None, {
                "success": False,
                "error": (
                    f"源 Mod 目录不存在（库：{self.library_root}，mod_id={mid}）"
                ),
                "mod_id": mid,
            }

        source = source.resolve()
        fs_meta = self.files.load_metadata(source)
        app_id = 0
        if db_meta and db_meta.app_id:
            app_id = int(db_meta.app_id)
        elif fs_meta and fs_meta.app_id:
            app_id = int(fs_meta.app_id)

        if not app_id:
            return None, {
                "success": False,
                "error": f"无法解析 Mod 所属游戏 AppID（mod_id={mid}）",
                "mod_id": mid,
            }

        cfg = db.get_game_deploy_config(app_id)
        if cfg is None:
            return None, {
                "success": False,
                "error": "请先配置游戏部署目录",
                "mod_id": mid,
            }

        deploy_type = resolve_deploy_type(app_id, cfg.deploy_type)
        if deploy_type == DEPLOY_TYPE_FOLDER_COPY and not str(
            cfg.mod_path or ""
        ).strip():
            return None, {
                "success": False,
                "error": "请先配置游戏部署目录",
                "mod_id": mid,
            }
        # Palworld: install_path and/or mod_path — strategy picks pak vs folder_copy.
        # Do not require install_path alone (folder-only Mods still need mod_path).
        if deploy_type == DEPLOY_TYPE_PALWORLD_PAK:
            has_install = bool(str(cfg.install_path or "").strip())
            has_mod = bool(str(cfg.mod_path or "").strip())
            if not has_install and not has_mod:
                return None, {
                    "success": False,
                    "error": "请先配置游戏安装目录或部署目录",
                    "mod_id": mid,
                }
        if (
            require_target_exists
            and deploy_type == DEPLOY_TYPE_FOLDER_COPY
        ):
            mod_path = Path(str(cfg.mod_path).strip()).expanduser()
            if not mod_path.exists():
                return None, {
                    "success": False,
                    "error": "Target mod directory does not exist",
                    "mod_id": mid,
                }
        if (
            require_target_exists
            and deploy_type == DEPLOY_TYPE_PALWORLD_PAK
        ):
            install_raw = str(cfg.install_path or "").strip()
            mod_raw = str(cfg.mod_path or "").strip()
            install_ok = (
                bool(install_raw)
                and Path(install_raw).expanduser().is_dir()
            )
            mod_ok = bool(mod_raw) and Path(mod_raw).expanduser().exists()
            if not install_ok and not mod_ok:
                return None, {
                    "success": False,
                    "error": "Target mod directory does not exist",
                    "mod_id": mid,
                }

        return (
            DeployContext(
                mod_id=mid,
                source=source,
                app_id=app_id,
                config=cfg,
                deploy_type=deploy_type,
                allowed_rel_paths=resolve_deploy_sources(
                    mid, source, db=db
                ),
            ),
            None,
        )

    def _mark_failed(
        self, mid: str, *, app_id: int = 0, error: str = ""
    ) -> None:
        msg = _normalize_deploy_error(error)
        try:
            self._database().update_mod_deploy_status(
                mid,
                deploy_status=DEPLOY_STATUS_FAILED,
                deploy_path="",
                deploy_time="",
                deploy_error=msg,
                app_id=app_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DEPLOY] mod_id=%s failed to record failed status: %s (%s)",
                mid,
                exc,
                msg,
            )

    def deploy_mod(self, mod_id: int | str) -> dict[str, Any]:
        """Deploy one Mod by Workshop / published file id."""
        mid = str(mod_id).strip()
        log_prefix = f"[DEPLOY] mod_id={mid}"

        if mid.isdigit() and not self._database().is_mod_enabled(mid):
            error = "Mod disabled"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {
                "success": False,
                "error": error,
                "mod_id": mid,
            }

        # Relationship warnings (hint only — never blocks, never auto-enables)
        relationship_warnings: list[dict[str, Any]] = []
        if mid.isdigit():
            try:
                relationship_warnings = (
                    self._database().check_relationship_deploy_warnings(mid)
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s relationship warning check failed", log_prefix)
            for w in relationship_warnings:
                logger.warning("%s relation_warn=%s", log_prefix, w.get("message"))

        ctx, early = self._resolve_context(mod_id, require_target_exists=True)
        if early is not None:
            logger.warning("%s result=fail error=%s", log_prefix, early.get("error"))
            if mid.isdigit():
                self._mark_failed(mid, error=str(early.get("error") or ""))
            out = dict(early)
            out["error"] = _normalize_deploy_error(str(early.get("error") or ""))
            return out
        assert ctx is not None

        strategy = get_strategy(ctx.deploy_type, app_id=ctx.app_id)
        if strategy is None:
            error = f"暂不支持的部署类型：{ctx.deploy_type}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": ctx.mod_id,
                "supported": list(supported_deploy_types()),
            }

        game_label = (
            "Palworld"
            if int(ctx.app_id) == PALWORLD_APP_ID
            else (ctx.config.name or f"App_{ctx.app_id}")
        )
        logger.info(
            "[DEPLOY] game=%s app_id=%s strategy=%s",
            game_label,
            ctx.app_id,
            type(strategy).__name__,
        )

        # Conflict detection (warn only — never blocks deploy)
        conflicts_payload: dict[str, Any] | None = None
        planned = strategy.plan(ctx)
        if planned.success and planned.files:
            conflicts_payload = self.check_conflict_preview(
                ctx.mod_id,
                [e.target for e in planned.files],
            )
            if conflicts_payload and conflicts_payload.get("conflict"):
                logger.warning(
                    "%s conflict=true files=%s",
                    log_prefix,
                    len(conflicts_payload.get("files") or []),
                )

        result = strategy.deploy(ctx)
        if not result.success:
            err = _normalize_deploy_error(result.error)
            logger.warning(
                "%s source=%s result=fail error=%s",
                log_prefix,
                ctx.source,
                err,
            )
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=err)
            out = {
                "success": False,
                "error": err,
                "mod_id": ctx.mod_id,
                "deploy_type": ctx.deploy_type,
            }
        if conflicts_payload:
            out["conflicts"] = conflicts_payload
        if relationship_warnings:
            out["relationship_warnings"] = relationship_warnings
        return out

        assert result.manifest is not None
        try:
            save_manifest(ctx.source, result.manifest)
        except OSError as exc:
            error = f"文件已复制，但写入部署清单失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        try:
            self._database().update_mod_deploy_status(
                ctx.mod_id,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path=result.target,
                deploy_time=result.deploy_time,
                deploy_error="",
                app_id=ctx.app_id,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"文件已复制，但更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        # Post-deploy: refresh library-wide file-path conflicts into SQLite
        try:
            ConflictDetector(
                self.library_root, db=self._database()
            ).check_all_mods(persist=True)
        except Exception:  # noqa: BLE001
            logger.exception("%s post-deploy conflict scan failed", log_prefix)

        logger.info(
            "%s source=%s target=%s type=%s result=ok copied=%s",
            log_prefix,
            ctx.source,
            result.target,
            result.deploy_type,
            result.copied_files,
        )
        out = {
            "success": True,
            "mod_id": ctx.mod_id,
            "target": result.target,
            "copied_files": result.copied_files,
            "deploy_type": result.deploy_type,
            "deploy_time": result.deploy_time,
        }
        if conflicts_payload:
            out["conflicts"] = conflicts_payload
        return out

    def check_conflict_preview(
        self,
        mod_id: int | str,
        planned_targets: list[str | Path],
    ) -> dict[str, Any] | None:
        """
        Pre-deploy path-overlap preview. Never blocks; returns warning payload.
        """
        mid = str(mod_id).strip()
        report = ConflictDetector(
            self.library_root, db=self._database()
        ).preview_targets(mid, list(planned_targets))
        if report.status == "none" or not report.conflicts:
            return None
        files = []
        for entry in report.conflicts:
            others = [m for m in entry.mods if m != mid]
            files.append(
                {
                    "target": entry.file,
                    "existing_mod": others[0] if others else "",
                }
            )
        return {
            "conflict": True,
            "status": report.status,
            "conflicts": [c.as_dict() for c in report.conflicts],
            "files": files,
        }

    def undeploy_mod(self, mod_id: int | str) -> dict[str, Any]:
        """
        Remove files listed in ``deploy_manifest.json`` and clear DB status.

        Never deletes an entire target directory tree — only manifest targets.
        """
        mid = str(mod_id).strip()
        log_prefix = f"[UNDEPLOY] mod_id={mid}"

        ctx, early = self._resolve_context(mod_id)
        if early is not None:
            if mid.isdigit() and "源 Mod 目录不存在" in str(early.get("error") or ""):
                try:
                    self._database().update_mod_deploy_status(
                        mid,
                        deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                        deploy_path="",
                        deploy_time="",
                        deploy_error="",
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.warning("%s result=fail error=%s", log_prefix, early.get("error"))
            out = dict(early)
            out["error"] = _normalize_deploy_error(str(early.get("error") or ""))
            return out
        assert ctx is not None

        strategy = get_strategy(ctx.deploy_type, app_id=ctx.app_id)
        if strategy is None:
            strategy = get_strategy(DEPLOY_TYPE_FOLDER_COPY, app_id=0)
        assert strategy is not None

        manifest = load_manifest(ctx.source)
        result = strategy.undeploy(ctx, manifest)
        if not result.success:
            logger.warning("%s result=fail error=%s", log_prefix, result.error)
            return {
                "success": False,
                "error": result.error,
                "mod_id": ctx.mod_id,
            }

        delete_manifest(ctx.source)
        try:
            self._database().update_mod_deploy_status(
                ctx.mod_id,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error="",
                app_id=ctx.app_id,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"文件已移除，但更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        try:
            ConflictDetector(
                self.library_root, db=self._database()
            ).check_all_mods(persist=True)
        except Exception:  # noqa: BLE001
            logger.exception("%s post-undeploy conflict scan failed", log_prefix)

        logger.info(
            "%s source=%s result=ok removed=%s",
            log_prefix,
            ctx.source,
            result.copied_files,
        )
        return {
            "success": True,
            "mod_id": ctx.mod_id,
            "removed_files": result.copied_files,
            "deploy_type": result.deploy_type or ctx.deploy_type,
        }

    def redeploy_mod(self, mod_id: int | str) -> dict[str, Any]:
        """
        Redeploy ≡ undeploy (old manifest) + deploy (new files + new manifest).

        Aborts if undeploy fails, so removed source files cannot linger.
        """
        mid = str(mod_id).strip()
        und = self.undeploy_mod(mod_id)
        if not und.get("success"):
            err = str(und.get("error") or "取消部署失败")
            # Persist failed redeploy reason when we still have a source
            if mid.isdigit() and "源 Mod 目录不存在" not in err:
                self._mark_failed(mid, error=f"重新部署中止：{err}")
            return {
                "success": False,
                "error": f"重新部署中止：取消部署未完成 — {err}",
                "mod_id": mid,
                "undeploy": und,
            }
        return self.deploy_mod(mod_id)
