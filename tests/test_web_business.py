"""Real WebChannel -> command -> DAL -> rendered refresh, isolated filesystem."""
import json
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from frontend.web_window import WebLibraryWindow
from tools.web_business_sandbox import open_sandbox


@pytest.fixture
def business(tmp_path):
    db = open_sandbox(tmp_path / 'sandbox', initialize=True)
    db._conn.execute("UPDATE mods SET cover_path=?, user_override_fields=? WHERE mod_id=142",
                     ('user-cover.png', '{"cover":true}'))
    db._conn.commit()
    yield LibraryAdapter(db, writable=True)
    db.close()


def command_args(bridge):
    snapshot = bridge.execute('library', {'game': 42})
    asset = snapshot['assets'][0]
    return {'token': asset['token'], 'internal_id': asset['internal_id'],
            'game': '42', 'snapshot': snapshot['snapshot'], 'value': True}


def test_command_identity_whitelist_and_actual_write(business):
    app = QApplication.instance() or QApplication([])
    bridge = LibraryBridge(business)
    args = command_args(bridge)
    bridge.execute_command('favorite-set', args)
    assert business.db.get_mod_display_info(142).favorite
    with pytest.raises(ValueError):
        bridge.execute_command('favorite-set', {**args, 'value': 'true'})
    with pytest.raises(RuntimeError):
        bridge.execute_command('favorite-set', {**args, 'game': '43'})
    with pytest.raises(PermissionError):
        bridge.execute_command('sql', args)
    bridge.execute('library', {'game': 43})
    with pytest.raises(RuntimeError):
        bridge.execute_command('favorite-set', args)
    assert not business.db.get_mod_display_info(143).favorite
    assert bridge.shutdown()


def test_queue_duplicate_and_game_switch_refuse_before_write(business):
    from threading import Event
    from PySide6.QtCore import QRunnable
    class Blocker(QRunnable):
        def run(self):
            entered.set()
            release.wait(5)
    app = QApplication.instance() or QApplication([])
    bridge = LibraryBridge(business)
    bridge.pool.setMaxThreadCount(1)
    args = command_args(bridge)
    entered, release = Event(), Event()
    messages = []
    bridge.response.connect(lambda raw: messages.append(json.loads(raw)))
    bridge.pool.start(Blocker())
    assert entered.wait(2)
    try:
        bridge.command('first', 'favorite-set', json.dumps(args))
        bridge.command('duplicate', 'favorite-set', json.dumps(args))
        assert any(m['request'] == 'duplicate' and 'error' in m for m in messages)
        bridge.request('switch', 'library', json.dumps({'game': '43'}))
        release.set()
        for _ in range(200):
            app.processEvents()
            if any(m['request'] == 'first' for m in messages):
                break
            QTest.qWait(10)
        assert any(m['request'] == 'first' and 'error' in m for m in messages)
        assert not business.db.get_mod_display_info(142).favorite
        assert not business.db.get_mod_display_info(143).favorite
    finally:
        release.set()
        assert bridge.shutdown()


def test_readonly_command_is_rejected_in_python(business):
    from core.db_manager import DatabaseManager
    db = DatabaseManager.open_existing(business.db.db_path)
    bridge = LibraryBridge(LibraryAdapter(db))
    messages = []
    bridge.response.connect(lambda raw: messages.append(json.loads(raw)))
    try:
        args = command_args(bridge)
        bridge.command('readonly', 'favorite-set', json.dumps(args))
        assert len(messages) == 1 and 'error' in messages[0]
        assert not db.get_mod_display_info(142).favorite
        assert not bridge._busy
    finally:
        assert bridge.shutdown()
        db.close()


