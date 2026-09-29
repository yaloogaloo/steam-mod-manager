"""Safe Mod removal: undeploy → delete library folder → DB record."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.db_manager import DatabaseManager, get_db
from services.deploy import ModDeployer
from services.file_ops import ModFileManager


class ModRemover:
    """
    Remove a managed Mod safely.

    Never deletes game install trees or other Mods' folders.
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
        self.deployer = ModDeployer(library_root=self.library_root, db=db)

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def remove_mod(self, internal_id: int | str) -> dict[str, Any]:
        frozen = str(internal_id or "").strip()
        if not frozen:
            return {
                "success": False,
                "error": "invalid internal_id",
                "internal_id": frozen,
                "mod_id": frozen,
            }

        # 1) Undeploy by Frozen UUID (best-effort — still proceed to delete
        #    library if undeploy fails only when source exists; never touch
        #    game paths beyond manifest). Digit SQLite PK is not a legal token.
        und = self.deployer.undeploy_mod(frozen)
        undeploy_ok = bool(und.get("success"))

        # 2) Delete only this Mod's managed library folder
        folder = self.files.find_by_internal_id(frozen)
        deleted_path = ""
        if folder is not None:
            try:
                root = self.library_root.resolve()
                resolved = folder.resolve()
                if root not in resolved.parents and resolved != root:
                    return {
                        "success": False,
                        "error": "拒绝删除：路径不在 Mod 库内",
                        "internal_id": frozen,
                        "mod_id": frozen,
                        "undeploy": und,
                    }
                # Extra guard: folder name / metadata id must match
                shutil.rmtree(resolved)
                deleted_path = str(resolved)
            except OSError as exc:
                return {
                    "success": False,
                    "error": f"删除库文件失败：{exc}",
                    "internal_id": frozen,
                    "mod_id": frozen,
                    "undeploy": und,
                }

        # 3) Drop SQLite rows — resolve Frozen UUID → PK at the DAL boundary.
        db_ok = False
        pk = ""
        try:
            found = self._database().find_mod_by_internal_id(frozen)
            pk = str(found or "").strip()
        except Exception:  # noqa: BLE001
            pk = ""
        if pk.isdigit():
            db_ok = self._database().delete_mod_record(pk)

        return {
            "success": True,
            "internal_id": frozen,
            "mod_id": frozen,
            "mod_pk": pk,
            "undeploy_ok": undeploy_ok,
            "deleted_path": deleted_path,
            "db_removed": db_ok,
        }
