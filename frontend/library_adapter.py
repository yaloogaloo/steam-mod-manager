"""Explicit DAL boundary for the new Library. No scanning or repair on reads."""
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from core.db_manager import DatabaseManager
from .models import GameContext


@dataclass(frozen=True)
class Asset:
    internal_id: str
    workspace_id: str
    mod_pk: int
    managed_path: str
    name: str
    platform: str
    deployed: bool
    favorite: bool
    category: str
    cover: str
    status: str


class LibraryAdapter:
    def __init__(self, db: DatabaseManager, *, writable: bool = False):
        if writable and getattr(db, '_read_only', False):
            raise PermissionError('只读数据库不能绑定可写 Adapter')
        self.db = db
        self.writable = writable

    def games(self):
        with self.db._lock:
            games=self.db.list_games()
            unassigned=self.db._conn.execute('SELECT 1 FROM mods WHERE app_id=0 LIMIT 1').fetchone()
            empty=not any(int(g.app_id)!=0 for g in games)
            return [GameContext(str(g.app_id), g.name or '未归属资产', str(g.app_id)) for g in games
                    if int(g.app_id)!=0 or unassigned or empty]

    def assets(self, app_id: int):
        result = []
        for row in self.db.list_mod_list_items(game_id=app_id):
            if int(row['game_id']) != app_id:
                continue
            identity = str(row['internal_id'])
            try:
                UUID(identity)
                identity_error = False
            except ValueError:
                identity_error = True
            result.append(Asset(identity, row['workspace_id'], int(row['mod_pk']),
                row['managed_path'], row['name'], row['platform'], row['deployed'],
                row['favorite'], str(row.get('category') or ''), row['cover_path'],
                '身份异常' if identity_error else '待补齐内容' if row['content_status'] == 'content_missing' else
                '身份异常' if row['identity_status'] not in ('', 'ok') else
                '已标记异常' if row['invalid'] else '已标记冲突' if row['conflict'] else
                '已禁用' if not row['enabled'] else '已标记停更' if row['abandoned'] else
                '目录缺失' if row['folder_absent'] else ''))
        return result

    def verify(self, asset: Asset):
        UUID(asset.internal_id)
        if str(self.db.find_mod_by_internal_id(asset.internal_id)) != str(asset.mod_pk):
            raise RuntimeError('Mod 身份已变化，请刷新资产库')
        info = self.db.get_mod_display_info(asset.mod_pk)
        if info is None:
            raise LookupError('Mod 已不存在')
        return info

    def folder(self, asset):
        self.verify(asset)
        from services.path_lifecycle import _folder_proves_mod_entity
        path = Path(asset.managed_path)
        if not asset.managed_path or not path.is_dir():
            raise FileNotFoundError('Mod 目录不存在')
        if not _folder_proves_mod_entity(path, mod_id=str(asset.mod_pk), db=self.db):
            raise RuntimeError('目录身份无法确认，请在旧版检查路径；新版不会自动重绑定')
        return path.resolve()

    def detail(self, asset: Asset):
        # Corrupt identities remain inspectable by their already-resolved DAL PK.
        # They must still fail verify() before any command.
        info = self.db.get_mod_display_info(asset.mod_pk)
        if info is None:
            raise LookupError('Mod 已不存在')
        from .readonly_metadata import resolve_detail
        return resolve_detail(self.db, asset, info)

    def _write(self):
        if not self.writable:
            raise PermissionError('当前为只读模式')
        from services.mutation_context import assert_mutation_allowed
        assert_mutation_allowed(self.db.db_path)

    def favorite(self, asset: Asset):
        self._write()
        info = self.verify(asset)
        self.db.update_mod_user_metadata(asset.mod_pk, {
            'display_name': info.user_display_name,
            'custom_description': info.custom_description,
            'user_notes': info.user_notes, 'favorite': not info.favorite})

    def collections(self, app_id):
        from services.collection import list_collections
        return list_collections(app_id, db=self.db)

    def members(self, collection_id):
        return set(self.db.list_collection_member_ids(collection_id))

    def create_collection(self, app_id, name):
        self._write()
        from services.collection import create_collection
        return create_collection(app_id, name, db=self.db)

    def add_members(self, collection_id, assets):
        self._write()
        collection = self.db.get_collection(collection_id)
        if collection is None:
            raise LookupError('合集已不存在')
        for asset in assets:
            if self.verify(asset).app_id != collection.app_id:
                raise ValueError('合集成员必须属于同一游戏')
        # Existing DAL explicitly takes resolved SQLite FK handles.
        return self.db.add_mods_to_collection(collection_id, [a.mod_pk for a in assets])

    def cover_file(self, asset):
        from .readonly_metadata import resolve_cover
        return resolve_cover(self.db, asset)
