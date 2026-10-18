"""Actual WebEngine Inspector integration; all writes use a temporary root."""
import hashlib
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def prepare(root):
    from tools.organization_validation import initialize
    resources, ids = initialize(root)
    with resources.bind():
        resources.path('data/mod_types.json').write_text(json.dumps({
            'games': {'1142710': {'types': [{'id': 3, 'name': '内容'}, {'id': 4, 'name': '前置'}]}}
        }, ensure_ascii=False), encoding='utf8')
        resources.db.set_mod_type_id(ids[0].mod_id, 3)
        for label in ('内容', '自建标签'):
            resources.db.add_mod_tag(ids[1].mod_id, 'category', label)
        resources.db.update_mod_user_metadata(ids[1].mod_id, {'custom_description':
            '[quote][b]未闭合 BBCode 回归[list][*]正文\n' * 5
            + '<table><tr><td>' + 'long-token-' * 80 + '<td>第二列'})
    return resources, ids


def verify(window, resources, ids):
    def js(code):
        result = []
        window.page.runJavaScript(code, result.append)
        for _ in range(300):
            if result:
                return result[0]
            QTest.qWait(10)
            time.sleep(.002)
        raise AssertionError('JavaScript callback timed out')

    def wait(code):
        for _ in range(500):
            if js('Boolean(' + code + ')'):
                return
            QTest.qWait(10)
            time.sleep(.002)
        raise AssertionError(code)

    db = resources.db
    commands = []
    original = window.bridge.execute_command

    def trace(action, args):
        commands.append(action)
        return original(action, args)

    window.bridge.execute_command = trace
    console = []
    window.page.javaScriptConsoleMessage = lambda level, message, line, source: console.append(message)
    wait('typeof S!=="undefined"&&S.snapshot')
    js("$('game').value='1142710';load()")
    wait("!$('refresh').disabled&&S.assets.length===2")
    js('inspect(S.assets.find(a=>a.internal_id===' + json.dumps(ids[1].internal_id) + '))')
    wait('S.details&&S.details.internal_id===' + json.dumps(ids[1].internal_id))
    assert js("!$('manage-types') && !$('editor').open")
    assert js("!document.querySelector('.inspector-scroll').innerText.includes('资产状态')")
    assert js("!document.querySelector('.inspector-identity .deployed') && !document.querySelector('.inspector-properties')")
    assert js("$('inspector-type-action').textContent==='未设置' && !$('inspector-type-action').querySelector('path[d=\"'+paths.pencil+'\"]')")
    assert js("document.querySelector('.inspector-source').closest('.inspector-identity')!==null && document.querySelector('.inspector-user-tags>svg path').getAttribute('d')===paths.tags")
    assert js("S.current.size===8 && document.querySelector('.inspector-size').textContent==='8 B'")
    assert js("!document.querySelector('.inspector-scroll').innerText.includes('描述来源') && !document.querySelector('.deployment-notice')")
    tags = db.get_category_tags(ids[1].mod_id)
    assert len(tags) == 3
    # Preserve reading space below the short-title identity and three-tag property group.
    assert js("document.querySelector('.inspector-summary').getBoundingClientRect().top-document.querySelector('.inspector-top').getBoundingClientRect().bottom<=210")
    assert json.loads(js("JSON.stringify([...document.querySelectorAll('.inspector-tag')].map(e=>e.textContent))")) == tags
    assert js("document.querySelector('.inspector-type-property').nextElementSibling.matches('.inspector-user-tags') && document.querySelector('.inspector-user-tags').nextElementSibling.matches('.facts') && document.querySelector('.facts').nextElementSibling.textContent==='描述摘要'")
    footer = js("JSON.stringify(document.querySelector('.inspector-footer').getBoundingClientRect().toJSON())")
    js("document.querySelector('.inspector-scroll').scrollTop=100000")
    assert js("JSON.stringify(document.querySelector('.inspector-footer').getBoundingClientRect().toJSON())") == footer
    assert js("document.querySelector('.inspector-footer').parentElement===$('inspector') && !document.querySelector('.inspector-summary .inspector-footer') && document.querySelector('.inspector-scroll').scrollWidth<=document.querySelector('.inspector-scroll').clientWidth+1")
    js("document.querySelector('.inspector-scroll').scrollTop=0;$('inspector-type-action').focus()")
    before = db._conn.total_changes
    catalog = resources.path('data/mod_types.json').read_bytes()
    QTest.keyClick(window.view.focusProxy(), Qt.Key_Return)
    wait("$('inspector-type-menu').matches(':popover-open')")
    assert js("!$('editor').open && document.activeElement.id==='inspector-type-select'")
    QTest.keyClick(window.view.focusProxy(), Qt.Key_Escape)
    wait("!$('inspector-type-menu').matches(':popover-open')")
    assert db._conn.total_changes == before and not commands
    assert db.get_mod_type_id(ids[1].mod_id) is None
    cancel_focus = js('document.activeElement.id')
    assert cancel_focus == 'inspector-type-action'
    js("$('inspector-type-action').click()")
    wait("$('inspector-type-menu').matches(':popover-open')")
    QTest.qWait(150)
    window.grab().save(str(resources.path('multi-tags-type-picker.png')))
    js("$('inspector-type-select').value='3';$('inspector-type-select').dispatchEvent(new Event('change',{bubbles:true}))")
    wait("!S.busy && S.details.type_id===3 && $('inspector-type-action').textContent==='内容'")
    assert db.get_mod_type_id(ids[1].mod_id) == 3
    assert db.get_category_tags(ids[1].mod_id) == tags
    assert resources.path('data/mod_types.json').read_bytes() == catalog
    assert commands == ['type-set']
    assert js("document.activeElement.id==='inspector-type-action'")
    assert js("!$('editor').open && document.querySelectorAll('#inspector-type-action').length===1")
    QTest.qWait(150)
    window.grab().save(str(resources.path('multi-tags-assigned.png')))
    js("$('inspector-type-action').click()")
    wait("$('inspector-type-menu').matches(':popover-open')")
    js("$('inspector-type-select').value='';$('inspector-type-select').dispatchEvent(new Event('change',{bubbles:true}))")
    wait("!S.busy && S.details.type_id===null")
    assert db.get_mod_type_id(ids[1].mod_id) is None
    assert db.get_category_tags(ids[1].mod_id) == tags
    assert commands == ['type-set', 'type-set']
    js("$('open-detail').click()")
    wait("$('detail').open")
    js("$('detail-return').click();document.querySelector('[data-command=edit]').click()")
    wait("$('editor').open")
    js("$('edit-cancel').click();$('inspector-more').click();document.querySelector('[data-action=tags]').click()")
    wait("$('organization').open&&!!$('tag-query')")
    label = '超长用户标签_' * 8
    js("$('tag-query').value=" + json.dumps(label) + ";$('tag-add').click()")
    wait('!S.busy&&O.tags.tags.includes(' + json.dumps(label) + ')')
    assert db.get_mod_type_id(ids[1].mod_id) is None
    assert set(db.get_category_tags(ids[1].mod_id)) == set(tags + [label])
    js("$('organization-close').click()")
    wait("!$('organization').open&&S.details&&S.details.tags.includes(" + json.dumps(label) + ')')
    QTest.qWait(150)
    window.grab().save(str(resources.path('long-user-tag.png')))
    assert js("document.querySelector('.inspector-scroll').scrollWidth<=document.querySelector('.inspector-scroll').clientWidth+1")
    js("$('inspector-more').click();document.querySelector('[data-action=tags]').click()")
    wait("$('organization').open&&!!$('tag-query')")
    js("[...document.querySelectorAll('[data-remove-tag]')].find(e=>e.dataset.removeTag===" + json.dumps(label) + ').click()')
    wait('!S.busy&&!O.tags.tags.includes(' + json.dumps(label) + ')')
    js("$('organization-close').click()")
    assert db.get_category_tags(ids[1].mod_id) == tags
    assert db.get_mod_type_id(ids[1].mod_id) is None
    assert commands == ['type-set', 'type-set', 'tag-add', 'tag-remove']
    js('inspect(S.assets.find(a=>a.internal_id===' + json.dumps(ids[0].internal_id) + '))')
    wait('S.details&&S.details.internal_id===' + json.dumps(ids[0].internal_id))
    assert js("document.querySelector('.inspector-identity .deployed').textContent==='已部署' && !document.querySelector('.inspector-user-tags')")
    assert js("!document.querySelector('.inspector-scroll').innerText.includes('无标签')")
    assert not console
    return {'passed': True, 'commands': commands, 'cancel_writes': 0, 'cancel_focus': cancel_focus,
            'tags': tags, 'catalog_sha256': hashlib.sha256(catalog).hexdigest(), 'console': console}


def test_inspector_type_picker_and_user_tag_layout(tmp_path):
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    app = QApplication.instance() or QApplication([])
    resources, ids = prepare(tmp_path / 'inspector')
    window = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
    window.resize(1728, 900)
    window.show()
    try:
        assert verify(window, resources, ids)['passed']
    finally:
        window.bridge.shutdown()
        window.close()
        app.processEvents()
        resources.db.close()