def test_real_ui_favorite_editor_refresh_and_cancel(business, tmp_path):
    app = QApplication.instance() or QApplication([])
    window = WebLibraryWindow(business)
    window.show()

    def js(script):
        result = []
        window.page.runJavaScript(script, result.append)
        for _ in range(150):
            if result:
                return result[0]
            QTest.qWait(20)
        raise AssertionError('JavaScript timed out')

    def wait(script):
        for _ in range(200):
            if js(script):
                return
            QTest.qWait(20)
        raise AssertionError(script+' '+str(js("JSON.stringify({busy:S.busy,current:S.current?.favorite,message:$('results').textContent})")))

    try:
        wait("typeof S!=='undefined' && S.assets.length===1")
        js('inspect(S.assets[0]);')
        wait('!!S.details')
        assert js("!document.querySelector('[data-command=edit]').disabled")
        js("document.querySelector('[data-favorite]').click();document.querySelector('[data-favorite]').click();")
        wait('!S.busy && !!S.current && S.current.favorite')
        assert business.db.get_mod_display_info(142).favorite
        assert js("!document.querySelector('[data-favorite]').disabled")
        js("document.querySelector('[data-favorite]').click()")
        wait('!S.busy && !!S.current && !S.current.favorite')
        assert not business.db.get_mod_display_info(142).favorite
        assert js("!document.querySelector('[data-favorite]').disabled")
        js("document.querySelector('[data-favorite]').click()")
        wait('!S.busy && !!S.current && S.current.favorite')
        js("document.querySelector('[data-command=edit]').click();$('edit-name').value='取消的名称';$('edit-cancel').click();")
        assert business.db.get_mod_display_info(142).user_display_name != '取消的名称'
        js("document.querySelector('[data-command=edit]').click();$('edit-name').value='用户名称';$('edit-description').value='用户描述';$('edit-notes').value='用户备注';$('edit-form').requestSubmit();")
        wait("!S.busy && ! $('editor').open && !!S.current && S.current.name==='用户名称'")
        info = business.db.get_mod_display_info(142)
        assert info.user_display_name == '用户名称'
        assert info.custom_description == '用户描述'
        assert info.user_notes == '用户备注' and info.favorite
        assert info.steam_description == '这是来源描述，用户编辑不能覆盖此字段。'
        js("closeInspector();document.querySelector('[data-favorite]').click()")
        wait('!S.busy && !S.current && !S.assets[0].favorite')
        assert js("$('inspector').hidden && S.selected.size===0")
        js("document.querySelector('[data-favorite]').click()")
        wait('!S.busy && !S.current && S.assets[0].favorite')
        js('inspect(S.assets[0])')
        wait('!!S.details')

        row = business.db._conn.execute('SELECT cover_path,user_override_fields FROM mods WHERE mod_id=142').fetchone()
        assert row['cover_path'] == 'user-cover.png'
        assert json.loads(row['user_override_fields'])['cover'] is True
        assert js("document.querySelector('.card-title').textContent==='用户名称'")
        js("openEditor();runCommand('metadata-save',{fields:{display_name:'非法字段',source_url:'https://example.com'}});")
        wait("!S.busy && $('edit-error').textContent.length>0")
        assert business.db.get_mod_display_info(142).user_display_name == '用户名称'
        assert js("$('editor').open")
        js("$('edit-cancel').click();")
        window.grab().save(str(tmp_path / 'writable-ui.png'))
        js("document.querySelector('[data-favorite]').click();")
        wait('!S.busy && !!S.current && !S.current.favorite')
        assert not business.db.get_mod_display_info(142).favorite
        assert not business.db.get_mod_display_info(143).favorite
        assert js("Math.round($('viewport').getBoundingClientRect().top-document.querySelector('.querybar').getBoundingClientRect().bottom)===12")
    finally:
        assert window.bridge.shutdown()
        window.close()
        app.processEvents()


def test_category_is_separate_from_type_source_and_user_tags(business):
    app=QApplication.instance() or QApplication([])
    b=LibraryBridge(business)
    try:
        args=command_args(b);before=business.db.get_mod_display_info(142)
        business.db.add_mod_tag(142,'category','keep-tag')
        b.execute_command('metadata-save',{**args,'fields':{'display_name':before.user_display_name or '',
            'custom_description':before.custom_description or '', 'user_notes':before.user_notes or '', 'category':'UI tools'}})
        after=business.db.get_mod_display_info(142)
        assert after.category=='UI tools'
        assert after.platform==before.platform and after.source_url==before.source_url
        assert business.db.get_category_tags(142)==['keep-tag']
        assert 'category' not in b.execute('library',{'game':42})['assets'][0]
        assert business.db.get_mod_display_info(142).category=='UI tools'
    finally:b.shutdown()
