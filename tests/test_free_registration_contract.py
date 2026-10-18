"""Manual source choice, truthful Nexus identity, and isolated persistence."""
import json
import time
from email.message import EmailMessage
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_order_sandbox import initialize


@pytest.fixture
def registration(tmp_path):
    app=QApplication.instance() or QApplication([])
    r=initialize(tmp_path/'free-registration');r.path('incoming').mkdir()
    for game,name in [(2054972,"Dragon's Dogma 2"),(916440,'Anno 1800'),(281990,'Stellaris'),(1623730,'Palworld'),(999999,'Unknown selected game')]:
        r.db.update_game_deploy_config(game,name=name,deploy_type='folder_copy')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    def context(game):
        snapshot=b.execute('library',{'game':game})
        return {'game':str(game),'snapshot':snapshot['snapshot']}
    yield app,r,b,context
    while not b.shutdown():app.processEvents();time.sleep(.01)
    r.db.close()


def page(path,slug='dragonsdogma2',identity='8',*,mhtml=False):
    html=f'<html><head><meta property="og:url" content="https://www.nexusmods.com/{slug}/mods/{identity}"><meta property="og:title" content="REFramework"><meta property="og:description" content="Offline description"></head><body><div id="description">Saved description</div></body></html>'
    if mhtml:
        message=EmailMessage();message['MIME-Version']='1.0';message.set_type('multipart/related')
        part=EmailMessage();part.set_content(html,subtype='html');part['Content-Location']=f'https://www.nexusmods.com/{slug}/mods/{identity}';message.attach(part)
        path.write_bytes(message.as_bytes())
    else:path.write_text(html,encoding='utf8')
    return path


def submit(app,b,action,args):
    responses=[];b.response.connect(lambda raw:responses.append(json.loads(raw)))
    request=str(time.monotonic_ns());b.command(request,action,json.dumps(args))
    until=time.monotonic()+25
    while not any(r['request']==request for r in responses):
        app.processEvents();time.sleep(.01);assert time.monotonic()<until,responses
    return next(r for r in responses if r['request']==request)


@pytest.mark.parametrize('game,platform,url',[
    (2054972,'Nexus','https://www.nexusmods.com/dragonsdogma2/mods/8'),
    (2054972,'Nexus','8'),(916440,'mod.io','https://mod.io/g/anno-1800/m/example'),
    (916440,'mod.io','8302'),(281990,'Nexus','https://www.nexusmods.com/stellaris/mods/9'),
    (1623730,'GitHub','https://github.com/owner/repository'),(999999,'Other',''),
    (999999,'Steam','901001'),(999999,'Nexus','https://www.nexusmods.com/unmapped-game/mods/17'),
])
def test_manual_sources_without_api_or_payload(registration,game,platform,url):
    _,r,b,context=registration;args=context(game)
    draft=b.execute('registration-preview',{**args,'kind':'url','platform':platform,'url':url,'title':'Manual registration'})
    if draft['identity_confirmation'].get('required'):
        with pytest.raises(ValueError,match='确认'):b.execute_command('registration-save',{**args,'draft':draft['draft']})
    result=b.execute_command('registration-save',{**args,'draft':draft['draft'],'confirm_nexus_identity':True})
    row=r.db.get_mod_backup_row(result['mod_pk'])
    assert row['app_id']==game and result['content_status']=='content_missing'
    assert Path(result['managed_path']).is_dir()
    assert row['internal_id']==result['internal_id']
    assert 'apikey' not in json.dumps(draft).lower()


