"""Web capability contracts use isolated persistence only."""
import json
from uuid import uuid4
import pytest
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge


@pytest.fixture
def bridge(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'fixture.db'
    db = DatabaseManager(path)
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Game','2026-01-01')")
    uid = str(uuid4())
    db._conn.execute("INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,updated_at,local_size_bytes,local_size_status,type_id,deploy_status) VALUES(17,42,?,'external-17','Real title','2026-01-01',0,'ok',3,'deployed')", (uid,))
    db._conn.commit()
    db.close()
    (tmp_path / 'mod_types.json').write_text(json.dumps({'games': {'42': {'types': [{'id': 3, 'name': 'Type'}]}}}))
    ro = DatabaseManager.open_existing(path)
    result = LibraryBridge(LibraryAdapter(ro))
    yield result
    assert result.shutdown()
    ro.close()


def test_authoritative_card_fields_and_identity(bridge):
    result = bridge.execute('library', {'game': 42})
    row = result['assets'][0]
    assert row['name'] == 'Real title'
    assert row['workspace_id'] == 'external-17'
    assert row['internal_id'] != row['workspace_id']
    assert row['size'] == 0 and row['type'] == 'Type' and row['deployed']
    detail = bridge.execute('detail', {'token': row['token']})
    assert detail['mod_pk'] == 17
    assert detail['workspace_id'] == 'external-17'
    assert detail['actions']['primary']['id'] == 'redeploy'
    assert detail['actions']['undeploy']['id'] == 'undeploy'


@pytest.mark.parametrize('status, primary, undo, platform', [
    ('not_deployed', 'deploy', False, 'steam'), ('deployed', 'redeploy', True, 'steam'),
    ('failed', 'deploy', False, 'steam'), ('', 'unconfirmed', False, 'steam'),
    ('unexpected', 'unconfirmed', False, 'steam'),
    ('deployed ', 'unconfirmed', False, 'steam'),
    ('not_deployed', 'deploy', False, 'nexus'), ('not_deployed', 'deploy', False, 'other'),
])
def test_actions_use_raw_deploy_state_not_display_fallback(tmp_path, status, primary, undo, platform):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'states.db'
    db = DatabaseManager(path)
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Game','2026-01-01')")
    db._conn.execute("INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,updated_at,deploy_status,source_url,platform) VALUES(17,42,?,'external','Title','2026-01-01',?,'https://[invalid',?)", (str(uuid4()), status, platform))
    db._conn.commit()
    db.close()
    db = DatabaseManager.open_existing(path)
    bridge = LibraryBridge(LibraryAdapter(db))
    try:
        row = bridge.execute('library', {'game': 42})['assets'][0]
        actions = bridge.execute('detail', {'token': row['token']})['actions']
        assert actions['primary']['id'] == primary
        assert bool(actions['undeploy']) == undo
        assert not actions['primary']['enabled'] and '只读' in actions['primary']['reason']
        ids=[a['id'] for a in actions['menu']]
        assert len(ids)==len(set(ids))
        assert {'content-add','content-rescan','asset-management','tags','cover'} <= set(ids)
        assert not {'deploy','redeploy','undeploy','edit'} & set(ids)
        if status=='failed':assert actions['primary']['label']=='重试部署'
        assert all(a['icon'] and not a['enabled'] and a['reason'] for a in actions['menu'])
        assert not {'deploy', 'redeploy', 'undeploy', 'edit'} & {a['id'] for a in actions['menu']}
        assert 'URL 未通过校验' in actions['menu'][0]['reason']
        if platform=='other':assert not {'import-offline','save-offline'} & set(ids)
        else:assert actions['menu'][3]['id'] == ('import-offline' if platform=='nexus' else 'save-offline')
    finally:
        assert bridge.shutdown()
        db.close()


def test_actions_reject_stale_identity(bridge):
    from dataclasses import replace
    asset = bridge.adapter.assets(42)[0]
    changed = replace(asset, internal_id=str(uuid4()))
    actions = bridge.inspector_actions(changed, 'https://example.com')
    assert actions['primary']['id'] == 'unconfirmed'
    assert actions['undeploy'] is None
    assert '身份异常' in actions['primary']['reason']


@pytest.mark.parametrize('operation', ['deploy', 'redeploy', 'undeploy', 'edit', 'save-offline',
    'import-offline', 'open-source', 'open-folder', 'open-offline', 'change-cover',
    'refresh-metadata', 'execute'])
def test_inspector_operations_have_no_execution_bridge(bridge, operation):
    messages = []
    bridge.response.connect(messages.append)
    bridge.request('blocked', operation, '{"path":"D:/arbitrary"}')
    assert 'Unsupported read capability' in messages[-1]


def test_bridge_rejects_write_and_arbitrary_path(bridge):
    messages = []
    bridge.response.connect(messages.append)
    bridge.request('1', 'favorite', '{}')
    assert 'Unsupported read capability' in messages[-1]
    with pytest.raises(LookupError):
        bridge.execute('cover', {'token': 'D:/production/secret.png'})
    with pytest.raises(LookupError):
        bridge.execute('library', {'game': 99})


def test_invalid_cover_is_an_error_not_missing_data(bridge, tmp_path, monkeypatch):
    path = tmp_path / 'broken.png'
    path.write_bytes(b'not an image')
    monkeypatch.setattr(bridge.adapter, 'cover_file', lambda asset: path)
    row = bridge.execute('library', {'game': 42})['assets'][0]
    with pytest.raises(ValueError, match='无法解码'):
        bridge.execute('cover', {'token': row['token']})


def test_window_is_offrecord_and_disallows_local_network(bridge):
    from frontend.web_window import WebLibraryWindow
    from PySide6.QtWebEngineCore import QWebEngineSettings
    window = WebLibraryWindow(bridge.adapter)
    assert window.profile.isOffTheRecord()
    assert not window.page.settings().testAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls)
    assert not window.page.settings().testAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls)
    window.close()


