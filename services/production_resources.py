"""Production paths from the existing QSettings/DAL contracts, with scoped writes."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock

from .business_resources import BusinessResources, checked_resource_path, resolve_steam_addresses


_source_operation=ContextVar('smm_production_source_operation',default='')
_projection_operation=ContextVar('smm_production_projection_operation',default=None)
_deployment_operation=ContextVar('smm_production_deployment_operation',default=None)


class ProductionResources(BusinessResources):
    production=True

    @contextmanager
    def deployment_scope(self,internal_id):
        """Bind the existing Deployer's authoritative roots, never UI-made paths."""
        from services.deploy import ModDeployer
        from services.deploy_security import collect_allowed_target_roots
        from contextlib import nullcontext
        deployer=ModDeployer(library_root=self.library,db=self.db)
        context,error,_=deployer._resolve_context(internal_id,prepare_archives=False,for_undeploy=True)
        if context is None:raise PermissionError((error or {}).get('error','无法解析部署上下文'))
        for raw in (context.config.install_path,context.config.mod_path,context.custom_deploy_path):
            if raw:checked_resource_path(Path(raw).resolve(),Path(raw))
        roots=collect_allowed_target_roots(context)
        for root in roots:checked_resource_path(root.resolve(),root)
        grant={'resources':self,'internal_id':internal_id,'roots':roots}
        token=_deployment_operation.set(grant)
        try:
            projection=self.game_projection(context.app_id,backup=True) if context.app_id in (281990,1158310,1086940) else nullcontext()
            with projection:yield
        finally:_deployment_operation.reset(token)

    def projection_directory(self,app_id):
        if int(app_id)==1086940:
            from services.bg3_activation import bg3_modsettings_path
            raw=bg3_modsettings_path()
            return checked_resource_path(raw.parent.resolve(),raw).parent
        from services.paradox_activation import is_paradox_activation_app,resolve_paradox_user_dir
        if not is_paradox_activation_app(app_id):raise PermissionError('此游戏没有 Paradox 投影合同')
        if self.db.get_game(int(app_id)) is None:raise PermissionError('游戏记录不存在')
        raw=resolve_paradox_user_dir(self.db,app_id=int(app_id))
        return checked_resource_path(raw.resolve(),raw)

    def allows_database(self,value):
        grant=_projection_operation.get()
        if not grant or grant['resources'] is not self:return False
        text=str(value)
        if text.startswith('file:'):
            from urllib.request import url2pathname
            text=url2pathname(text[5:].split('?',1)[0])
        try:return Path(text).resolve() in grant['databases']
        except (OSError,ValueError):return False

    def projection_version(self,app_id):
        from services.paradox_activation import DLC_LOAD_FILENAME,LAUNCHER_DB_FILENAME
        docs=self.projection_directory(app_id)
        result=[]
        if int(app_id)==1086940:
            from services.bg3_activation import bg3_modsettings_path
            names=(bg3_modsettings_path().name,)
        else:names=(DLC_LOAD_FILENAME,LAUNCHER_DB_FILENAME,LAUNCHER_DB_FILENAME+'-wal')
        for name in names:
            path=checked_resource_path(docs,docs/name)
            stat=path.stat() if path.exists() else None
            result.append([str(path),stat.st_size if stat else None,stat.st_mtime_ns if stat else None])
        return result

    @contextmanager
    def game_projection(self,app_id,*,backup=False):
        """Grant only the original game's projection files for one operation."""
        from services.paradox_activation import DLC_LOAD_FILENAME,LAUNCHER_DB_FILENAME
        docs=self.projection_directory(app_id)
        if int(app_id)==1086940:
            from services.bg3_activation import bg3_modsettings_path
            database=None;targets=(bg3_modsettings_path(),);files=set(targets)
        else:
            database=docs/LAUNCHER_DB_FILENAME;targets=(docs/DLC_LOAD_FILENAME,database)
            files={*targets,*[Path(str(database)+suffix) for suffix in ('-journal','-wal','-shm')]}
        for path in files:checked_resource_path(docs,path)
        grant={'resources':self,'directory':docs,'files':files,'databases':{database} if database else set(),'bg3':int(app_id)==1086940}
        token=_projection_operation.set(grant)
        try:
            if backup:
                import json,shutil,sqlite3
                from uuid import uuid4
                dest=self.path('data/projection_recovery')/str(app_id)/uuid4().hex
                dest.mkdir(parents=True)
                receipt={'game':int(app_id),'directory':str(docs),'files':[]}
                for source in targets:
                    if not source.exists():receipt['files'].append({'path':str(source),'existed':False});continue
                    copied=dest/source.name
                    if source==database:
                        grant['databases'].add(copied)
                        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original,sqlite3.connect(copied) as snapshot:
                            original.backup(snapshot)
                            if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Launcher SQLite 恢复点校验失败')
                    else:
                        shutil.copy2(source,copied)
                        if source.read_bytes()!=copied.read_bytes():raise RuntimeError('游戏投影恢复点校验失败')
                    receipt['files'].append({'path':str(source),'existed':True,'backup':str(copied)})
                (dest/'restore-point.json').write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf8')
            yield docs
        finally:_projection_operation.reset(token)

    @contextmanager
    def source_network(self, operation='sync'):
        if operation not in ('sync','offline'):raise PermissionError('未知来源业务')
        from services.proxy_resolver import resolve_proxy
        import socket
        proxy=resolve_proxy()
        approved=set()
        if proxy.enabled and proxy.host and proxy.port:
            approved={(a[4][0],proxy.port) for a in socket.getaddrinfo(proxy.host,proxy.port,type=socket.SOCK_STREAM)}
        token=_source_operation.set((operation,approved))
        try:yield
        finally:_source_operation.reset(token)

    def steam_source_network_active(self):
        """Only an explicit original source workflow may use its configured proxy."""
        grant=_source_operation.get()
        return bool(grant and grant[0]=='sync')

    def allow_source_pipe(self,fd):
        grant=_source_operation.get()
        if not grant or grant[0]!='offline':return False
        import os
        if os.name!='nt':return False
        import ctypes,msvcrt
        kind=ctypes.windll.kernel32.GetFileType
        kind.argtypes=[ctypes.c_void_p];kind.restype=ctypes.c_uint32
        return kind(msvcrt.get_osfhandle(fd))==3

    def allow_source_external(self,event,args):
        grant=_source_operation.get()
        if not grant:return False
        operation,proxies=grant
        if event=='socket.connect':
            import ipaddress
            address=args[1]
            if isinstance(address,tuple) and address[:2] in proxies:return True
            if operation=='offline' and isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):
                # Windows asyncio creates its private wake-up channel through
                # stdlib socketpair. Permit only that already-listening pair;
                # ordinary page requests to localhost remain denied.
                import socket,sys
                code=getattr(socket.socketpair,'__code__',None);frame=sys._getframe()
                while frame:
                    if frame.f_code is code:
                        listener=frame.f_locals.get('lsock');client=frame.f_locals.get('csock')
                        if listener is not None and client is args[0] and listener.getsockname()[:2]==address[:2]:return True
                    frame=frame.f_back
            if isinstance(address,tuple) and address[1] in (80,443):
                try:return ipaddress.ip_address(address[0]).is_global
                except ValueError:return False
        if event=='subprocess.Popen' and operation=='offline':
            from playwright._impl._driver import compute_driver_executable
            node,_=compute_driver_executable()
            return Path(args[0]).resolve()==Path(node).resolve()
        return False

    def __init__(self, root, db, library):
        # Deliberately do not call the isolated-root constructor.
        checked_resource_path(Path(root).resolve(),Path(root))
        checked_resource_path(Path(library).resolve(),Path(library))
        object.__setattr__(self, 'root', Path(root).resolve())
        object.__setattr__(self, 'db', db)
        object.__setattr__(self, 'library', Path(library).resolve())
        object.__setattr__(self, 'steam_api_url', '')
        object.__setattr__(self, 'steam_addresses', ())
        object.__setattr__(self, 'steam_public', True)
        object.__setattr__(self, '_inputs', set())
        object.__setattr__(self, '_input_lock', RLock())
        checked_resource_path(self.root, self.root)
        checked_resource_path(self.library, self.library)
        if len(self.library.parts) < 2 or self.library == self.root:
            raise PermissionError('归档目录必须是独立目录，不能是磁盘根或项目根')
        if Path(db.db_path).resolve() != self.root / 'data/mod_manager.db':
            raise PermissionError('生产 SQLite 与正式入口不一致')
        from services.proxy_resolver import resolve_proxy
        resolve_proxy()
        self.validate_database()
        with db._lock:
            db._conn.execute('PRAGMA temp_store=MEMORY')

    def check_context(self, context):
        if context.mode != 'production':
            raise PermissionError('生产资源只能由正式应用入口启用')

    def path(self, name):
        parts=Path(name).parts
        if not parts or Path(name).is_absolute() or '..' in parts:
            raise PermissionError('非法资源名称')
        if parts[0] == 'mod':
            return checked_resource_path(self.library, self.library.joinpath(*parts[1:]))
        return checked_resource_path(self.root, self.root / name)

    def _games(self):
        return [self.db.get_game_deploy_config(g.app_id) for g in self.db.list_games()]

    def game_target(self, value):
        path=Path(value).resolve()
        grant=_deployment_operation.get()
        if grant and grant['resources'] is self:
            for root in grant['roots']:
                if path.is_relative_to(root):checked_resource_path(root,value);return True
        for game in self._games():
            for raw in (game.install_path, game.mod_path, game.workshop_path):
                if raw and path.is_relative_to(Path(raw).resolve()):
                    checked_resource_path(Path(raw).resolve(), value)
                    return True
        return False

    def validate(self, value):
        path=Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise PermissionError('业务路径必须是明确绝对路径')
        grant=_projection_operation.get()
        if grant and grant['resources'] is self and path.resolve().is_relative_to(grant['directory']):
            resolved=checked_resource_path(grant['directory'],path)
            descriptor=(not grant['bg3'] and resolved.parent==grant['directory']/'mod' and resolved.suffix.lower()=='.mod')
            temporary=(grant['bg3'] and resolved.parent==grant['directory'] and
                       ((resolved.name.startswith('smm_bg3_order_') and resolved.suffix=='.lsx') or
                        (resolved.name.startswith('.bg3_modsettings_') and resolved.suffix=='.tmp')))
            if resolved in grant['files'] or resolved==grant['directory'] or descriptor or temporary:
                return resolved
            raise PermissionError('此文件不属于当前 Backend 的投影读写合同：'+str(path))
        for root in (self.root,self.library):
            if path.resolve().is_relative_to(root):return checked_resource_path(root,path)
        if self.game_target(path):return path.resolve()
        with self._input_lock:
            for root in self._inputs:
                if path.resolve().is_relative_to(root):return checked_resource_path(root,path)
        raise PermissionError('资源不属于已配置的生产目录或明确选择的输入：'+str(path))

    def validate_write(self, value):
        path=self.validate(value)
        for game in self._games():
            if game.workshop_path and path.is_relative_to(Path(game.workshop_path).resolve()):
                raise PermissionError('Workshop 原始内容只能读取，不能修改或删除')
        grant=_projection_operation.get()
        if grant and grant['resources'] is self and (path in grant['files'] or path==grant['directory']):return path
        if grant and grant['resources'] is self and grant['bg3'] and path.parent==grant['directory']:
            if (path.name.startswith('smm_bg3_order_') and path.suffix=='.lsx') or (path.name.startswith('.bg3_modsettings_') and path.suffix=='.tmp'):return path
        roots=[self.library,*[self.root/n for n in ('data','config','cache','logs','incoming')]]
        for game in self._games():
            if game.mod_path:
                # Workshop inputs are always read-only; their native WH3 projection
                # is the single used_mods.txt file, not the source .pack tree.
                mod=Path(game.mod_path).resolve()
                workshop=Path(game.workshop_path).resolve() if game.workshop_path else None
                if len(mod.parts)>2 and (not workshop or not mod.is_relative_to(workshop)):
                    roots.append(mod)
            if int(game.app_id)==1142710 and game.install_path and path==Path(game.install_path).resolve()/'used_mods.txt':
                return path
        deployment=_deployment_operation.get()
        if deployment and deployment['resources'] is self:
            for root in deployment['roots']:
                if path.is_relative_to(root):return checked_resource_path(root,path)
        if any(path.is_relative_to(root) for root in roots):return path
        raise PermissionError('禁止写入 Workshop 输入或未授权的生产目标：'+str(path))

    def select_input(self, value):
        path=Path(value)
        checked_resource_path(path.resolve(),path)
        if not path.exists() or len(path.resolve().parts)<3:
            raise PermissionError('请选择具体文件或 Mod 目录')
        with self._input_lock:
            self._inputs.add(path.resolve())
            if path.is_file() and path.suffix.lower() in ('.html','.htm'):
                from services.offline.html_rewriter import companion_files_dir
                companion=companion_files_dir(path)
                if companion:
                    checked_resource_path(companion.resolve(),companion)
                    self._inputs.add(companion.resolve())
        return path.resolve()

    def input_allowed(self, value):
        path=self.validate(value)
        with self._input_lock:
            return path in self._inputs or path.is_relative_to(self.path('incoming'))

    def resolve_steam(self):
        object.__setattr__(self,'steam_addresses',resolve_steam_addresses())

    def validate_deploy(self, internal_id, *, scan=True, require_payload=True):
        # Production uses the original configured paths, not the sandbox's
        # mandatory install/mod/games tree. Strategy preconditions remain in
        # ModDeployer; this layer only proves resource ownership and targets.
        from services.deploy_identity import resolve_deploy_entity
        from services.deploy_rules import resolve_deploy_type
        entity=resolve_deploy_entity(internal_id,db=self.db)
        info=self.db.get_mod_deploy_info(entity.mod_pk)
        config=self.db.get_game_deploy_config(info.app_id)
        if config is None:raise PermissionError('请先配置当前游戏')
        strategy=resolve_deploy_type(info.app_id,config.deploy_type)
        custom=self.db.get_mod_display_info(entity.mod_pk).custom_deploy_path or ''
        if custom:strategy='custom_path'
        from services.deploy_rules.manifest import load_manifest
        row=self.db.get_mod_backup_row(entity.mod_pk)
        if not row or not row.get('last_known_path'):raise PermissionError('Mod 缺少权威归档路径')
        managed=self.validate(row['last_known_path'])
        if managed==self.library or not managed.is_relative_to(self.library):
            raise PermissionError('Mod 归档不属于当前资源库')
        if strategy=='warhammer3_pack':
            from services.wh3_activation import workshop_content_root
            if not config.install_path:raise PermissionError('WH3 缺少游戏安装目录，无法投影 used_mods.txt')
            self.validate_write(Path(config.install_path)/'used_mods.txt')
            workshop=workshop_content_root(config.workshop_path)
            wid=str(row.get('workspace_id') or '')
            if workshop is None or not wid.isascii() or not wid.isdigit():
                raise PermissionError('WH3 缺少有效 Workshop 目录或来源身份')
            native=self.validate(workshop/wid)
            if require_payload and (not native.is_dir() or not any(native.rglob('*.pack'))):
                raise PermissionError('该 Mod 缺少原生 Workshop .pack；压缩包部署需单独校验解包目标')
            if scan and native.is_dir():
                for path in native.rglob('*'):self.validate(path)
        elif strategy=='folder_copy':
            if not config.mod_path:raise PermissionError('请先配置当前游戏的 Mod 部署目录')
            self.validate_write(config.mod_path)
        else:
            from services.deploy import ModDeployer
            context,error,_=ModDeployer(library_root=self.library,db=self.db)._resolve_context(internal_id,prepare_archives=False,for_undeploy=not require_payload)
            if context is None:raise PermissionError((error or {}).get('error','无法解析原部署策略 '+strategy))
        if scan and managed.is_dir():
            for path in managed.rglob('*'):self.validate(path)
        manifest=load_manifest(managed)
        if manifest and manifest.files:
            if manifest.schema_version<2 or manifest.internal_id!=internal_id:
                raise PermissionError('历史复制 manifest 尚未准入；拒绝覆盖或撤出，原资源保持不动')
            from services.deploy_security import collect_allowed_target_roots,validate_manifest_targets
            from services.deploy_rules.base import DeployContext
            ctx=DeployContext(internal_id=internal_id,source=managed,app_id=info.app_id,config=config,deploy_type=strategy,mod_pk=entity.mod_pk,custom_deploy_path=custom)
            roots=collect_allowed_target_roots(ctx)
            validate_manifest_targets(manifest,allowed_roots=roots)
            for entry in manifest.files:
                self.validate(entry.source)
                for root in roots:
                    if Path(entry.target).resolve().is_relative_to(root):
                        checked_resource_path(root,entry.target);break
                if entry.backup:
                    from services.backup_manager import BackupManager
                    backup=BackupManager(managed,internal_id=internal_id,mod_pk=entity.mod_pk).resolve_backup_file(entry.backup)
                    checked_resource_path(self.path('data/deploy_backup')/internal_id,backup)
        return entity
