"""Manual Nexus identity checks; game mappings are evidence, never permissions."""
import re
from urllib.parse import urlsplit


def nexus_identity(url):
    parsed=urlsplit(str(url or '').strip())
    parts=parsed.path.strip('/').split('/')
    if (parsed.scheme!='https' or parsed.hostname not in ('www.nexusmods.com','nexusmods.com')
        or parsed.port or parsed.username or parsed.password or any(ord(c)<32 for c in url)):
        raise ValueError('请输入有效的 Nexus HTTPS Mod 链接')
    if len(parts)==3 and parts[1]=='mods' and re.fullmatch(r'[a-z0-9][a-z0-9-]*',parts[0]):
        slug,identity=parts[0],parts[2]
    elif len(parts)==2 and parts[0]=='mods':slug,identity='',parts[1]
    else:raise ValueError('Nexus 链接必须包含有效的 Mod ID')
    if not identity.isascii() or not identity.isdigit() or int(identity)<=0:
        raise ValueError('Nexus Mod ID 必须是有效的正整数')
    return slug,identity


def check_game(db,game,url):
    """Unknown mapping requires explicit per-draft confirmation, not refusal."""
    slug,identity=nexus_identity(url)
    from services.metadata_owner_guard import _NEXUS_SLUG_APP
    known=_NEXUS_SLUG_APP.get(slug)
    expected={s for s,app in _NEXUS_SLUG_APP.items() if app==int(game)}
    if slug and (known and known!=int(game) or expected and slug not in expected):
        raise ValueError('Nexus 网页游戏与当前游戏冲突，请切换目标游戏或纠正来源链接')
    if slug and known==int(game):return {'required':False,'game_slug':slug}
    # Existing source identities corroborate a previous manual association.
    with db._lock:
        rows=db._conn.execute("SELECT source_url FROM mods WHERE app_id=? AND lower(platform)='nexus'",(int(game),)).fetchall()
    established=set()
    for row in rows:
        try:established.add(nexus_identity(row[0])[0])
        except (ValueError,TypeError):continue
    if slug and slug in established:return {'required':False,'game_slug':slug}
    return {'required':True,'game_slug':slug,'message':
        f'网页中的游戏为 Nexus「{slug}」。请确认此 Mod 属于当前选择的游戏。' if slug else
        f'仅提供 Nexus Mod ID {identity}，尚未确认网页游戏。请核对其属于当前游戏，或返回填写完整链接。'}


def require_confirmation(check,confirmed):
    if check.get('required') and confirmed is not True:
        raise ValueError('请先确认 Nexus 来源与当前游戏的归属，或返回纠正来源链接')