def test_real_webchannel_render_selection_and_virtualization(bridge):
    from frontend.web_window import WebLibraryWindow
    from PySide6.QtTest import QTest
    app = QApplication.instance()
    window = WebLibraryWindow(bridge.adapter)
    window.show()

    def evaluate(script):
        result = []
        window.page.runJavaScript(script, result.append)
        for _ in range(100):
            if result:
                return result[0]
            QTest.qWait(20)
        raise AssertionError('JavaScript callback timed out')

    try:
        for _ in range(150):
            if evaluate("typeof S!=='undefined' && S.assets.length === 1"):
                break
            QTest.qWait(20)
        else:
            raise AssertionError('Actual WebChannel did not load the isolated DAL')
        assert evaluate("document.querySelector('.card-title').textContent") == 'Real title'
        assert evaluate("$('inspector').hidden")
        evaluate("toggleSelection(S.assets[0].token);inspect(S.assets[0]);")
        for _ in range(100):
            if evaluate('!!S.details'):
                break
            QTest.qWait(20)
        assert evaluate('S.selected.size === 1 && !!S.details')
        assert not evaluate("$('inspector').hidden")
        assert evaluate("document.querySelectorAll('.resultbar').length===0 && !document.getElementById('browse-hint')")
        assert evaluate("document.querySelectorAll('.inspector-properties .badge').length===3")
        assert evaluate("[...document.querySelectorAll('.inspector-scroll .facts dt')].every(e=>!['类型 / 分类','本地大小','部署状态'].includes(e.textContent))")
        evaluate("globalThis.originalType=S.current.type;S.current.type='非常长的真实类型名称'.repeat(15);drawInspector(S.current,S.details);")
        assert evaluate("document.querySelector('.inspector-properties').getBoundingClientRect().height<=53 && [...document.querySelectorAll('.inspector-properties .badge')].every(e=>e.getBoundingClientRect().height===24 && e.getBoundingClientRect().right<=document.querySelector('.inspector-scroll').getBoundingClientRect().right)")
        evaluate('S.current.type=originalType;drawInspector(S.current,S.details);')
        assert evaluate("JSON.stringify([...document.querySelectorAll('[data-command]')].map(e=>e.dataset.command))") == '["redeploy","undeploy","edit"]'
        assert evaluate("[...document.querySelectorAll('[data-command]')].every(e=>e.disabled && e.title.includes('只读'))")
        assert evaluate("new Set([...document.querySelectorAll('.inspector-command-row button')].map(e=>e.getBoundingClientRect().top)).size===1")
        assert evaluate("[...document.querySelectorAll('.inspector-command-row button')].every(e=>e.getBoundingClientRect().right<=document.querySelector('.inspector-footer').getBoundingClientRect().right)")
        evaluate("globalThis.bodyHeight=document.querySelector('.inspector-scroll').clientHeight;openMoreMenu();")
        assert evaluate("$('more-menu').matches(':popover-open') && document.querySelector('.inspector-scroll').clientHeight===bodyHeight")
        assert evaluate("[...document.querySelectorAll('#more-menu [role=menuitem]')].every(e=>e.querySelectorAll('svg').length===1 && e.getAttribute('aria-disabled')==='true')")
        evaluate("globalThis.requestCount=serial;document.querySelector('#more-menu [role=menuitem]').click();")
        assert evaluate('serial===requestCount')
        evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));")
        assert evaluate("document.activeElement.dataset.action==='open-folder'")
        evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));")
        assert evaluate("!$('more-menu').matches(':popover-open') && !$('inspector').hidden && document.activeElement.id==='inspector-more'")
        # Feed real Qt input to the Chromium widget, including outside-click dismissal.
        from PySide6.QtCore import QPoint, Qt
        evaluate('openMoreMenu();')
        point = json.loads(evaluate("JSON.stringify({x:$('search').getBoundingClientRect().left+20,y:$('search').getBoundingClientRect().top+15})"))
        QTest.mouseClick(window.view.focusProxy(), Qt.LeftButton, Qt.NoModifier, QPoint(round(point['x']), round(point['y'])))
        QTest.qWait(100)
        assert evaluate("!$('more-menu').matches(':popover-open') && !$('inspector').hidden")
        evaluate('openGameMenu();')
        assert evaluate("$('game-menu').matches(':popover-open') && getComputedStyle($('game-menu')).backgroundColor==='rgb(40, 54, 61)'")
        QTest.keyClick(window.view.focusProxy(), Qt.Key_Escape)
        QTest.qWait(100)
        assert evaluate("!$('game-menu').matches(':popover-open') && document.activeElement.id==='game-picker'")
        evaluate('openDetail();')
        assert evaluate("$('detail').open")
        evaluate("$('detail').close();closeInspector();")
        assert evaluate('S.selected.size === 1')
        # 5,000 synthetic rows are a stress check, never production visual evidence.
        evaluate("S.assets=Array.from({length:5000},(_,i)=>({...S.assets[0],token:'stress-'+i,name:'Stress '+i,search_fields:['Stress '+i]}));filter();")
        assert evaluate("S.rows.length === 5000 && document.querySelectorAll('.card').length < 100")
        assert evaluate("$('viewport').clientHeight >= 2*S.cell-12")
        evaluate("$('viewport').scrollTop=50000;renderGrid();")
        assert evaluate("document.querySelectorAll('.card').length < 100")
        # The same first-visible asset remains visible after changing column count.
        evaluate('globalThis.savedAnchor=browseAnchor();')
        evaluate("$('inspector').hidden=false;reflow();restoreAnchor(savedAnchor);")
        assert evaluate("S.rows.findIndex(a=>a.token===savedAnchor.token)>=Math.floor($('viewport').scrollTop/S.cell)*S.columns && S.rows.findIndex(a=>a.token===savedAnchor.token)<(Math.floor($('viewport').scrollTop/S.cell)+1)*S.columns")
        evaluate("$('search').value='Stress 4999';filter();")
        assert evaluate('S.rows.length === 1')
    finally:
        assert window.bridge.shutdown()
        window.close()
        app.processEvents()
