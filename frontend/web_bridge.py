"""WebChannel queries and explicit business commands; no generic execution capability."""
import base64
import json
import hashlib
from dataclasses import asdict
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
from urllib.parse import urlsplit
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from .description_view import render_description
from .acquisition import ACTIONS as ACQUISITION_ACTIONS, start as start_acquisition
from .deployment import ACTIONS as ORDER_ACTIONS, state as order_state, execute as execute_order
from .offline_registration import ACTIONS as OFFLINE_ACTIONS, preview as preview_offline, start as start_offline
from .workshop import ACTIONS as WORKSHOP_ACTIONS, scan as scan_workshop, start as start_workshop
from .registration import ACTIONS as REGISTRATION_ACTIONS, preview as registration_preview, execute as execute_registration, managed as registration_managed, source_options as registration_source_options
from .app_settings import state as app_settings_state, save as save_app_settings
from .mod_operations import ACTIONS as MOD_ACTIONS, execute as execute_mod_operation, pick as pick_mod_file, source_url as mod_source_url
from .game_configuration import ACTIONS as GAME_ACTIONS, state as game_config_state, pick as pick_game_directory, save as save_game_config, create as create_game
from .game_configuration import check_paths as check_game_paths
from .snapshots import ACTIONS as SNAPSHOT_ACTIONS, state as snapshot_state, execute as execute_snapshot
from .type_management import ACTIONS as TYPE_ACTIONS, state as type_state, execute as execute_type
from .asset_management import ACTIONS as ASSET_ACTIONS, state as asset_state, execute as execute_asset
from .asset_management import conflict_report
from .deregistration import state as removal_state,execute as deregister_mod
from .batch_management import ACTIONS as BATCH_ACTIONS, state as batch_state, execute as execute_batch


ORGANIZE_ACTIONS = {'tag-add', 'tag-remove', 'collection-create', 'collection-rename', 'collection-delete', 'collection-add', 'collection-remove','collection-cover-pick','collection-cover-member','collection-move'}


class _OfflineLauncher(QObject):
    """GUI-thread receiver deliberately not registered with WebChannel."""
    @Slot(str,object,object)
    def open(self,request_id,args,result):
        self.parent()._launch_offline(request_id,args,result)


