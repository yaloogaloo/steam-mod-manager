"""Explicit, thread-local isolated business resources; no production opt-in.

Legacy callers keep their existing paths. Workers receive this object explicitly
and enter bind(); nested legacy helpers resolve the same DB/resource namespace.
An audit guard rejects unaccounted filesystem writes even if a helper bypasses
the canonical paths. It is a Python boundary, not an OS sandbox.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
import os
import stat
import sys

_scope = ContextVar('smm_business_resources', default=None)
_guard_installed = False


def current_resources():
    scope = _scope.get()
    return scope.resources if scope else None


def resolve_steam_addresses():
    import socket
    import ipaddress
    addresses={item[4][0] for item in socket.getaddrinfo('api.steampowered.com',443,type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise PermissionError('Steam DNS 返回非公网地址，拒绝连接')
    return tuple(sorted(addresses))


def resource_path(name, fallback):
    resources = current_resources()
    if resources:
        return resources.path(name)
    return fallback


def checked_sandbox_root(value):
    root = Path(value)
    production = Path(__file__).resolve().parents[1]
    if not root.is_absolute() or '..' in root.parts or root.drive.startswith('\\\\'):
        raise PermissionError('隔离根必须是明确的绝对路径')
    resolved = root.resolve()
    if resolved.is_relative_to(production) or production.is_relative_to(resolved):
        raise PermissionError('隔离根不能包含生产项目')
    for candidate in (root, *root.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PermissionError('隔离根不能通过链接或 junction 建立')
    return resolved


def checked_resource_path(root, value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionError('拒绝相对或未规范化的业务路径')
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(f'资源路径逃逸：{path}')
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PermissionError(f'资源路径包含链接或 reparse point：{candidate}')
    return resolved


@dataclass
class _Scope:
    resources: object
    violations: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    network: list = field(default_factory=list)
    steam_addresses: tuple = ()


def _audit(event, args):
    scope = _scope.get()
    if scope is None:
        return
    if event in ('socket.connect','subprocess.Popen'):
        admit=getattr(scope.resources,'allow_source_external',None)
        if admit and admit(event,args):
            scope.network.append({'operation':'existing-source-service','event':event})
            return
    paths = ()
    if event == 'open':
        path, mode, flags = args
        if isinstance(path, int):
            pipe=getattr(scope.resources,'allow_source_pipe',None)
            if pipe and pipe(path):return
            # CAS atomic publish wraps an already-open mkstemp descriptor. Resolve
            # its real OS target; never permit descriptors merely because they exist.
            try:
                if os.name == 'nt':
                    import ctypes
                    import msvcrt
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    query = kernel.GetFinalPathNameByHandleW
                    query.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
                    query.restype = ctypes.c_uint32
                    buffer = ctypes.create_unicode_buffer(32768)
                    length = query(msvcrt.get_osfhandle(path), buffer, len(buffer), 0)
                    if not length or length >= len(buffer):raise OSError('文件描述符路径不可解析')
                    path = buffer.value
                    if path.startswith('\\\\?\\UNC\\'):path = '\\\\' + path[8:]
                    elif path.startswith('\\\\?\\'):path = path[4:]
                else:path = os.readlink(f'/proc/self/fd/{path}')
                scope.resources.validate(path)
            except (OSError, ValueError) as exc:
                scope.violations.append((event, str(args[0])))
                raise PermissionError('无法确认文件描述符的隔离归属') from exc
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            paths = (path,)
        elif isinstance(path, (str, bytes, os.PathLike)):
            candidate = Path(os.fsdecode(path)).resolve()
            code = Path(__file__).resolve().parents[1]
            import_read = (candidate.suffix in ('.py', '.pyc') and
                           any(candidate.is_relative_to(code / name) for name in ('core','services','ui','frontend','tests','tools')))
            dependency_read = any(candidate.is_relative_to(Path(prefix).resolve()) for prefix in (sys.prefix, sys.base_prefix))
            if not import_read and not dependency_read:
                try:
                    scope.resources.validate(os.fsdecode(path))
                except (OSError, ValueError) as exc:
                    scope.violations.append((event, str(path)))
                    raise PermissionError(f'隔离资源读取被拒绝：{path}') from exc
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime', 'os.truncate'):
        paths = args[:1]
    elif event in ('os.link', 'os.symlink'):
        scope.violations.append((event, str(args[:2])))
        raise PermissionError('隔离部署禁止创建文件系统链接')
    elif event == 'os.rename':
        paths = args[:2]
    elif event == 'sqlite3.connect':
        if str(args[0]) != str(scope.resources.db.db_path) and not getattr(scope.resources,'allows_database',lambda path:False)(args[0]):
            scope.violations.append((event, str(args[0])))
            raise PermissionError('隔离运行禁止打开其他 SQLite')
    elif event == 'socket.connect' and scope.resources.steam_public:
        address = args[1]
        approved=scope.steam_addresses or scope.resources.steam_addresses
        if isinstance(address, tuple) and address[0] in approved and address[1] == 443:
            scope.network.append({'host':'api.steampowered.com','address':str(address)})
            return
        scope.violations.append((event, str(address)))
        raise PermissionError('Steam 公网连接目标未获准入')
    elif event == 'socket.connect' and scope.resources.steam_api_url:
        from urllib.parse import urlsplit
        endpoint = urlsplit(scope.resources.steam_api_url)
        address = args[1]
        if isinstance(address, tuple) and address[:2] == ('127.0.0.1', endpoint.port):
            return
        scope.violations.append((event, str(address)))
        raise PermissionError('同步只允许已授权的本地来源地址')
    elif event in ('subprocess.Popen', 'os.system', 'os.startfile', 'socket.connect'):
        scope.violations.append((event, 'external capability'))
        raise PermissionError('隔离部署禁止外部执行或网络访问')
    for path in paths:
        try:
            if not isinstance(path, (str, bytes, os.PathLike)):
                raise PermissionError('无法确认文件描述符写入归属')
            if event == 'os.mkdir' and Path(os.fsdecode(path)).is_dir():
                scope.resources.validate(os.fsdecode(path))
                continue
            scope.resources.validate_write(os.fsdecode(path))
            scope.writes.append((event, os.fsdecode(path)))
        except (OSError, ValueError) as exc:
            scope.violations.append((event, str(path)))
            raise PermissionError(f'隔离写入被拒绝：{path}') from exc


@dataclass(frozen=True)
class BusinessResources:
    root: Path
    db: object
    steam_api_url: str = ''
    steam_public: bool = False
    steam_addresses: tuple = field(default=(), init=False)

    def __post_init__(self):
        if self.steam_public:
            if self.steam_api_url:
                raise PermissionError('公网与 Fixture 来源不能同时启用')
            object.__setattr__(self,'steam_addresses',resolve_steam_addresses())
        if self.steam_api_url:
            from urllib.parse import urlsplit
            endpoint = urlsplit(self.steam_api_url)
            if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
                    or not endpoint.port or endpoint.username or endpoint.password
                    or endpoint.path != '/steam/details' or endpoint.query or endpoint.fragment):
                raise PermissionError('隔离来源端点必须是明确的本地 HTTP Fixture')
        root = Path(self.root)
        resolved = checked_sandbox_root(root)
        object.__setattr__(self, 'root', resolved)
        self.validate(root)
        database = self.validate(self.db.db_path)
        if not database.is_relative_to(self.path('data')):
            raise PermissionError('隔离 SQLite 必须归属 data 树')
        self.validate_database()
        if getattr(self.db, '_read_only', False):
            raise PermissionError('部署需要隔离可写数据库')
        with self.db._lock:
            self.db._conn.execute('PRAGMA temp_store=MEMORY')

    def validate(self, value):
        return checked_resource_path(self.root, value)

    def validate_write(self, value):
        return self.validate(value)

    def input_allowed(self, value):
        return self.validate(value).is_relative_to(self.path('incoming'))

    def select_input(self, value):
        if not self.input_allowed(value):raise PermissionError('请选择 incoming 内的输入资源')
        return self.validate(value)

    def check_context(self, context):
        if context.mode != 'test':
            raise PermissionError('隔离资源不接受生产 mutation context')

    def game_target(self, value):
        return self.validate(value).is_relative_to(self.path('games'))

    def path(self, name):
        return self.validate(self.root / name)

    def validate_database(self):
        # Check the live SQLite connection, not merely its Python path label.
        with self.db._lock:
            databases = list(self.db._conn.execute('PRAGMA database_list'))
        main = [row for row in databases if row[1] == 'main']
        if len(main) != 1 or self.validate(main[0][2]) != self.validate(self.db.db_path):
            raise PermissionError('SQLite 实际连接与资源上下文不匹配')
        if any(row[1] not in ('main', 'temp') for row in databases):
            raise PermissionError('隔离业务禁止未声明的 attached SQLite')
        for row in databases:
            if row[1] == 'temp' and row[2]:
                self.validate(row[2])

    @contextmanager
    def bind(self):
        global _guard_installed
        from services.mutation_context import assert_mutation_allowed
        context = assert_mutation_allowed(self.root, self.db.db_path)
        self.check_context(context)
        self.validate_database()
        if not _guard_installed:
            sys.dont_write_bytecode = True
            sys.addaudithook(_audit)
            _guard_installed = True
        existing = _scope.get()
        if existing and existing.resources is self:
            yield existing
            return
        scope = _Scope(self)
        token = _scope.set(scope)
        try:
            yield scope
            if scope.violations:
                raise PermissionError(f'业务出现未准入的资源访问：{scope.violations}')
        finally:
            _scope.reset(token)

    def validate_deploy(self, internal_id, *, scan=True, require_payload=True):
        from services.deploy_identity import resolve_deploy_entity
        from services.deploy_rules import resolve_deploy_type
        entity = resolve_deploy_entity(internal_id, db=self.db)
        info = self.db.get_mod_deploy_info(entity.mod_pk)
        row = self.db.get_mod_backup_row(entity.mod_pk) or {}
        managed = row.get('last_known_path')
        config = self.db.get_game_deploy_config(info.app_id)
        if config is None:
            raise PermissionError('缺少隔离游戏配置')
        strategy = resolve_deploy_type(info.app_id, config.deploy_type)
        if strategy not in ('folder_copy', 'warhammer3_pack'):
            raise PermissionError('该部署策略尚未完成隔离验收，拒绝执行')
        if not self.path('config/game_capabilities.json').is_file():
            raise PermissionError('缺少隔离能力配置，拒绝读取生产默认值')
        for value in (managed, config.install_path, config.mod_path):
            if not value:
                raise PermissionError('缺少明确的业务资源路径')
            self.validate(value)
        if not Path(managed).resolve().is_relative_to(self.path('mod')):
            raise PermissionError('Mod 必须归属隔离归档库')
        for value in (config.install_path, config.mod_path):
            if not self.game_target(value):
                raise PermissionError('部署游戏目录必须位于隔离 games 树')
        if config.workshop_path:
            self.validate(config.workshop_path)
        if strategy == 'warhammer3_pack':
            from services.wh3_activation import workshop_content_root
            if info.app_id != 1142710:
                raise PermissionError('WH3 策略与游戏身份不匹配')
            workshop = workshop_content_root(config.workshop_path)
            if workshop is None or not self.game_target(workshop):
                raise PermissionError('WH3 需要隔离 games 树内的 Workshop Content 路径')
            wid = str(row.get('workspace_id') or '')
            if not wid.isascii() or not wid.isdigit():
                raise PermissionError('WH3 Workshop 身份无效')
            native = self.validate(workshop / wid)
            # Only native Workshop packs are admitted. Archive extraction has
            # a separate native resource boundary and is not yet approved.
            if require_payload and (not native.is_dir() or not any(native.rglob('*.pack'))):
                raise PermissionError('当前 WH3 仅准入原生 Workshop .pack，缺失内容或压缩包解包尚未准入')
            # Projection evaluates all deployed members, not just this Mod.
            if scan:
                for path in workshop.rglob('*'):
                    self.validate(path)
            self.validate(Path(config.install_path) / 'used_mods.txt')
        # Catch an in-tree link before plans normalize its path away.
        if scan:
            for base in (Path(managed), Path(config.mod_path)):
                if base.is_dir():
                    for path in base.rglob('*'):
                        self.validate(path)
        from services.deploy_rules.manifest import load_manifest
        manifest = load_manifest(Path(managed)) if scan or strategy == 'warhammer3_pack' else None
        if manifest is not None:
            if strategy == 'warhammer3_pack' and manifest.files:
                raise PermissionError('WH3 历史复制 manifest 的备份恢复尚未准入，请先处理旧部署记录')
            for entry in manifest.files:
                self.validate(entry.source)
                self.validate_write(entry.target)
        return entity


def isolated_deploy(method):
    @wraps(method)
    def call(self, internal_id, *args, **kwargs):
        resources = getattr(self, 'resources', None)
        if resources is None:
            return method(self, internal_id, *args, **kwargs)
        if self._db is not resources.db or self.library_root != resources.path('mod'):
            raise PermissionError('部署实例与隔离资源上下文不匹配')
        with resources.bind():
            from contextlib import nullcontext
            scoped=getattr(resources,'deployment_scope',None)
            with scoped(internal_id) if scoped else nullcontext():
                resources.validate_deploy(internal_id, require_payload=method.__name__ != 'undeploy_mod')
                return method(self, internal_id, *args, **kwargs)
    return call