def test_unknown_mapping_batch_confirmation_persists_and_repeats(registration):
    app,r,b,context=registration;args=context(2054972)
    source=page(r.path('incoming/framework.mhtml'),mhtml=True)
    broken=r.path('incoming/invalid.html');broken.write_text('<html>No Nexus identity</html>')
    preview=b.execute('offline-preview',{**args,'paths':[str(source),str(broken)]})
    assert [row['state'] for row in preview['rows']]==['confirmation','failed']
    assert preview['rows'][0]['identity_confirmation']['game_slug']=='dragonsdogma2'
    denied=submit(app,b,'offline-register',{**args,'preview':preview['preview'],'ids':['0']})
    assert '确认' in denied['error'] and not b.adapter.assets(2054972)
    reply=submit(app,b,'offline-register',{**args,'preview':preview['preview'],'ids':['0'],'confirmed_ids':['0']})
    assert reply['result']['items'][0]['state']=='success',reply
    asset=b.adapter.assets(2054972)[0];managed=Path(asset.managed_path)
    assert (managed/'.info/offline/index.html').is_file()
    assert (managed/'.info/offline/manifest.json').is_file()
    from core.db_manager import DatabaseManager
    restarted=DatabaseManager.open_existing(r.db.db_path,read_only=True)
    try:
        row=restarted.get_mod_backup_row(asset.mod_pk)
        assert row['app_id']==2054972 and restarted.get_mod_display_info(asset.mod_pk).source_url=='https://www.nexusmods.com/dragonsdogma2/mods/8'
        assert row['internal_id']==asset.internal_id
    finally:restarted.close()
    preview=b.execute('offline-preview',{**args,'paths':[str(source)]})
    assert preview['rows'][0]['state']=='existing'
    duplicate=submit(app,b,'offline-register',{**args,'preview':preview['preview'],'ids':['0']})
    assert duplicate['result']['items'][0]['state']=='skipped'
    assert len(b.adapter.assets(2054972))==1


@pytest.mark.parametrize('game,url',[(2054972,'https://www.nexusmods.com/stardewvalley/mods/8'),
    (1623730,'https://www.nexusmods.com/other-game/mods/8'),(2054972,'https://nexusmods.com.evil/dragonsdogma2/mods/8'),
    (2054972,'https://www.nexusmods.com/dragonsdogma2/mods/0'),(2054972,'https://www.nexusmods.com/dragonsdogma2/mods/invalid')])
def test_identity_conflicts_or_invalid_ids_are_not_permissions(registration,game,url):
    _,r,b,context=registration;args=context(game)
    with pytest.raises(ValueError):b.execute('registration-preview',{**args,'kind':'url','platform':'Nexus','url':url})
    assert not b.adapter.assets(game)


@pytest.mark.parametrize('platform,url',[
    ('Steam','https://example.org/?id=901001'),('Steam','0'),('mod.io','0'),
    ('mod.io','https://mod.io'),('mod.io','https://mod.io/not-a-mod'),
    ('GitHub','https://github.com.evil/owner/repository'),
])
def test_invalid_official_identity_is_not_registered(registration,platform,url):
    _,r,b,context=registration;args=context(999999)
    with pytest.raises(ValueError):b.execute('registration-preview',{**args,'kind':'url','platform':platform,'url':url})
    assert not b.adapter.assets(999999)


def test_local_nexus_direct_archive_requires_its_own_confirmation(registration):
    app,r,b,context=registration;args=context(2054972)
    source=r.path('incoming/real-content');source.mkdir();(source/'reframework.dll').write_bytes(b'ISOLATED TEST PAYLOAD')
    offline=page(r.path('incoming/details.mhtml'),mhtml=True)
    values={'kind':'local','platform':'Nexus','format':'folder','source':str(source),'title':'With content','offline_html_path':str(offline)}
    draft=b.execute('registration-preview',{**args,**values})
    from frontend.registration import confirm_local
    command={**args,**values,'registration_draft':draft['draft']}
    with pytest.raises(ValueError,match='确认'):confirm_local(b,command)
    response=submit(app,b,'import-folder',{**command,'confirm_nexus_identity':True})
    assert 'result' in response,response
    result=response['result'];managed=Path(result['managed_path'])
    assert (managed/'reframework.dll').read_bytes()==b'ISOLATED TEST PAYLOAD'
    assert (managed/'.info/offline/index.html').is_file()
    assert r.db.get_mod_backup_row(result['mod_pk'])['app_id']==2054972


