"""Read-only WebChannel capability API. Browser receives tokens, never paths to open."""
import base64
import json
from dataclasses import asdict
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from .description_view import render_description


class _Task(QRunnable):
    def __init__(self, bridge, request_id, operation, args):
        super().__init__()
        self.bridge, self.request_id, self.operation, self.args = bridge, request_id, operation, args

    def run(self):
        try:
            result = self.bridge.execute(self.operation, self.args)
            message = {'request': self.request_id, 'result': result}
        except Exception as exc:
            message = {'request': self.request_id, 'error': str(exc)}
        self.bridge.response.emit(json.dumps(message, ensure_ascii=False))


class LibraryBridge(QObject):
    response = Signal(str)

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        if adapter.writable or not getattr(adapter.db, '_read_only', False):
            raise PermissionError('Web frontend requires a native read-only database')
        self.adapter = adapter
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self._lock = RLock()
        self._assets = {}
        self._requests = set()
        self._closed = False

    @Slot(str, str, str)
    def request(self, request_id, operation, arguments):
        if self._closed:
            return
        try:
            if operation not in {'bootstrap', 'library', 'detail', 'cover'}:
                raise PermissionError('Unsupported read capability')
            if len(arguments) > 2048 or len(request_id) > 100:
                raise ValueError('Request too large')
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError('Object arguments required')
            with self._lock:
                if len(self._requests) >= 32 or request_id in self._requests:
                    raise ValueError('Too many outstanding requests')
                self._requests.add(request_id)
            self.pool.start(_Task(self, request_id, operation, args))
        except Exception as exc:
            self.response.emit(json.dumps({'request': request_id, 'error': str(exc)}))

    @Slot(str)
    def acknowledge(self, request_id):
        with self._lock:
            self._requests.discard(request_id)

    def execute(self, operation, args):
        if operation == 'bootstrap':
            return {'games': [asdict(g) for g in self.adapter.games()], 'readOnly': True}
        if operation == 'library':
            game = int(args['game'])
            if str(game) not in {g.key for g in self.adapter.games()}:
                raise LookupError('Unknown game')
            assets = self.adapter.assets(game)
            # Same DAL snapshot contract; no size scan, taxonomy migration or resolver writes.
            rows = {int(r['mod_pk']): r for r in self.adapter.db.list_mod_list_items(game_id=game)}
            types = {}
            catalog = Path(self.adapter.db.db_path).parent / 'mod_types.json'
            if catalog.is_file():
                try:
                    raw = json.loads(catalog.read_text('utf-8'))
                    types = {t['id']: t['name'] for t in raw.get('games', {}).get(str(game), {}).get('types', [])}
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            result, bindings = [], {}
            for asset in assets:
                token = token_urlsafe(18)
                bindings[token] = asset
                row = rows[asset.mod_pk]
                result.append({'token': token, 'internal_id': asset.internal_id,
                    'workspace_id': asset.workspace_id, 'name': asset.name,
                    'platform': {'steam': 'Steam', 'nexus': 'Nexus Mods', 'github': 'GitHub',
                                 'modio': 'mod.io', 'other': 'Other'}.get(asset.platform.lower(), asset.platform) or '来源未知',
                    'category': asset.category,
                    'type': types.get(row.get('type_id'), '类型未知'),
                    'size': row.get('local_size_bytes') if row.get('local_size_status') == 'ok' else None,
                    'deployed': asset.deployed, 'favorite': asset.favorite, 'status': asset.status})
            collections = []
            for collection in self.adapter.collections(game):
                members = self.adapter.members(collection.collection_id)
                collections.append({'id': str(collection.collection_id), 'name': collection.name,
                    'tokens': [t for t, a in bindings.items() if str(a.mod_pk) in members]})
            with self._lock:
                self._assets.update(bindings)
                # Retain one prior snapshot for in-flight readers, bound long-session memory.
                if len(self._assets) > max(10000, len(bindings) * 2):
                    self._assets = bindings
            return {'assets': result, 'collections': collections}
        with self._lock:
            asset = self._assets.get(args.get('token'))
        if asset is None:
            raise LookupError('Expired asset snapshot; refresh the library')
        if operation == 'detail':
            info = self.adapter.detail(asset)
            return {'html': render_description(info.description), 'description': info.description,
                    'source': info.description_source, 'internal_id': asset.internal_id,
                    'workspace_id': asset.workspace_id, 'mod_pk': asset.mod_pk,
                    'source_url': info.source_url, 'path': asset.managed_path,
                    'notes': info.user_notes, 'files': info.mod_files_json}
        if operation == 'cover':
            path = self.adapter.cover_file(asset)
            if path is None:
                return None
            image = QImage(str(path))
            if image.isNull():
                raise ValueError('封面文件无法解码')
            image = image.scaled(640, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            if not image.save(buffer, 'JPEG', 85):
                raise ValueError('封面预览编码失败')
            return 'data:image/jpeg;base64,' + base64.b64encode(bytes(buffer.data())).decode('ascii')
        raise PermissionError('Unsupported read capability')

    def shutdown(self):
        self._closed = True
        self.pool.clear()
        return self.pool.waitForDone(5000)
