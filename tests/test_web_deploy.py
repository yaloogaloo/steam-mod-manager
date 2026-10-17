"""Actual WebEngine -> DeployWorker -> original deployer -> files/DB refresh."""
import json
from threading import Event
import time
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow
from frontend.web_bridge import LibraryBridge
from tests.test_isolated_deploy import environment


def evaluate(window, script):
    result = []
    window.page.runJavaScript(script, result.append)
    for _ in range(150):
        if result:
            return result[0]
        QTest.qWait(20)
    raise AssertionError('JavaScript callback timed out')


def wait_ui(window, condition):
    for _ in range(300):
        if evaluate(window, condition):
            return
        # QtTest's native polling may retain the GIL; let the real Python
        # QThread execute between observations rather than starving it.
        time.sleep(0.01)
        QTest.qWait(20)
    state = evaluate(window, "JSON.stringify({busy:S.busy,message:$('results').textContent,pending:requests.size,state:S.details?.actions?.state})")
    raise AssertionError(condition + ': ' + state)


def test_real_ui_deployment_roundtrip(environment):
    resources, source, target, identity = environment
    app = QApplication.instance() or QApplication([])
    window = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
    window.show()
    js = lambda script: evaluate(window, script)
    try:
        wait_ui(window, "typeof S!=='undefined' && S.assets.length===1")
        js('inspect(S.assets[0]);')
        wait_ui(window, '!!S.details')
        for action in ('deploy', 'redeploy', 'undeploy'):
            if action == 'redeploy':
                (source / 'payload.txt').write_text('MOD-v2', encoding='utf8')
            assert js(f"!document.querySelector('[data-command={action}]').disabled")
            js(f"document.querySelector('[data-command={action}]').click();")
            wait_ui(window, "!S.busy && !!S.details && $('results').textContent==='操作成功'")
            expected = 'ORIGINAL' if action == 'undeploy' else 'MOD-v2' if action == 'redeploy' else 'MOD-v1'
            assert target.read_text('utf8') == expected
            assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == ('not_deployed' if action == 'undeploy' else 'deployed')
            assert js("[...document.querySelectorAll('[data-command]')].map(e=>e.dataset.command).join(',')") == ('deploy,edit' if action == 'undeploy' else 'redeploy,undeploy,edit')
            assert js(f"S.current.deployed==={'false' if action == 'undeploy' else 'true'}")
            assert js("new Set([...document.querySelectorAll('.inspector-command-row button')].map(e=>e.getBoundingClientRect().top)).size===1")
        assert (target.parent / 'untouched.txt').read_text('utf8') == 'KEEP'
    finally:
        assert window.bridge.shutdown()
        window.close()
        app.processEvents()


def test_worker_queued_context_duplicate_and_shutdown(environment, monkeypatch):
    from ui.deploy_thread import DeployWorker
    resources, source, target, identity = environment
    app = QApplication.instance() or QApplication([])
    resources.db.update_game_deploy_config(43, name='Other isolated game')
    bridge = LibraryBridge(LibraryAdapter(resources.db, writable=True), resources=resources)
    snapshot = bridge.execute('library', {'game': 42})
    asset = snapshot['assets'][0]
    args = {'token':asset['token'], 'internal_id':asset['internal_id'], 'game':'42', 'snapshot':snapshot['snapshot']}
    entered, release = Event(), Event()
    original = DeployWorker.__init__
    def initialize(worker, *positional, **kwargs):
        preflight = kwargs['preflight']
        def pause():
            entered.set()
            assert release.wait(5)
            preflight()
        kwargs['preflight'] = pause
        original(worker, *positional, **kwargs)
    monkeypatch.setattr(DeployWorker, '__init__', initialize)
    messages = []
    bridge.response.connect(lambda raw: messages.append(json.loads(raw)))
    bridge.command('first','deploy',json.dumps(args))
    assert entered.wait(2)
    bridge.command('duplicate','deploy',json.dumps(args))
    assert any(m['request']=='duplicate' and 'error' in m for m in messages)
    bridge.execute('library', {'game':43})
    assert not bridge.shutdown()
    release.set()
    for _ in range(200):
        app.processEvents()
        if any(m['request']=='first' for m in messages):
            break
        QTest.qWait(10)
    assert any(m['request']=='first' and 'error' in m for m in messages)
    assert target.read_text('utf8') == 'ORIGINAL'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'not_deployed'
    assert bridge.shutdown()


def test_ui_actual_failure_and_readonly_refusal(environment):
    from core.db_manager import DatabaseManager
    resources, source, target, identity = environment
    app = QApplication.instance() or QApplication([])
    window = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
    window.show()
    try:
        wait_ui(window, "typeof S!=='undefined' && S.assets.length===1")
        evaluate(window, 'inspect(S.assets[0]);')
        wait_ui(window, '!!S.details')
        (source / 'payload.txt').unlink()
        evaluate(window, "document.querySelector('[data-command=deploy]').click();")
        wait_ui(window, "!S.busy && $('results').textContent.startsWith('操作失败：')")
        assert target.read_text('utf8') == 'ORIGINAL'
        assert not evaluate(window, 'S.current.deployed')
        assert evaluate(window, "!document.querySelector('[data-command=undeploy]')")
    finally:
        assert window.bridge.shutdown()
        window.close()
        app.processEvents()
    db = DatabaseManager.open_existing(resources.db.db_path)
    bridge = LibraryBridge(LibraryAdapter(db))
    snapshot = bridge.execute('library', {'game':42})
    asset = snapshot['assets'][0]
    messages = []
    bridge.response.connect(lambda raw: messages.append(json.loads(raw)))
    try:
        for action in ('deploy','redeploy','undeploy'):
            bridge.command(action, action, json.dumps({'token':asset['token'], 'internal_id':asset['internal_id'], 'game':'42', 'snapshot':snapshot['snapshot']}))
        assert len(messages)==3 and all('error' in m for m in messages)
        assert not bridge._jobs and not bridge._busy
    finally:
        assert bridge.shutdown()
        db.close()