def test_direct_local_import_cannot_skip_identity_confirmation(registration):
    app,r,b,context=registration;args=context(2054972)
    source=r.path('incoming/direct-local');source.mkdir();(source/'content.bin').write_bytes(b'PAYLOAD')
    values={**args,'platform':'Nexus','source':str(source),'title':'Direct','url':'https://www.nexusmods.com/dragonsdogma2/mods/8'}
    denied=submit(app,b,'import-folder',values)
    assert '确认' in denied['error'] and not b.adapter.assets(2054972)
    denied=submit(app,b,'import-folder',{**values,'url':'https://www.nexusmods.com/stardewvalley/mods/8','confirm_nexus_identity':True})
    assert '冲突' in denied['error'] and not b.adapter.assets(2054972)


def test_single_mhtml_uses_same_identity_guard_and_archives_page(registration):
    _,r,b,context=registration;args=context(2054972)
    source=page(r.path('incoming/single.mhtml'),mhtml=True)
    draft=b.execute('registration-preview',{**args,'kind':'offline','source':str(source)})
    assert draft['identity_confirmation']['required']
    result=b.execute_command('registration-save',{**args,'draft':draft['draft'],'confirm_nexus_identity':True})
    assert (Path(result['managed_path'])/'.info/offline/index.html').is_file()


def test_missing_page_url_can_be_completed_only_with_matching_identity(registration):
    app,r,b,context=registration;args=context(2054972)
    source=r.path('incoming/missing-url.html')
    source.write_text('<html><title>REFramework at Nexus Mods</title><meta property="og:title" content="REFramework"><div data-mod-id="8">Nexus Mods</div></html>',encoding='utf8')
    preview=b.execute('offline-preview',{**args,'paths':[str(source)]})
    assert preview['rows'][0]['can_supply_url'] and not preview['rows'][0]['enabled']
    bad=b.execute('offline-preview',{**args,'paths':[str(source)],'source_urls':{str(source):'https://www.nexusmods.com/dragonsdogma2/mods/9'}})
    assert not bad['rows'][0]['enabled'] and '不一致' in bad['rows'][0]['message']
    fixed=b.execute('offline-preview',{**args,'paths':[str(source)],'source_urls':{str(source):'https://www.nexusmods.com/dragonsdogma2/mods/8'}})
    assert fixed['rows'][0]['state']=='confirmation'
    result=submit(app,b,'offline-register',{**args,'preview':fixed['preview'],'ids':['0'],'confirmed_ids':['0']})
    assert result['result']['items'][0]['state']=='success',result
    asset=b.adapter.assets(2054972)[0]
    assert r.db.get_mod_display_info(asset.mod_pk).source_url.endswith('/dragonsdogma2/mods/8')


def test_single_page_companion_change_requires_a_new_preview(registration):
    _,r,b,context=registration;args=context(2054972)
    source=page(r.path('incoming/single.html'))
    companion=r.path('incoming/single_files');companion.mkdir();resource=companion/'asset.txt';resource.write_bytes(b'BEFORE')
    draft=b.execute('registration-preview',{**args,'kind':'offline','source':str(source)})
    resource.write_bytes(b'AFTER')
    with pytest.raises(ValueError,match='变化'):
        b.execute_command('registration-save',{**args,'draft':draft['draft'],'confirm_nexus_identity':True})
    assert not b.adapter.assets(2054972)


def test_game_change_expires_confirmation_and_preferences_do_not_authorize(registration):
    _,r,b,context=registration;args=context(2054972)
    draft=b.execute('registration-preview',{**args,'kind':'url','platform':'Nexus','url':'https://www.nexusmods.com/dragonsdogma2/mods/8'})
    b.remember_registration('2054972',json.dumps({'kind':'offline','platform':'Nexus'}))
    context(1623730)
    with pytest.raises(RuntimeError):b.execute_command('registration-save',{**args,'draft':draft['draft'],'confirm_nexus_identity':True})
    assert not b.adapter.assets(2054972)