class _CommandTask(QRunnable):
    def __init__(self, bridge, request_id, action, args):
        super().__init__()
        self.bridge, self.request_id, self.action, self.args = bridge, request_id, action, args

    def run(self):
        queued_open=False
        try:
            from contextlib import nullcontext
            with self.bridge.resources.bind() if self.bridge.resources else nullcontext():
                result = self.bridge.execute_command(self.action, self.args)
            if self.action=='open-offline':
                queued_open=True
                self.bridge.offline_ready.emit(self.request_id,self.args,result)
                return
            message = {'request': self.request_id, 'result': result}
        except Exception as exc:
            message = {'request': self.request_id, 'error': str(exc)}
        finally:
            if not queued_open:
                with self.bridge._lock:
                    self.bridge._busy.discard(self.args.get('_busy_key', self.args.get('internal_id')))
        self.bridge.response.emit(json.dumps(message, ensure_ascii=False))


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
    offline_ready = Signal(str,object,object)

    def __init__(self, adapter, parent=None, *, resources=None):
        super().__init__(parent)
        if not adapter.writable and not getattr(adapter.db, '_read_only', False):
            raise PermissionError('Web frontend requires a native read-only database')
        if adapter.writable:
            adapter._write()
        self.adapter = adapter
        if resources is not None and (not adapter.writable or resources.db is not adapter.db):
            raise PermissionError('部署上下文必须绑定同一隔离可写数据库')
        self.resources = resources
        self._directory_choices = {}
        self._custom_paths = {}
        self._jobs = {}
        self._acquisition = {}
        self._registration = {}
        self._workshop_scans = {}
        self._workshop_game = None
        self._order_errors = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self._lock = RLock()
        self._assets = {}
        self._requests = set()
        self._offline_game = None
        self._offline_previews = {}
        self._closed = False
        self._generation = 0
        self._game = None
        self._snapshots = {}
        self._busy = set()
        self._offline_launcher=_OfflineLauncher(self)
        self.offline_ready.connect(self._offline_launcher.open,Qt.QueuedConnection)

    def _launch_offline(self,request_id,args,result):
        try:
            with self._lock:
                if self._closed:raise RuntimeError('窗口正在关闭')
                self._command_asset(args)
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl
                with self.resources.bind():
                    from services.offline_view_cache import require_offline_view_path
                    path=self.resources.validate(result['path'])
                    if require_offline_view_path(path) is None:raise PermissionError('不是权威离线恢复缓存路径')
                if not path.is_file():raise FileNotFoundError('恢复后的离线页面不存在')
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):raise RuntimeError('系统浏览器未能打开离线页面')
            message={'request':request_id,'result':{'success':True,'message':'已在系统默认浏览器打开完整离线页面','internal_id':args['internal_id']}}
        except Exception as exc:message={'request':request_id,'error':str(exc)}
        finally:self._busy.discard(args.get('_busy_key',args.get('internal_id')))
        self.response.emit(json.dumps(message,ensure_ascii=False))

    @Slot(str, str, str)
    def request(self, request_id, operation, arguments):
        if self._closed:
            return
        try:
            if operation not in {'bootstrap', 'library', 'detail', 'cover', 'tags', 'acquisition','deployment','deployment-snapshots','registration-preview','workshop-scan','offline-preview','game-config','app-settings','game-path-check','mod-types','asset-management','conflict-report','batch-management','deregister-preview'}:
                raise PermissionError('Unsupported read capability')
            if len(arguments) > (400000 if operation in ('offline-preview','batch-management') else 2048) or len(request_id) > 100:
                raise ValueError('Request too large')
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError('Object arguments required')
            with self._lock:
                if len(self._requests) >= 32 or request_id in self._requests:
                    raise ValueError('Too many outstanding requests')
                self._requests.add(request_id)
                if operation == 'library':
                    self._generation += 1
                    self._game = str(args['game'])
                    args['_generation'] = self._generation
            self.pool.start(_Task(self, request_id, operation, args))
        except Exception as exc:
            self.response.emit(json.dumps({'request': request_id, 'error': str(exc)}))

    @Slot(str)
    def acknowledge(self, request_id):
        with self._lock:
            self._requests.discard(request_id)

    @Slot(str,str)
    def remember_session(self,game,space):
        # Session UI preferences are not Mod business commands. Only the normal
        # production window persists the original settings keys.
        if self._closed or not getattr(self.resources,'production',False) or not self.adapter.writable:return
        if game!=self._game or space not in ('library','deployment','acquire'):return
        from PySide6.QtCore import QSettings
        info=self.adapter.db.get_game(int(game))
        if info is None:return
        settings=QSettings('SteamModManager','WorkshopLibrary')
        settings.setValue('ui/game_filter',info.name)
        settings.setValue('ui/page_index',{'acquire':0,'library':1,'deployment':2}[space])

    @Slot(str, str, str)
    def command(self, request_id, action, arguments):
        accepted = False
        try:
            if self._closed:
                raise RuntimeError('窗口正在关闭')
            self.adapter._write()
            if action not in MOD_ACTIONS | ASSET_ACTIONS | BATCH_ACTIONS | TYPE_ACTIONS | GAME_ACTIONS | {'settings-save'} | ORGANIZE_ACTIONS | ACQUISITION_ACTIONS | ORDER_ACTIONS | SNAPSHOT_ACTIONS | REGISTRATION_ACTIONS | WORKSHOP_ACTIONS | OFFLINE_ACTIONS | {'favorite-set', 'metadata-save', 'deploy', 'redeploy', 'undeploy','mod-deregister'}:
                raise PermissionError('Unsupported business command')
            if len(arguments) > (400000 if action in ORDER_ACTIONS | WORKSHOP_ACTIONS | OFFLINE_ACTIONS | BATCH_ACTIONS else 70000) or len(request_id) > 100:
                raise ValueError('Request too large')
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError('Object arguments required')
            with self._lock:
                if action in BATCH_ACTIONS | (TYPE_ACTIONS-{'type-set'}) | GAME_ACTIONS | {'settings-save'} | ORGANIZE_ACTIONS | ACQUISITION_ACTIONS | ORDER_ACTIONS | SNAPSHOT_ACTIONS | WORKSHOP_ACTIONS | OFFLINE_ACTIONS | {'registration-save'}:
                    self._command_game(args)
                    if self.resources is None:
                        raise PermissionError('标签与合集需要明确的隔离资源上下文')
                    key = ('snapshot:' if action in SNAPSHOT_ACTIONS else 'order:' if action in ORDER_ACTIONS else 'acquire:' if action in ACQUISITION_ACTIONS | WORKSHOP_ACTIONS | OFFLINE_ACTIONS | {'registration-save'} else 'organize:') + (self._game or '')
                else:
                    self._command_asset(args)
                    key = args['internal_id']
                if action in WORKSHOP_ACTIONS | OFFLINE_ACTIONS and action!='pick-offline-pages' and (self._workshop_game is not None or self._offline_game is not None):
                    raise RuntimeError('已有批量注册／同步任务，请等待完成后再提交')
                if self._workshop_game==self._game or self._offline_game==self._game:
                    raise RuntimeError('当前游戏有批量注册／同步任务正在执行，其他写入请等待')
                if action in WORKSHOP_ACTIONS | OFFLINE_ACTIONS and any(a.internal_id in self._busy for a in self._assets.values()):
                    raise RuntimeError('存在未完成的 Mod 写入，请等待完成后再提交批量任务')
                args['_busy_key'] = key
                args['_request_id'] = request_id
                if action in ORDER_ACTIONS | SNAPSHOT_ACTIONS and (any(a.internal_id in self._busy for a in self._assets.values()) or 'acquire:'+self._game in self._busy):
                    raise RuntimeError('当前游戏存在未完成写入，请等待后刷新')
                if action not in ORDER_ACTIONS and 'order:'+self._game in self._busy:
                    raise RuntimeError('正在保存加载顺序，请等待')
                if key in self._busy or request_id in self._requests or len(self._requests) >= 32:
                    raise RuntimeError('操作正在执行，请等待')
                if action in {'deploy', 'redeploy', 'undeploy'} and self.resources is None:
                    raise PermissionError('缺少已准入的隔离部署资源上下文')
                self._busy.add(key)
                self._requests.add(request_id)
                accepted = True
            if action == 'open-source':
                asset,_=self._command_asset(args)
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl
                if not QDesktopServices.openUrl(QUrl(mod_source_url(self,asset))):raise RuntimeError('Windows 无法打开来源网页')
                self._busy.discard(args['_busy_key'])
                self.response.emit(json.dumps({'request':request_id,'result':{'success':True,'message':'已打开官方来源网页'}}))
            elif action in ('save-offline','refresh-metadata'):
                # Resolve existing system proxy outside the filesystem write scope.
                from services.proxy_resolver import resolve_proxy
                resolve_proxy()
                self.pool.start(_CommandTask(self,request_id,action,args))
            elif action in ('cover','import-offline','custom-deploy-pick'):
                if pick_mod_file(self,request_id,action,args):self.pool.start(_CommandTask(self,request_id,action,args))
            elif action == 'game-directory-pick':
                pick_game_directory(self,request_id,args)
            elif action=='collection-cover-pick':
                from PySide6.QtWidgets import QFileDialog,QApplication
                selected,_=QFileDialog.getOpenFileName(QApplication.activeWindow(),'选择合集封面','','Images (*.png *.jpg *.jpeg *.jfif *.webp)')
                self._command_game(args)
                if not selected:
                    self._busy.discard(args['_busy_key'])
                    self.response.emit(json.dumps({'request':request_id,'result':{'cancelled':True}}))
                else:
                    args['_selected_source']=str(self.resources.select_input(selected))
                    self.pool.start(_CommandTask(self,request_id,action,args))
            elif action in OFFLINE_ACTIONS:
                start_offline(self,request_id,action,args)
            elif action in WORKSHOP_ACTIONS:
                args['_full_sync']=action=='workshop-sync-all'
                start_workshop(self,request_id,args)
            elif action == 'content-open':
                with self._lock,self.resources.bind():
                    self._command_game(args)
                    _,path=registration_managed(self,args)
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    raise RuntimeError('无法打开已验证的托管目录')
                self._busy.discard(args['_busy_key'])
                self.response.emit(json.dumps({'request':request_id,'result':{'success':True,'managed_path':str(path),'message':'已打开当前 Mod 托管目录'}}))
            elif action in ACQUISITION_ACTIONS:
                start_acquisition(self,request_id,action,args)
            elif action in {'deploy', 'redeploy', 'undeploy'}:
                self._start_deploy(request_id, action, args)
            else:
                self.pool.start(_CommandTask(self, request_id, action, args))
        except Exception as exc:
            if accepted:
                with self._lock:
                    self._busy.discard(args['_busy_key'])
                    self._requests.discard(request_id)
                    if action in ACQUISITION_ACTIONS and not action.startswith('pick-import-'):
                        self._acquisition[request_id]={'id':request_id,'game':str(args['game']),'game_name':str(args['game']),'action':action,'name':str(args.get('title') or args.get('internal_id') or '未提交资源'),'state':'failed','message':str(exc)}
            self.response.emit(json.dumps({'request': request_id, 'error': str(exc)}, ensure_ascii=False))

    def _start_deploy(self, request_id, action, args):
        from services.deploy import ModDeployer
        from ui.deploy_thread import DeployWorker
        def preflight():
            with self._lock:
                if self._closed:
                    raise RuntimeError('窗口正在关闭')
                asset, info = self._command_asset(args)
            policy = self.inspector_actions(asset, info.source_url)
            choice = policy['undeploy'] if action == 'undeploy' else policy['primary']
            if not choice or choice['id'] != action or not choice['enabled']:
                raise PermissionError(choice['reason'] if choice else '部署状态不允许该操作')
        deployer = ModDeployer(library_root=self.resources.path('mod'), db=self.adapter.db,
                               resources=self.resources)
        worker = DeployWorker(args['internal_id'], self.resources.path('mod'), self,
                              action=action, deployer=deployer, resources=self.resources,
                              preflight=preflight)
        self._jobs[request_id] = worker
        def terminal(payload):
            with self._lock:
                self._busy.discard(args['internal_id'])
            success = bool(payload.get('success')) and payload.get('status') == 'SUCCESS'
            message = {'request': request_id}
            if success:
                message['result'] = {'internal_id': args['internal_id'], 'game': str(args['game']),
                    'snapshot': args['snapshot'], 'action': action, 'success': True, 'deployment': payload}
            else:
                message['error'] = str(payload.get('error') or '部署未成功')
            self.response.emit(json.dumps(message, ensure_ascii=False))
        worker.deploy_finished.connect(terminal)
        worker.deploy_failed.connect(terminal)
        worker.finished.connect(lambda: (self._jobs.pop(request_id, None), worker.deleteLater()))
        worker.start()

    def _command_asset(self, args):
        asset = self._assets.get(args.get('token'))
        if (asset is None or args.get('snapshot') != self._generation
                or self._snapshots.get(args.get('token')) != self._generation
                or str(args.get('game')) != self._game
                or args.get('internal_id') != asset.internal_id):
            raise RuntimeError('资产或游戏上下文已变化，请刷新后重试')
        info = self.adapter.verify(asset)
        if str(info.app_id) != self._game:
            raise RuntimeError('游戏身份不匹配')
        return asset, info

    def execute_command(self, action, args):
        # Recheck inside the worker, then retain both locks through the DAL write.
        # A game switch may invalidate a queued command but cannot rebind one.
        self.adapter._write()
        if action=='mod-deregister':return deregister_mod(self,args)
        if action in BATCH_ACTIONS:
            return execute_batch(self,action,args)
        if action in ASSET_ACTIONS:
            return execute_asset(self,action,args)
        if action in TYPE_ACTIONS:
            return execute_type(self,action,args)
        if action in MOD_ACTIONS:
            return execute_mod_operation(self,action,args)
        if action == 'settings-save':
            return save_app_settings(self,args)
        if action == 'game-create':
            return create_game(self,args)
        if action == 'game-config-save':
            return save_game_config(self,args)
        if action in REGISTRATION_ACTIONS:
            return execute_registration(self,action,args)
        if action in SNAPSHOT_ACTIONS:
            return execute_snapshot(self,action,args)
        if action in ORDER_ACTIONS:
            return execute_order(self,action,args)
        if action in ORGANIZE_ACTIONS:
            return self.execute_organize(action, args)
        with self._lock, self.adapter.db._lock:
            if self._closed:
                raise RuntimeError('窗口正在关闭')
            asset, info = self._command_asset(args)
            payload = {'display_name': info.user_display_name,
                       'custom_description': info.custom_description,
                       'user_notes': info.user_notes, 'favorite': info.favorite}
            if action == 'favorite-set':
                if type(args.get('value')) is not bool:
                    raise ValueError('收藏状态必须为布尔值')
                payload['favorite'] = args['value']
            elif action == 'metadata-save':
                fields = args.get('fields')
                limits = {'display_name': 300, 'custom_description': 50000, 'user_notes': 10000, 'category': 200,
                          'platform':30,'source_url':4000,'custom_deploy_path':4000,'game_version':30}
                if not isinstance(fields, dict) or not {'display_name','custom_description','user_notes'}<=set(fields) or set(fields)-set(limits):
                    raise ValueError('编辑字段不符合白名单')
                for key, value in fields.items():
                    limit=limits[key]
                    if not isinstance(fields[key], str) or len(fields[key]) > limit:
                        raise ValueError('编辑内容超出允许长度')
                if 'platform' in fields:
                    from core.mod_platform import normalize_platform_if_known,get_available_sources
                    game=self.adapter.db.get_game(info.app_id)
                    if normalize_platform_if_known(fields['platform']) not in {p for p,label in get_available_sources(game.name,info.app_id)}:raise ValueError('该游戏不支持所选来源')
                if fields.get('source_url'):
                    url=urlsplit(fields['source_url'])
                    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or any(ord(c)<32 for c in fields['source_url']):raise ValueError('来源地址必须是合法 HTTP/HTTPS URL')
                if 'game_version' in fields:
                    from core.witcher3_game_version import is_witcher3_game,validate_witcher3_game_version
                    if not is_witcher3_game('',info.app_id):raise ValueError('仅 Witcher 3 支持版本字段')
                    validate_witcher3_game_version(fields['game_version'])
                custom=fields.get('custom_deploy_path',info.custom_deploy_path)
                if custom and custom!=info.custom_deploy_path:
                    from services.business_resources import checked_resource_path
                    path=Path(custom);checked_resource_path(path.resolve(),path)
                    if not path.is_dir() or str(path.resolve())!=self._custom_paths.get(asset.internal_id):raise ValueError('请通过目录选择器指定自定义部署目录')
                payload.update(fields)
            else:
                raise PermissionError('Unsupported business command')
            self.adapter.db.update_mod_user_metadata(asset.mod_pk, payload)
            if action=='metadata-save' and self.resources is not None:
                with self.resources.bind():
                    try:
                        folder=self.adapter.folder(asset)
                    except FileNotFoundError:
                        from services.mod_presence import persist_entity_metadata_to_backup
                        persist_entity_metadata_to_backup(asset.mod_pk,db=self.adapter.db)
                    else:
                        from services.info_sidecar import write_sidecar_for_mod
                        if write_sidecar_for_mod(folder,asset.mod_pk,db=self.adapter.db) is None:
                            raise RuntimeError('数据库已保存，但元数据侧车未保存；请重新读取并重试')
            return {'internal_id': asset.internal_id, 'game': self._game,
                    'snapshot': self._generation, 'action': action, 'success': True}

    def _command_game(self, args):
        if (self._closed or args.get('snapshot') != self._generation
                or str(args.get('game')) != self._game
                or self._game not in {g.key for g in self.adapter.games()}):
            raise RuntimeError('游戏或资产快照已变化，请刷新后重试')

    @staticmethod
    def _version(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def collection_version(self, record):
        return self._version([asdict(record), sorted(self.adapter.db.list_collection_member_ids(record.collection_id))])

    def tag_state(self, asset):
        tags = self.adapter.db.get_category_tags(asset.mod_pk)
        return {'tags': tags, 'available': self.adapter.db.list_all_category_tags(),
                'version': self._version(sorted(tags)), 'internal_id': asset.internal_id}

    def execute_organize(self, action, args):
        from services import collection
        self.adapter._write()
        if self.resources is None:
            raise PermissionError('缺少标签与合集的隔离资源上下文')
        db = self.adapter.db
        with self._lock, db._lock, self.resources.bind():
            self._command_game(args)
            try:
                changed = 1
                result = {'game': self._game, 'action': action}
                if action in ('tag-add', 'tag-remove'):
                    asset, _ = self._command_asset(args)
                    state = self.tag_state(asset)
                    if args.get('version') != state['version']:
                        raise RuntimeError('标签已经变化，请关闭后重新打开标签管理')
                    label = args.get('label')
                    if (not isinstance(label, str) or not label.strip() or len(label) > 80
                            or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                        raise ValueError('标签须为 1–80 个字符，不能包含控制字符')
                    label = label.strip()
                    if action == 'tag-add':
                        changed = int(label not in state['tags'])
                        if changed:
                            db.add_mod_tag(asset.mod_pk, 'category', label)
                    else:
                        changed = db.remove_mod_tag(asset.mod_pk, 'category', label)
                    result.update(self.tag_state(asset))
                elif action == 'collection-create':
                    name = self._collection_name(args.get('name'))
                    record = collection.create_collection(self._game, name, db=db)
                    result['collection_id'] = str(record.collection_id)
                elif action=='collection-move':
                    records=collection.list_collections(self._game,db=db)
                    signature=[[str(c.collection_id),self.collection_version(c)] for c in records]
                    if args.get('versions')!=signature:raise RuntimeError('合集列表已变化，请刷新后重试')
                    ids=[str(c.collection_id) for c in records]
                    if args.get('source') not in ids or args.get('target') not in ids:raise ValueError('合集不属于当前游戏')
                    order=collection.move_collection_in_order(ids,args['source'],args['target'])
                    collection.reorder_collections(self._game,order,db=db)
                elif action in ('collection-rename', 'collection-delete', 'collection-add', 'collection-remove','collection-cover-pick','collection-cover-member'):
                    record = collection.get_collection(args.get('collection_id'), db=db)
                    if record is None or str(record.app_id) != self._game:
                        raise ValueError('合集不存在或不属于当前游戏')
                    if args.get('version') != self.collection_version(record):
                        raise RuntimeError('合集已经变化，请刷新后重试')
                    if action == 'collection-rename':
                        collection.rename_collection(record.collection_id, self._collection_name(args.get('name')), db=db)
                    elif action=='collection-cover-pick':
                        collection.set_collection_cover(record.collection_id,self.resources.validate(args['_selected_source']),db=db)
                    elif action=='collection-cover-member':
                        iid=args.get('internal_id')
                        if not isinstance(iid,str) or str(UUID(iid))!=iid:raise ValueError('成员必须使用冻结 UUID')
                        collection.set_collection_cover_from_member(record.collection_id,iid,db=db)
                    elif action == 'collection-delete':
                        if args.get('confirm') is not True:
                            raise ValueError('请确认仅删除合集及组织关系')
                        collection.delete_collection(record.collection_id, db=db)
                    else:
                        ids = args.get('internal_ids')
                        if not isinstance(ids, list) or not ids or len(ids) > 500:
                            raise ValueError('请选择 1–500 个同游戏 Mod')
                        for token in ids:
                            if not isinstance(token, str) or str(UUID(token)) != token:
                                raise ValueError('成员必须使用冻结 UUID')
                            pk = db.find_mod_by_internal_id(token)
                            info = db.get_mod_deploy_info(pk) if pk else None
                            if info is None or str(info.app_id) != self._game:
                                raise ValueError('不能使用其他游戏或失效的 Mod')
                        method = collection.add_mods_to_collection if action == 'collection-add' else collection.remove_mods_from_collection
                        changed = method(record.collection_id, ids, db=db)
                    result['collection_id'] = str(record.collection_id)
                else:
                    raise PermissionError('Unsupported organisation command')
                return {**result, 'success': True, 'changed': changed}
            except Exception:
                db._conn.rollback()
                raise

    @staticmethod
    def _collection_name(value):
        if (not isinstance(value, str) or not value.strip() or len(value) > 100
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('合集名称须为 1–100 个字符，不能包含控制字符')
        return value.strip()

    def inspector_actions(self, asset, source_url):
        # Use the raw persisted fact: DAL list/display fallbacks turn empty status
        # into not_deployed. That fallback must not choose an operation slot.
        with self.adapter.db._lock:
            row = self.adapter.db._conn.execute(
                "SELECT internal_id, deploy_status, identity_status, content_status, "
                "is_invalid, conflict_status, platform FROM mods WHERE mod_id = ?",
                (asset.mod_pk,),
            ).fetchone()
        state = str(row['deploy_status'] or '') if row else ''
        source_platform=str(row['platform'] if row else asset.platform).lower()
        identity_ok = row is not None and str(row['internal_id']) == asset.internal_id
        try:
            UUID(asset.internal_id)
        except ValueError:
            identity_ok = False
        if not identity_ok:
            state = 'unknown'
        reason = '只读预览：未开放可写业务桥接'
        if not identity_ok or str(row['identity_status'] or '') not in ('', 'ok'):
            reason = '身份异常，不能执行部署；' + reason
        elif row['content_status'] == 'content_missing':
            reason = '内容缺失，不能执行部署；' + reason
        elif row['is_invalid'] or str(row['conflict_status'] or 'none') != 'none':
            reason = '存在异常或冲突标记，部署前置条件尚未验证；' + reason

        def action(key, label, icon, why):
            if self.adapter.writable:
                why = '该操作尚未完成可写迁移' if '只读预览' in why else why
            if key=='mod-deregister' and self.resources is not None and self.adapter.writable and identity_ok:
                return {'id':key,'label':label,'icon':icon,'enabled':True,'reason':''}
            if key in ('asset-management','tags','content-add','content-rescan','open-folder') and self.resources is not None and self.adapter.writable and identity_ok:
                return {'id': key, 'label': label, 'icon': icon, 'enabled': True, 'reason': ''}
            if key in MOD_ACTIONS and self.adapter.writable and self.resources is not None and identity_ok:
                why=''
                try:
                    if key=='open-source':mod_source_url(self,asset)
                    elif key=='open-offline':
                        from services.offline.paths import resolve_offline_page
                        from services.info_asset_runtime import probe_offline_open
                        if not resolve_offline_page(Path(asset.managed_path)) and not (lambda p: p.cache_hit or p.can_materialize)(probe_offline_open(asset.managed_path,mod_id=str(asset.mod_pk))):
                            why='未保存离线页面'
                    elif key=='save-offline' and source_platform not in ('steam','github','mod.io','modio'):why='此来源没有离线保存服务'
                    elif key=='import-offline' and source_platform!='nexus':why='旧版手动关联 HTML/MHTML 仅支持 Nexus'
                    elif key=='refresh-metadata':
                        platform=source_platform
                        if platform not in ('nexus','github','other','mod.io','modio','steam'):why='此来源没有原版元数据刷新服务'
                    if key not in ('open-source','cover') and not Path(asset.managed_path).is_dir():why='托管目录缺失，需先补齐资源'
                except Exception as exc:why=str(exc)
                return {'id':key,'label':label,'icon':icon,'enabled':not bool(why),'reason':why}
            if key == 'edit' and self.adapter.writable and identity_ok:
                return {'id': key, 'label': label, 'icon': icon, 'enabled': True, 'reason': ''}
            return {'id': key, 'label': label, 'icon': icon, 'enabled': False, 'reason': why}

        deploy_allowed = False
        strategy_approved = False
        deploy_reason = '缺少已准入的隔离部署资源上下文'
        if self.resources is not None and identity_ok:
            try:
                with self.resources.bind():
                    self.resources.validate_deploy(asset.internal_id, scan=False, require_payload=False)
                    from services.mod_presence import deployment_capability
                    capability = deployment_capability(asset.mod_pk, db=self.adapter.db)
                    deploy_allowed = capability.allowed
                    deploy_reason = capability.reason or ''
                strategy_approved = True
                if deploy_allowed:
                    with self.resources.bind():
                        self.resources.validate_deploy(asset.internal_id, scan=False)
                if str(row['identity_status'] or '') not in ('', 'ok') or (row['content_status'] == 'content_missing' and capability.source_kind!='workshop') or row['is_invalid'] or str(row['conflict_status'] or 'none') != 'none':
                    deploy_allowed, deploy_reason = False, '存在身份异常、失效或冲突标记'
            except Exception as exc:
                deploy_allowed = False
                deploy_reason = str(exc)

        def deploy_action(key, label, glyph):
            enabled = self.adapter.writable and strategy_approved and identity_ok and (key == 'undeploy' or deploy_allowed)
            return {'id': key, 'label': label, 'icon': glyph, 'enabled': enabled,
                    'reason': '' if enabled else deploy_reason if self.adapter.writable else reason}

        if state == 'deployed':
            primary = deploy_action('redeploy', '重新部署', 'refresh-cw')
        elif state == 'not_deployed':
            primary = deploy_action('deploy', '部署', 'play')
        elif state == 'failed':
            # A confirmed failed command may be retried through the original
            # Deployer's recovery checks. Unknown status remains non-actionable.
            primary = deploy_action('deploy', '重试部署', 'refresh-cw')
        else:
            primary = action('unconfirmed', '状态异常' if state == 'failed' else '状态待确认',
                             'circle-alert', '部署状态未确认，不能默认按未部署处理；' + reason)
        try:
            url = str(source_url or '').strip()
            parsed = urlsplit(url)
            valid_url = (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                         and not parsed.username and not parsed.password
                         and not any(ord(c) < 32 or ord(c) == 127 for c in url))
        except ValueError:
            valid_url = False
        opening = '只读预览：尚未准入安全外部打开桥接'
        platform = str(row['platform'] or '').lower() if row else ''
        import_only = platform in ('nexus', 'other')
        return {'state': state, 'primary': primary,
                'undeploy': deploy_action('undeploy', '取消部署', 'package-minus') if state == 'deployed' else None,
                'edit': action('edit', '编辑', 'pencil', '只读预览：编辑信息会写入 SQLite'),
                'menu': [
                    action('open-source', '打开来源网页', 'external-link', opening if valid_url else '来源 URL 未通过校验；' + opening),
                    action('open-folder', '打开托管归档目录', 'folder', '目录身份及路径未准入打开校验；' + opening),
                    action('open-offline', '打开离线页面', 'file-text', '离线打开可能触发 CAS 资源物化；当前只读桥接未开放'),
                    *([action('import-offline' if source_platform=='nexus' else 'save-offline', '导入离线网页' if source_platform=='nexus' else '保存离线网页', 'upload' if source_platform=='nexus' else 'download', '只读预览：离线保存或导入会写入资源')] if source_platform in ('nexus','steam','github','mod.io','modio') else []),
                    action('content-add', '补齐本地内容', 'package', '只读模式：不允许归档写入'),
                    action('content-rescan', '重新识别内容', 'refresh', '只读模式：不允许更新内容状态'),
                    action('asset-management', '文件、关系与用户标记', 'layers', '只读模式：不可修改文件、关系或标记'),
                    action('tags', '管理标签', 'tags', '只读预览：标签管理会写入 SQLite'),
                    action('cover', '更换封面', 'image', '只读预览：更换封面会写入封面和元数据'),
                    action('refresh-metadata', '刷新官方元数据' if source_platform=='steam' else '刷新本地与来源信息' if source_platform in ('mod.io','modio') else '刷新本地元数据', 'refresh-cw', '只读预览：刷新会写入元数据'),
                    action('mod-deregister','从 SMM 注销 Mod…','circle-alert','只读模式：不可注销记录'),
                ]}

    def execute(self, operation, args):
        if operation=='deregister-preview':
            with self._lock,self.adapter.db._lock:return removal_state(self,args)
        if operation=='conflict-report':
            with self._lock,self.adapter.db._lock:
                return conflict_report(self,args)
        if operation=='game-path-check':
            with self._lock,self.adapter.db._lock:
                return check_game_paths(self,args)
        if operation=='batch-management':
            with self._lock,self.adapter.db._lock:
                return batch_state(self,args)
        if operation=='asset-management':
            with self._lock,self.adapter.db._lock:
                return asset_state(self,args)
        if operation=='mod-types':
            with self._lock,self.adapter.db._lock:
                return type_state(self,args)
        if operation=='offline-preview':
            with self._lock,self.adapter.db._lock:
                return preview_offline(self,args)
        if operation=='workshop-scan':
            with self._lock,self.adapter.db._lock:
                return scan_workshop(self,args)
        if operation=='registration-preview':
            with self._lock,self.adapter.db._lock:
                return registration_preview(self,args)
        if operation=='deployment-snapshots':
            with self._lock,self.adapter.db._lock:
                # Native readonly connections intentionally deny transaction commands.
                # data_version detects commits by other connections without weakening that guard.
                for _ in range(2):
                    before = self.adapter.db._conn.execute('PRAGMA data_version').fetchone()[0]
                    result = snapshot_state(self,args)
                    if before == self.adapter.db._conn.execute('PRAGMA data_version').fetchone()[0]:
                        return result
                raise RuntimeError('数据库正在变化，请刷新后重试')
        if operation=='deployment':
            with self._lock,self.adapter.db._lock:
                return order_state(self,args)
        if operation == 'app-settings':
            return app_settings_state(self)
        if operation == 'game-config':
            with self._lock,self.adapter.db._lock:
                return game_config_state(self,args)
        if operation == 'acquisition':
            with self._lock, self.adapter.db._lock:
                game_config=self.adapter.db.get_game_deploy_config(int(self._game or 0))
                eligible={str(row[0]) for row in self.adapter.db._conn.execute(
                    "SELECT mod_id FROM mods WHERE app_id=? AND LOWER(platform)='steam' AND COALESCE(official_metadata_synced,0)=0",(int(self._game or 0),))}
                candidates=[{'token':token,'internal_id':asset.internal_id,'name':asset.name} for token,asset in self._assets.items() if str(asset.mod_pk) in eligible and self._snapshots.get(token)==self._generation]
                return {'game':self._game,'candidates':candidates,
                    'tasks':[dict(job) for job in self._acquisition.copy().values()],
                    'incoming':str(self.resources.path('incoming')) if self.resources else '',
                    'archive_root':str(self.resources.path('mod')) if self.resources else '',
                    'import_allowed':bool(self.resources and self.adapter.writable),
                    'workshop_path':game_config.workshop_path if game_config else '',
                    'sync_public':bool(self.resources and self.resources.steam_public),
                    'sync_allowed':bool(self.resources and (self.resources.steam_api_url or self.resources.steam_public) and self.adapter.writable)}
        if operation == 'bootstrap':
            from PySide6.QtCore import QSettings
            preferences=QSettings('SteamModManager','WorkshopLibrary')
            session={'game_name':preferences.value('ui/game_filter','',str),'page':preferences.value('ui/page_index',1,int)} if getattr(self.resources,'production',False) else {}
            return {'games': [asdict(g) for g in self.adapter.games()], 'readOnly': not self.adapter.writable,
                    'session':session,
                    'registrationSources': registration_source_options(self), 'deployCommands': self.resources is not None, 'organizeCommands': self.resources is not None, 'production': bool(self.resources and type(self.resources).__name__=='ProductionResources')}
        if operation == 'library':
            game = int(args['game'])
            with self._lock:
                if '_generation' not in args:
                    self._generation += 1
                    self._game = str(game)
                generation = args.get('_generation', self._generation)
            if str(game) not in {g.key for g in self.adapter.games()}:
                raise LookupError('Unknown game')
            assets = self.adapter.assets(game)
            # Same DAL snapshot contract; no size scan, taxonomy migration or resolver writes.
            rows = {int(r['mod_pk']): r for r in self.adapter.db.list_mod_list_items(game_id=game)}
            from .library_query_adapter import query_projection
            query_data=query_projection(rows,self.adapter.db.get_mods_search_fields(rows),
                                        self.adapter.db.get_mods_tag_flags(rows))
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
                    'type_id': row.get('type_id'),
                    'type': types.get(row.get('type_id'), '类型未知'),
                    'size': row.get('local_size_bytes') if row.get('local_size_status') == 'ok' else None,
                    'deployed': asset.deployed, 'favorite': asset.favorite, 'status': asset.status,
                    **query_data[asset.mod_pk]})
            collections = []
            for collection in self.adapter.collections(game):
                members = self.adapter.members(collection.collection_id)
                collections.append({'id': str(collection.collection_id), 'name': collection.name,
                    'cover':self.collection_cover_preview(collection),
                    'version': self.collection_version(collection),
                    'tokens': [t for t, a in bindings.items() if str(a.mod_pk) in members]})
            with self._lock:
                self._assets.update(bindings)
                self._snapshots.update({token: generation for token in bindings})
                # Retain one prior snapshot for in-flight readers, bound long-session memory.
                if len(self._assets) > max(10000, len(bindings) * 2):
                    self._assets = bindings
                    self._snapshots = {token: generation for token in bindings}
            return {'assets': result, 'collections': collections, 'snapshot': generation}
        with self._lock:
            asset = self._assets.get(args.get('token'))
        if asset is None:
            raise LookupError('Expired asset snapshot; refresh the library')
        if operation == 'tags':
            self._command_asset(args)
            return self.tag_state(asset)
        if operation == 'detail':
            info = self.adapter.detail(asset)
            return {'html': render_description(info.description), 'description': info.description,
                    'source': info.description_source, 'internal_id': asset.internal_id,
                    'workspace_id': asset.workspace_id, 'mod_pk': asset.mod_pk,
                    'source_url': info.source_url, 'path': asset.managed_path,
                    'notes': info.user_notes, 'files': info.mod_files_json,
                    'editable': {'display_name': info.user_display_name or '',
                                 'custom_description': info.custom_description or '',
                                 'user_notes': info.user_notes or '', 'category': info.category or ''},
                    'advanced':{'platform':info.platform,'source_url':info.source_url or '',
                                'custom_deploy_path':info.custom_deploy_path or '',
                                'game_version':info.game_version or '',
                                'witcher3':int(info.app_id)==292030},
                    'type_id': self.adapter.db.get_mod_type_id(asset.mod_pk),
                    'tags': self.adapter.db.get_category_tags(asset.mod_pk),
                    'actions': self.inspector_actions(asset, info.source_url)}
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

    def collection_cover_preview(self,record):
        if not record.cover_path:return None
        from services.collection_cover import relative_collection_cover_path
        from services.business_resources import checked_resource_path
        relative=str(record.cover_path).replace('\\','/')
        if relative!=relative_collection_cover_path(record.collection_id,Path(relative).suffix):return None
        path=checked_resource_path(Path(self.adapter.db.db_path).parent,Path(self.adapter.db.db_path).parent/relative)
        if not path.is_file():return None
        image=QImage(str(path))
        if image.isNull():return None
        image=image.scaled(320,160,Qt.KeepAspectRatio,Qt.SmoothTransformation)
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer,'JPEG',80):return None
        return 'data:image/jpeg;base64,'+base64.b64encode(bytes(buffer.data())).decode('ascii')

    def shutdown(self):
        self._closed = True
        self.pool.clear()
        # Do not kill a deployment thread midway through copy/restore. Refuse
        # the close while it finishes against its original frozen identity.
        jobs_idle = all(worker.wait(50) for worker in list(self._jobs.values()))
        return self.pool.waitForDone(5000) and jobs_idle
