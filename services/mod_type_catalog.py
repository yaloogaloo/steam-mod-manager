"""Game-scoped Mod Type Definitions.

Authority is a user-editable JSON file. Mods persist only ``mods.type_id``.
Type names are display data and must never become the binding key.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.models import MOD_TYPE_BEAUTIFY, MOD_TYPE_EXTENSION
from core.paths import mod_types_path

logger = logging.getLogger(__name__)

CATALOG_VERSION = 1


class ModTypeCatalogError(ValueError):
    """Invalid Type Definition file or mutation."""


@dataclass(frozen=True)
class ModTypeDef:
    app_id: int
    type_id: int
    name: str


def coerce_type_id(value: object) -> int | None:
    """Positive int Type ID, or None when unbound / unparseable."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        tid = int(text)
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def legacy_migrated_marker_path(catalog_path: Path) -> Path:
    """Sidecar so deleting ``mod_types.json`` cannot re-import legacy tables."""
    return Path(catalog_path).with_name(Path(catalog_path).stem + ".legacy_migrated")


def _marker_is_set(catalog_path: Path) -> bool:
    return legacy_migrated_marker_path(catalog_path).is_file()


def _write_legacy_migrated_marker(catalog_path: Path) -> None:
    marker = legacy_migrated_marker_path(catalog_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="utf-8")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class ModTypeCatalog:
    """In-memory Type Definition store backed by ``data/mod_types.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else mod_types_path()
        self._games: dict[int, dict[int, str]] = {}
        self._order: dict[int, list[int]] = {}
        self._extension_ids: dict[int, int] = {}
        self._valid = False
        self._error: str = ""
        self._legacy_migrated = False

    def reload(self, *, db: Any | None = None, reconcile: bool = False) -> None:
        """Load the persistence file. Invalid files are refused, not repaired."""
        if not self.path.is_file():
            self._games = {}
            self._order = {}
            self._extension_ids = {}
            self._valid = True
            self._error = ""
            self._legacy_migrated = _marker_is_set(self.path)
            if db is not None:
                from services.mod_type_legacy_migration import migrate_legacy_mod_types

                migrate_legacy_mod_types(self, db)
            if reconcile and db is not None:
                reconcile_orphan_type_ids(self, db)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            games, order, extension_ids = _parse_catalog_payload(raw)
            migrated = bool(raw.get("legacy_migrated")) if isinstance(raw, dict) else False
        except ModTypeCatalogError as exc:
            self._error = str(exc)
            if not self._games:
                self._valid = False
            logger.warning("mod type catalog refused: %s", exc)
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._error = str(exc)
            if not self._games:
                self._valid = False
            logger.warning("mod type catalog unreadable: %s", exc)
            raise ModTypeCatalogError(f"无法读取类型定义文件：{exc}") from exc
        self._games = games
        self._order = order
        dirty = self._adopt_extension_ids(extension_ids)
        self._valid = True
        self._error = ""
        self._legacy_migrated = bool(migrated) or _marker_is_set(self.path)
        if dirty:
            try:
                self._save()
            except Exception:  # noqa: BLE001
                logger.debug("could not persist inferred extension_type_id", exc_info=True)
        if db is not None:
            from services.mod_type_legacy_migration import migrate_legacy_mod_types

            migrate_legacy_mod_types(self, db)
        if reconcile and db is not None:
            reconcile_orphan_type_ids(self, db)

    def is_legacy_migrated(self) -> bool:
        return bool(self._legacy_migrated)

    def is_valid(self) -> bool:
        return self._valid

    def error_message(self) -> str:
        return self._error

    def find_type_by_name(self, app_id: int | str, name: str) -> ModTypeDef | None:
        """Exact name match within one game. Names are display data, not identity."""
        aid = int(app_id or 0)
        label = str(name or "")
        if aid <= 0 or not label or not self._valid:
            return None
        for tid, existing in (self._games.get(aid) or {}).items():
            if existing == label:
                return ModTypeDef(app_id=aid, type_id=int(tid), name=existing)
        return None

    def ensure_named_type(self, app_id: int | str, name: str) -> tuple[ModTypeDef, bool]:
        """
        Reuse the existing Type ID for an exact ``(app_id, name)``, or mint one.

        Does not persist. Caller must ``_save`` / ``persist``.
        """
        aid = int(app_id or 0)
        label = str(name or "")
        if aid <= 0:
            raise ModTypeCatalogError("缺少游戏作用域，无法新增类型")
        if not label:
            raise ModTypeCatalogError("类型名称不能为空")
        if not self._valid:
            raise ModTypeCatalogError(self._error or "类型定义文件无效，拒绝写入")
        found = self.find_type_by_name(aid, label)
        if found is not None:
            self._stamp_extension_type(aid, found.type_id, found.name)
            return found, False
        names = self._games.setdefault(aid, {})
        order = self._order.setdefault(aid, [])
        next_id = (max(names) + 1) if names else 1
        names[next_id] = label
        order.append(next_id)
        self._stamp_extension_type(aid, next_id, label)
        return ModTypeDef(app_id=aid, type_id=next_id, name=label), True

    def persist(self) -> None:
        self._save()

    def mark_legacy_migrated(self) -> None:
        self._legacy_migrated = True
        self._save()
        _write_legacy_migrated_marker(self.path)

    def list_types(self, app_id: int | str) -> list[ModTypeDef]:
        aid = int(app_id or 0)
        if aid <= 0 or not self._valid:
            return []
        names = self._games.get(aid) or {}
        order = self._order.get(aid) or list(names)
        out: list[ModTypeDef] = []
        for tid in order:
            name = names.get(tid)
            if name is None:
                continue
            out.append(ModTypeDef(app_id=aid, type_id=tid, name=name))
        return out

    def extension_type_id(self, app_id: int | str) -> int | None:
        """Per-game Type ID that unlocks Extension Category. Independent of display name."""
        aid = int(app_id or 0)
        if aid <= 0 or not self._valid:
            return None
        tid = self._extension_ids.get(aid)
        if tid is None:
            return None
        names = self._games.get(aid) or {}
        return int(tid) if tid in names else None

    def is_extension_type(self, app_id: int | str, type_id: int | str | None) -> bool:
        ext = self.extension_type_id(app_id)
        tid = coerce_type_id(type_id)
        return ext is not None and tid == ext

    def unlocks_subcategory(self, app_id: int | str, type_id: int | str | None) -> bool:
        """True when this Type shows the optional「分类」field.

        拓展 uses the durable per-game stamp (survives display rename).
        美化 uses the current Type display name and the same ``mods.category`` column.
        """
        if self.is_extension_type(app_id, type_id):
            return True
        return self.resolve_name(app_id, type_id) == MOD_TYPE_BEAUTIFY

    def _stamp_extension_type(self, app_id: int, type_id: int, name: str) -> bool:
        """First Type created/found as the canonical extension label wins. Name may later change."""
        if str(name or "") != MOD_TYPE_EXTENSION:
            return False
        if app_id in self._extension_ids:
            return False
        self._extension_ids[app_id] = int(type_id)
        return True

    def _adopt_extension_ids(self, parsed: Mapping[int, int]) -> bool:
        """Keep persisted stamps; infer from canonical name when missing. Returns persist-needed."""
        adopted: dict[int, int] = {}
        dirty = False
        for aid, names in self._games.items():
            stamped = coerce_type_id((parsed or {}).get(aid))
            if stamped is not None and stamped in names:
                adopted[aid] = stamped
                continue
            inferred: int | None = None
            for tid, name in names.items():
                if name == MOD_TYPE_EXTENSION:
                    inferred = int(tid)
                    break
            if inferred is not None:
                adopted[aid] = inferred
                dirty = True
        self._extension_ids = adopted
        return dirty

    def resolve_name(self, app_id: int | str, type_id: int | str | None) -> str:
        aid = int(app_id or 0)
        tid = coerce_type_id(type_id)
        if aid <= 0 or tid is None or not self._valid:
            return ""
        return str((self._games.get(aid) or {}).get(tid) or "")

    def get(self, app_id: int | str, type_id: int | str | None) -> ModTypeDef | None:
        name = self.resolve_name(app_id, type_id)
        tid = coerce_type_id(type_id)
        aid = int(app_id or 0)
        if not name or tid is None:
            return None
        return ModTypeDef(app_id=aid, type_id=tid, name=name)

    def app_ids(self) -> list[int]:
        return sorted(self._games)

    def add_type(self, app_id: int | str, name: str) -> ModTypeDef:
        aid = int(app_id or 0)
        label = str(name or "").strip()
        if aid <= 0:
            raise ModTypeCatalogError("缺少游戏作用域，无法新增类型")
        if not label:
            raise ModTypeCatalogError("类型名称不能为空")
        if not self._valid:
            raise ModTypeCatalogError(self._error or "类型定义文件无效，拒绝写入")
        names = self._games.setdefault(aid, {})
        for existing in names.values():
            if existing.casefold() == label.casefold():
                raise ModTypeCatalogError(f"「{label}」已存在。")
        order = self._order.setdefault(aid, [])
        next_id = (max(names) + 1) if names else 1
        names[next_id] = label
        order.append(next_id)
        stamped = self._stamp_extension_type(aid, next_id, label)
        try:
            self._save()
        except Exception:
            names.pop(next_id, None)
            if next_id in order:
                order.remove(next_id)
            if stamped:
                self._extension_ids.pop(aid, None)
            raise
        return ModTypeDef(app_id=aid, type_id=next_id, name=label)

    def delete_type(self, app_id: int | str, type_id: int | str, db: Any) -> bool:
        """
        Remove a Type Definition and unbind every Mod in that game.

        SQLite is updated first so a crash cannot leave ``type_id`` pointing at
        a deleted definition. File write failure restores the previous bindings.
        """
        aid = int(app_id or 0)
        tid = coerce_type_id(type_id)
        if aid <= 0 or tid is None:
            return False
        if not self._valid:
            raise ModTypeCatalogError(self._error or "类型定义文件无效，拒绝删除")
        names = self._games.get(aid) or {}
        if tid not in names:
            if db is not None:
                db.clear_mods_type_id(aid, tid)
            return False
        affected = list(db.list_mod_ids_with_type(aid, tid)) if db is not None else []
        if db is not None:
            db.clear_mods_type_id(aid, tid)
        snapshot_names = dict(names)
        snapshot_order = list(self._order.get(aid) or [])
        prev_ext = self._extension_ids.get(aid)
        names.pop(tid, None)
        order = self._order.setdefault(aid, [])
        if tid in order:
            order.remove(tid)
        if prev_ext == tid:
            self._extension_ids.pop(aid, None)
        try:
            self._save()
        except Exception:
            self._games[aid] = snapshot_names
            self._order[aid] = snapshot_order
            if prev_ext == tid:
                self._extension_ids[aid] = prev_ext
            if db is not None:
                for mid in affected:
                    db.set_mod_type_id(mid, tid, touch_updated_at=False)
            raise
        return True

    def _save(self) -> None:
        if not self._valid:
            raise ModTypeCatalogError(self._error or "类型定义文件无效，拒绝保存")
        payload = _dump_catalog_payload(self._games, self._order, self._extension_ids)
        payload["legacy_migrated"] = bool(self._legacy_migrated)
        _parse_catalog_payload(payload)
        _atomic_write_json(self.path, payload)


def _parse_catalog_payload(
    raw: object,
) -> tuple[dict[int, dict[int, str]], dict[int, list[int]], dict[int, int]]:
    if not isinstance(raw, dict):
        raise ModTypeCatalogError("类型定义文件必须是 JSON 对象")
    version = raw.get("version", CATALOG_VERSION)
    try:
        ver = int(version)
    except (TypeError, ValueError) as exc:
        raise ModTypeCatalogError("类型定义 version 无效") from exc
    if ver != CATALOG_VERSION:
        raise ModTypeCatalogError(f"不支持的类型定义 version={ver}")
    games_raw = raw.get("games", {})
    if games_raw is None:
        games_raw = {}
    if not isinstance(games_raw, dict):
        raise ModTypeCatalogError("games 必须是对象")
    games: dict[int, dict[int, str]] = {}
    order: dict[int, list[int]] = {}
    extension_ids: dict[int, int] = {}
    for key, body in games_raw.items():
        try:
            aid = int(str(key).strip())
        except (TypeError, ValueError) as exc:
            raise ModTypeCatalogError(f"非法游戏键：{key!r}") from exc
        if aid <= 0:
            raise ModTypeCatalogError(f"非法游戏键：{key!r}")
        if not isinstance(body, dict):
            raise ModTypeCatalogError(f"游戏 {aid} 的类型定义必须是对象")
        types_raw = body.get("types", [])
        if types_raw is None:
            types_raw = []
        if not isinstance(types_raw, list):
            raise ModTypeCatalogError(f"游戏 {aid} 的 types 必须是数组")
        names: dict[int, str] = {}
        seq: list[int] = []
        seen: set[int] = set()
        for item in types_raw:
            if not isinstance(item, dict):
                raise ModTypeCatalogError(f"游戏 {aid} 含有非法类型条目")
            tid = coerce_type_id(item.get("id"))
            if tid is None:
                raise ModTypeCatalogError(f"游戏 {aid} 含有非法 type id")
            if tid in seen:
                raise ModTypeCatalogError(f"游戏 {aid} 存在重复 type id={tid}")
            label = str(item.get("name") or "").strip()
            if not label:
                raise ModTypeCatalogError(f"游戏 {aid} type id={tid} 名称为空")
            seen.add(tid)
            names[tid] = label
            seq.append(tid)
        games[aid] = names
        order[aid] = seq
        ext = coerce_type_id(body.get("extension_type_id"))
        if ext is not None and ext in names:
            extension_ids[aid] = ext
    return games, order, extension_ids


def _dump_catalog_payload(
    games: Mapping[int, Mapping[int, str]],
    order: Mapping[int, Iterable[int]],
    extension_ids: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    out_games: dict[str, Any] = {}
    stamps = extension_ids or {}
    for aid in sorted(games):
        names = games[aid]
        seq = list(order.get(aid) or names)
        types = []
        for tid in seq:
            label = names.get(tid)
            if label is None:
                continue
            types.append({"id": int(tid), "name": str(label)})
        entry: dict[str, Any] = {"types": types}
        ext = coerce_type_id(stamps.get(aid))
        if ext is not None and ext in names:
            entry["extension_type_id"] = int(ext)
        out_games[str(int(aid))] = entry
    return {"version": CATALOG_VERSION, "games": out_games}


def reconcile_orphan_type_ids(catalog: ModTypeCatalog, db: Any) -> int:
    """NULL every ``mods.type_id`` that has no Type Definition in its game."""
    if not catalog.is_valid():
        return 0
    app_ids = set(catalog.app_ids())
    try:
        app_ids.update(int(a) for a in db.list_app_ids_with_type_bindings())
    except Exception:  # noqa: BLE001
        logger.debug("list_app_ids_with_type_bindings failed", exc_info=True)
    total = 0
    for aid in sorted(app_ids):
        if aid <= 0:
            continue
        valid = {t.type_id for t in catalog.list_types(aid)}
        try:
            total += int(db.clear_orphan_mod_type_ids(aid, valid) or 0)
        except Exception:  # noqa: BLE001
            logger.debug("clear_orphan_mod_type_ids failed app_id=%s", aid, exc_info=True)
    return total


_INSTANCE: ModTypeCatalog | None = None


def get_mod_type_catalog() -> ModTypeCatalog:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ModTypeCatalog()
        try:
            _INSTANCE.reload()
        except ModTypeCatalogError:
            pass
    return _INSTANCE


def reset_mod_type_catalog(path: Path | None = None) -> ModTypeCatalog:
    global _INSTANCE
    _INSTANCE = ModTypeCatalog(path)
    try:
        _INSTANCE.reload()
    except ModTypeCatalogError:
        pass
    return _INSTANCE