def test_qt_card_controls_keep_selection_favorite_and_virtual_state_separate(registration):
    from PySide6.QtTest import QTest
    from frontend.web_window import WebLibraryWindow
    app,r,_,context=registration
    window=WebLibraryWindow(LibraryAdapter(r.db,writable=True),resources=r)
    window.resize(1000,500);window.show()
    def js(code):
        result=[];window.page.runJavaScript(code,result.append)
        for _ in range(300):
            if result:return result[0]
            QTest.qWait(10);time.sleep(.002)
        raise AssertionError(code)
    def wait(code):
        for _ in range(300):
            if js(code):return
            QTest.qWait(20);time.sleep(.002)
        raise AssertionError(code+' '+str(js("JSON.stringify({current:S.current?.name,detail:S.details,inspector:$('inspector').textContent,result:$('results').textContent,error:window.__testError})")))
    try:
        wait('typeof S!=="undefined"&&!!S.snapshot')
        js("window.addEventListener('unhandledrejection',e=>window.__testError=String(e.reason?.stack||e.reason));$('game').value='1142710';load()")
        wait('S.assets.length===3&&!S.busy&&document.querySelectorAll(".card").length>0')
        dimensions=json.loads(js("JSON.stringify([...document.querySelector('.card').querySelectorAll('.check,.cover-star')].map(e=>({w:e.offsetWidth,h:e.offsetHeight,top:getComputedStyle(e).top,radius:getComputedStyle(e).borderRadius})))"))
        assert dimensions==[{'w':32,'h':32,'top':'9px','radius':'8px'}]*2
        token=js("document.querySelector('.card').dataset.token")
        initial=js('S.rows[0].favorite')
        js("document.querySelector('.check').click()")
        assert js('S.selected.size===1&&S.current===null')
        js("document.querySelectorAll('.check')[1].click()")
        assert js('S.selected.size===2&&S.current===null')
        js("document.querySelector('.cover-star path').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        wait('!S.busy&&S.rows[0].favorite!=='+json.dumps(initial))
        assert js('S.selected.size===2&&S.current===null')
        token=js("document.querySelector('.card').dataset.token")
        assert js("document.querySelector('.cover-star').classList.contains('is-favorite')")==(not initial)
        js("document.querySelector('.card-body').click()")
        wait('!!S.details')
        assert js('S.selected.size===2')
        assert js("document.querySelectorAll('#inspector .cover-star').length")==0
        text=js("$('inspector').textContent")
        assert 'Manifest' not in text and 'Workshop 包' not in text and '原生' not in text
        js("$('viewport').scrollTop=$('viewport').scrollHeight;renderGrid()")
        QTest.qWait(100)
        js("$('viewport').scrollTop=0;renderGrid()")
        wait("!!document.querySelector('.card')")
        assert js("document.querySelector('.card').dataset.token")==token
        assert js("document.querySelector('.check').checked")
        assert js("document.querySelector('.cover-star').classList.contains('is-favorite')")==(not initial)
        first=window.bridge._assets[token]
        assert r.db.get_mod_display_info(first.mod_pk).favorite==(not initial)
        # The existing preview list can repair a missing URL without creating an empty Mod first.
        source=r.path('incoming/ui-missing-url.html')
        source.write_text('<html><title>REFramework at Nexus Mods</title><meta property="og:title" content="REFramework"><div data-mod-id="8">Nexus Mods</div></html>',encoding='utf8')
        js("$('game').value='2054972';load()")
        wait("!$('refresh').disabled&&S.assets.length===0")
        js("openRegistration();$('registration-kind').value='offline';updateRegistrationFields();$('offline-paths').value="+json.dumps(str(source))+";parseOfflinePages()")
        wait('!!NB.data&&!NB.busy')
        assert js('NB.data.rows[0].can_supply_url')
        js("const input=document.querySelector('[data-offline-url]');input.value='https://www.nexusmods.com/dragonsdogma2/mods/8';input.dispatchEvent(new Event('input',{bubbles:true}));parseOfflinePages()")
        wait("!!NB.data&&!NB.busy&&NB.data.rows[0].state==='confirmation'")
        js("document.querySelector('[data-offline-id]').click()")
        assert js("$('offline-submit').disabled")
        js("document.querySelector('[data-offline-confirm]').click();$('offline-submit').click()")
        wait('!!NB.result&&!NB.busy')
        assert js("NB.result.items[0].state==='success'")
        assert len(window.bridge.adapter.assets(2054972))==1
    finally:
        while not window.bridge.shutdown():app.processEvents();QTest.qWait(20)
        window.close();window.deleteLater();QTest.qWait(50)
