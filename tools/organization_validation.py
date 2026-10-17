"""Real isolated organisation fixture and Windows WebEngine verification."""
import hashlib
import json
from pathlib import Path


def initialize(root):
    from tools.wh3_deploy_validation import initialize as wh3_initialize
    from services.identity_service import create_mod_identity, identity_create_scope
    from services.deploy import ModDeployer
    from services import collection
    resources, source, target, first = wh3_initialize(root)
    # One already-deployed member proves organisation leaves deployment intact.
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    assert deployer.deploy_mod(first.internal_id)['success']
    from PySide6.QtGui import QImage
    image = QImage(24,24,QImage.Format_RGB32)
    image.fill(0x467C74)
    cover = resources.validate(source/'.info/user-cover.png')
    assert image.save(str(cover),'PNG')
    resources.db.update_mod_cover_path(first.mod_id, '.info/user-cover.png')
    identities = [first]
    with resources.bind():
        resources.db.update_game_deploy_config(43, name='另一隔离游戏')
        for app, wid, title in ((1142710,'902101','Quality of Life'),(43,'902102','Other Game Mod')):
            with identity_create_scope():
                identity = create_mod_identity(resources.db, platform='Steam', external_id=wid,
                    workshop_id=wid, title=title, app_id=app, game_name=str(app), operation='import')
            folder = resources.path('mod') / str(app) / title.replace(' ','')
            (folder/'.info').mkdir(parents=True)
            (folder/'payload.txt').write_text('PRESERVE', encoding='utf8')
            (folder/'.info/metadata.json').write_text(json.dumps({
                'internal_id':identity.internal_id,'workspace_id':identity.workspace_id,
                'title':title,'platform':'Steam','app_id':app,'external_id':wid,
                'source_url':f'https://steamcommunity.com/sharedfiles/filedetails/?id={wid}',
            }), encoding='utf8')
            resources.db.update_mod_identity_fields(identity.mod_id, last_known_path=str(folder),
                folder_present=True, title=title, game_name=str(app))
            identities.append(identity)
        other = collection.create_collection(43, '其他游戏的合集', db=resources.db)
        collection.add_mod_to_collection(other.collection_id, identities[2].internal_id, db=resources.db)
        resources.db.add_mod_tag(identities[1].mod_id, 'category', 'Graphics')
        (resources.root/'.smm-organization-sandbox').write_text('v1',encoding='utf8')
    return resources, identities


