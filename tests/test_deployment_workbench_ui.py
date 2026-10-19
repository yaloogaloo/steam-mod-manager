"""Real Qt workbench presentation and isolated order/snapshot interactions."""
import json
import os
import stat

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow, OfflinePage
from tools.web_order_sandbox import initialize
from tools.cover_visual_probe import evaluate, wait_until


@pytest.mark.parametrize('viewport', [(1728, 900), (1180, 760)])
def test_workbench_details_spacing_and_order_contract(tmp_path, monkeypatch, viewport):
    app = QApplication.instance() or QApplication([])
    resources = initialize(tmp_path / 'workbench')
    with resources.bind():
        ids = [r[0] for r in resources.db._conn.execute('SELECT mod_id FROM mods WHERE deploy_status="deployed" ORDER BY mod_id')]
        resources.db.update_mod_user_metadata(ids[0], {'display_name':'中文长标题 ' * 10,
            'custom_description':'[b]Safe description[/b]\n' + 'Long readable description.\n' * 120})
        resources.db.add_category_tag(ids[0], 'First')
        resources.db.add_category_tag(ids[0], 'Second')
        sidecar = resources.path('mod/Warhammer3/NativeWorkshop/.info/metadata.json')
        metadata = json.loads(sidecar.read_text('utf8'))
        metadata['description'] = '[b]Safe description[/b]\n' + 'Long readable description.\n' * 120
        sidecar.write_text(json.dumps(metadata), encoding='utf8')
    errors = []
    monkeypatch.setattr(OfflinePage, 'javaScriptConsoleMessage', lambda self, level, message, line, source: errors.append(message))
    window = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
    window.setFixedSize(*viewport); window.show()
    js = lambda code: evaluate(window, code)
    wait = lambda code: wait_until(window, code)
    output = tmp_path / 'evidence'; output.mkdir()
    order_path = resources.path('config/load_order/wh3.json')
    projection = resources.path('games/Warhammer3/used_mods.txt')
    try:
        wait("typeof S!=='undefined'&&S.snapshot")
        js("$('game').value='1142710';load()")
        wait("!$('refresh').disabled&&S.assets.length===3")
        js("$('deployment-nav').click()")
        wait('deploymentData?.rows.length===2')
        assert window.grab().save(str(output / 'default-layout.png'))
        (output / 'layout.json').write_text(js("JSON.stringify({heading:document.querySelector('.deployment-heading').getBoundingClientRect().toJSON(),caption:$('deployment-caption').getBoundingClientRect().toJSON(),status:$('deployment-status').getBoundingClientRect().toJSON(),firstRow:document.querySelector('.deployment-row').getBoundingClientRect().toJSON()})"), encoding='utf8')
        assert js("$('deployment-caption').textContent==='仅显示已部署 Mod · 调整顺序后自动保存'")
        assert js("$('deployment-status').getBoundingClientRect().height===0")
        assert js("document.querySelector('.deployment-heading').getBoundingClientRect().height<64")
        original_order = json.loads(js('JSON.stringify(deploymentData.order)'))
        original_files = (order_path.read_bytes(), projection.read_bytes())
        js("document.querySelector('[data-order-view]').click()")
        wait('deploymentDetail?.data')
        assert js("S.space==='deployment'&&!$('deployment-inspector').hidden&&document.querySelector('.deployment-help').hidden")
        assert js("!$('deployment-inspector').querySelector('.inspector-footer,[data-command],#open-detail')")
        assert js("$('deployment-inspector').querySelector('.inspector-type-name').textContent==='未设置'")
        assert js("$('deployment-inspector').querySelectorAll('.inspector-tag').length===2")
        assert js("!$('deployment-inspector').querySelector('script,iframe')")
        assert js("$('deployment-inspector').querySelector('.inspector-scroll').scrollHeight>$('deployment-inspector').querySelector('.inspector-scroll').clientHeight")
        assert js("document.documentElement.scrollWidth===innerWidth")
        assert (order_path.read_bytes(), projection.read_bytes()) == original_files
        assert window.grab().save(str(output / 'current-detail.png'))
        js("closeDeploymentInspector(false);$('deployment-list').style.maxHeight='100px';$('deployment-list').scrollTop=30;globalThis.savedScroll=$('deployment-list').scrollTop;document.querySelector('[data-order-view]').click()")
        wait('deploymentDetail?.data')
        assert js("Math.abs($('deployment-list').scrollTop-savedScroll)<1")
        js("$('deployment-detail-close').click()")
        assert js("Math.abs($('deployment-list').scrollTop-savedScroll)<1")
        js("$('deployment-list').style.maxHeight='';document.querySelector('[data-order-view]').click()")
        wait('deploymentDetail?.data')
        # Selection changes refresh only the side panel, leaving the order rows intact.
        js("globalThis.firstOrderRow=document.querySelector('.deployment-row');document.querySelectorAll('[data-order-view]')[1].click()")
        wait('deploymentDetail?.data&&deploymentDetail.asset.internal_id==='+json.dumps(original_order[1]))
        assert js('firstOrderRow===document.querySelector(".deployment-row")')
        assert js("!$('deployment-inspector').querySelector('.inspector-user-tags')")
        js("$('deployment-detail-close').click()")
        assert js("!deploymentDetail&&!document.querySelector('.deployment-help').hidden")
        # Exercise real isolated buttons and drag handlers while the Inspector is open.
        js("document.querySelector('[data-order-view]').click()")
        wait('deploymentDetail?.data')
        for selector, expected in [('[data-order-down]', original_order[::-1]),
                                   ('[data-order-bottom]', original_order),
                                   ('[data-order-up]:not(:disabled)', original_order[::-1]),
                                   ('[data-order-top]:not(:disabled)', original_order)]:
            js('document.querySelector('+json.dumps(selector)+').click()')
            wait('!orderSaving&&deploymentData.order[0]==='+json.dumps(expected[0]))
            assert json.loads(order_path.read_text('utf8'))['order'] == expected
            assert js("S.space==='deployment'&&!!deploymentDetail")
        js("{const r=document.querySelectorAll('[data-order-id]'),d=new DataTransfer();r[0].querySelector('.drag-handle').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:d}));r[1].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:d}));r[1].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:d}));}")
        wait('!orderSaving&&deploymentData.order[0]==='+json.dumps(original_order[1]))
        assert json.loads(order_path.read_text('utf8'))['order'] == original_order[::-1]
        # Partial failure must stay visible; retry continues using the original Backend.
        os.chmod(projection, stat.S_IREAD)
        try:
            js("document.querySelector('[data-order-down]').click()")
            wait('!orderSaving&&!!deploymentData.projection_error')
            assert js("$('deployment-status').textContent.includes('投影失败')&&!$('order-retry').hidden")
            assert js("$('deployment-status').getBoundingClientRect().height>0")
            assert window.grab().save(str(output / 'partial-failure.png'))
        finally: os.chmod(projection, stat.S_IREAD | stat.S_IWRITE)
        js("$('order-retry').click()")
        wait('!orderSaving&&!deploymentData.projection_error')
        wait("$('deployment-status').textContent===''")
        # Real snapshot CRUD; details temporarily use the existing comparison region.
        js("$('snapshot-tab').click()")
        wait("snapshotData?.game==='1142710'")
        assert js("!deploymentDetail&&$('snapshot-status').getBoundingClientRect().height===0")
        gap = js("document.querySelector('.snapshot-body').getBoundingClientRect().top-document.querySelector('#snapshot-view .deployment-heading').getBoundingClientRect().bottom")
        assert 11.9 <= gap <= 16.1, gap
        for name in ['First snapshot', 'Second snapshot']:
            js("$('snapshot-create').click();$('snapshot-name').value="+json.dumps(name)+";$('snapshot-form').requestSubmit()")
            wait("!snapshotBusy&&!$('snapshot-dialog').open&&snapshotData.records.some(r=>r.name==="+json.dumps(name)+')')
        js("document.querySelector('[data-snapshot-open]').click()")
        wait('snapshotData.comparison')
        rid = js('snapshotSelected')
        js("document.querySelector('[data-snapshot-detail]').click()")
        wait('deploymentDetail?.data')
        assert js("S.space==='deployment'&&workbenchView==='snapshots'&&$('snapshot-comparison').hidden")
        assert window.grab().save(str(output / 'snapshot-detail.png'))
        js("$('deployment-detail-close').click()")
        assert js("!$('snapshot-comparison').hidden&&snapshotSelected==="+str(rid))
        js("document.querySelectorAll('[data-snapshot-open]')[1].click()")
        wait('snapshotSelected!=='+str(rid)+'&&snapshotData.comparison')
        # A late detail reply cannot resurrect an old object after a game switch.
        js("globalThis.originalRpc=rpc;rpc=(op,args,...rest)=>op==='detail'?new Promise(resolve=>globalThis.lateDetail=()=>originalRpc(op,args,...rest).then(resolve)):originalRpc(op,args,...rest);document.querySelector('[data-snapshot-detail]').click()")
        wait("typeof lateDetail==='function'")
        js("$('game').value='43';load()")
        wait("!$('refresh').disabled&&snapshotData?.game==='43'")
        js('lateDetail();rpc=originalRpc')
        QTest.qWait(150)
        assert js("!deploymentDetail&&$('deployment-inspector').hidden")
        js("$('current-deployment-tab').click()")
        wait("deploymentData?.game==='43'")
        assert '自动保存' not in js("$('deployment-caption').textContent")
        assert not errors, errors
    finally:
        window.bridge.shutdown(); window.close(); app.processEvents(); resources.db.close()
