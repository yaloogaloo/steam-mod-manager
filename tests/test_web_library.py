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
        evaluate('openDetail();')
        assert evaluate("$('detail').open")
        evaluate("$('detail').close();closeInspector();")
        assert evaluate('S.selected.size === 1')
        # 5,000 synthetic rows are a stress check, never production visual evidence.
        evaluate("S.assets=Array.from({length:5000},(_,i)=>({...S.assets[0],token:'stress-'+i,name:'Stress '+i}));filter();")
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