def protected_state(resources):
    """Organisation may change SQLite tags/collections and collection covers only."""
    db=resources.db
    with db._lock:
        mods=[tuple(row) for row in db._conn.execute(
            'SELECT mod_id,app_id,internal_id,workspace_id,deploy_status,deploy_path,enabled,cover_path,user_override_fields,category,type_id,source_type,title,description FROM mods ORDER BY mod_id')]
        other=[tuple(row) for row in db._conn.execute('SELECT * FROM collections WHERE app_id=43')]
        members=[tuple(row) for row in db._conn.execute('SELECT * FROM collection_mods WHERE collection_id IN (SELECT collection_id FROM collections WHERE app_id=43)')]
    files={}
    for directory in ('mod','games','config','data/asset_store','data/mod_backup','data/deploy_backup'):
        for path in resources.path(directory).rglob('*'):
            if path.is_file():
                files[str(path.relative_to(resources.root))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return {'mods':mods,'other_collections':other,'other_members':members,'files':files}


def database_state(resources):
    with resources.db._lock:
        return {table:[dict(r) for r in resources.db._conn.execute('SELECT * FROM '+table)]
            for table in ('collections','collection_mods','mod_tags')}


def verify_window(window, resources, failure_mode=None):
    import time
    from PySide6.QtTest import QTest
    output=resources.root/'evidence'
    output.mkdir()
    before=protected_state(resources)
    report={'protected_before':before,'stages':{},'responses':[], 'failure_mode':failure_mode}
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    def js(code):
        values=[]
        window.page.runJavaScript(code,values.append)
        for _ in range(300):
            if values:return values[0]
            QTest.qWait(10)
            time.sleep(.005)
        raise TimeoutError('JavaScript callback timed out')
    def wait(condition):
        for _ in range(600):
            if js(condition):return
            QTest.qWait(15)
            time.sleep(.01)
        raise TimeoutError(condition+': '+str(js("JSON.stringify({message:$('results').textContent,org:$('organization-message').textContent,busy:S.busy})")))
    def capture(name):
        QTest.qWait(250)
        window.grab().save(str(output/(name+'.png')))
        report['stages'][name]=database_state(resources)
        covers=resources.path('data/collection_covers')
        report['stages'][name]['cover_files']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in covers.glob('*') if p.is_file()}
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load();")
        wait("S.assets.length===2 && !document.getElementById('refresh').disabled")
        js("inspect(S.assets.find(a=>a.workspace_id==='902100'));")
        wait('!!S.details')
        js("openMoreMenu();document.querySelector('[data-action=tags]').click();")
        wait("$('organization').open && !!$('tag-query')")
        capture('01-tag-manager')
        if failure_mode=='tag-db':
            resources.db._conn.execute("CREATE TEMP TRIGGER reject_tag BEFORE INSERT ON mod_tags BEGIN SELECT RAISE(ABORT,'tag write refused'); END")
        js("$('tag-query').value='Gameplay';$('tag-add').click();")
        if failure_mode=='tag-db':
            wait("!S.busy && $('organization-message').textContent.startsWith('操作失败：')")
            assert not resources.db.get_category_tags(resources.db.find_mod_by_internal_id(js('O.target')))
            capture('07-tag-write-failure')
            resources.db._conn.execute('DROP TRIGGER reject_tag')
            report['tag_failure_recovered']=True
            js("$('tag-add').click();")
        wait("!S.busy && !!document.querySelector('[data-remove-tag=Gameplay]')")
        first=resources.db.find_mod_by_internal_id(js('O.target'))
        assert resources.db.get_category_tags(first)==['Gameplay']
        capture('02-tag-added')
        js("$('organization-cancel').click();")
        assert js("document.querySelector('.inspector-user-tags').textContent.includes('Gameplay')")
        capture('02b-tagged-inspector')
        js("openMoreMenu();document.querySelector('[data-action=tags]').click();")
        wait("$('organization').open && !!document.querySelector('[data-remove-tag=Gameplay]')")
        js("document.querySelector('[data-remove-tag=Gameplay]').click();")
        wait("!S.busy && !document.querySelector('[data-remove-tag=Gameplay]')")
        assert resources.db.get_category_tags(first)==[]
        capture('02c-tag-removed')
        js("$('organization-cancel').click();document.querySelector('[data-browse=collections]').click();$('collection-new').click();$('collection-name').value='日常体验增强';$('organization-save').click();")
        wait("!S.busy && S.collections.length===1 && !!$('collection-add-members')")
        cid=int(js("$('collection').value"))
        from services.collection import set_collection_cover
        with resources.bind():
            source=Path(resources.db.get_mod_backup_row(first)['last_known_path'])/'.info/user-cover.png'
            set_collection_cover(cid,source,db=resources.db)
        js("$('refresh').click();")
        wait("!$('refresh').disabled && !!$('collection-add-members')")
        js("$('collection-add-members').click();document.querySelectorAll('input[name=member]').forEach(e=>e.click());")
        capture('04a-add-members')
        js("$('organization-save').click();")
        wait("!S.busy && currentCollection()?.tokens.length===2")
        assert len(resources.db.list_collection_member_ids(cid))==2
        js("$('refresh').click();")
        wait("!$('refresh').disabled && S.rows.length===2")
        capture('04-members')
        js("$('collection-back').click();")
        capture('03-collection-list')
        js("document.querySelector('[data-open-collection]').click();document.querySelector('.card .check').click();$('collection-remove-members').click();")
        wait("!S.busy && currentCollection()?.tokens.length===1")
        js("$('collection-rename').click();$('collection-name').value='常用组合';$('organization-save').click();")
        wait("!S.busy && currentCollection()?.name==='常用组合'")
        capture('04b-renamed')
        js("$('collection-delete').click();")
        capture('05-delete-confirmation')
        if failure_mode=='cover-cleanup':
            import stat
            owned=resources.path(f'data/collection_covers/{cid}.png')
            original=owned.read_bytes()
            owned.chmod(stat.S_IREAD)
        js("$('organization-save').click();")
        if failure_mode=='cover-cleanup':
            wait("!S.busy && $('organization-message').textContent.startsWith('操作失败：')")
            assert resources.db.get_collection(cid) is not None
            assert len(resources.db.list_collection_member_ids(cid))==1
            assert owned.read_bytes()==original
            capture('07-cover-cleanup-failure')
            owned.chmod(stat.S_IREAD|stat.S_IWRITE)
            report['cover_failure_recovered']=True
            js("$('organization-save').click();")
        wait("!S.busy && S.collections.length===0 && !$('collection-list').hidden")
        assert resources.db.get_collection(cid) is None
        assert resources.db.list_collection_member_ids(cid)==[]
        assert not list(resources.path('data/collection_covers').glob('*'))
        capture('06-deleted')
        report['protected_after']=protected_state(resources)
        assert report['protected_after']==before
        report['success']=True
        report['dpi']=window.devicePixelRatioF()
    except Exception as exc:
        report['failure']=str(exc)
        capture('FAILED')
        raise
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
