"""Global settings persisted using the existing SMM QSettings keys."""
from PySide6.QtCore import QSettings
from services.proxy_resolver import parse_proxy_url


def state(bridge):
    s=QSettings('SteamModManager','WorkshopLibrary')
    return {'library':str(bridge.resources.path('mod')) if bridge.resources else s.value('paths/target_dir','',str),
        'proxy_mode':s.value('network/proxy_mode','auto',str),'proxy_url':s.value('network/proxy_url','',str),
        'has_cookie':bool(s.value('network/steam_cookie','',str)),'writable':bridge.adapter.writable}


def save(bridge,args):
    with bridge._lock:
        bridge._command_game(args);bridge.adapter._write()
        if bridge._jobs or bridge._busy-{args.get('_busy_key')}:raise RuntimeError('请等待业务任务结束后修改设置')
        mode=args.get('proxy_mode');url=args.get('proxy_url');cookie=args.get('steam_cookie')
        if mode not in ('auto','manual','direct') or not isinstance(url,str) or len(url)>2048:raise ValueError('代理设置无效')
        if mode=='manual':parse_proxy_url(url)
        if cookie is not None and (not isinstance(cookie,str) or len(cookie)>16000 or '\n' in cookie or '\r' in cookie):raise ValueError('Cookie 格式无效')
        s=QSettings('SteamModManager','WorkshopLibrary')
        old={k:s.value(k) for k in ('network/proxy_mode','network/proxy_url','network/steam_cookie')}
        s.setValue('network/proxy_mode',mode);s.setValue('network/proxy_url',url.strip())
        if cookie is not None:s.setValue('network/steam_cookie',cookie.strip())
        s.sync()
        if s.status()!=QSettings.NoError:
            for k,v in old.items():s.remove(k) if v is None else s.setValue(k,v)
            s.sync();raise OSError('设置未能持久化')
        from services.proxy_resolver import clear_proxy_cache
        clear_proxy_cache()
        return {'success':True,'message':'设置已保存；归档目录未改变',**state(bridge)}
