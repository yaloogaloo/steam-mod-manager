"""Interactive isolated deployment window, optionally capture real UI roundtrip."""
import argparse
import json
import sys
import time
import hashlib
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def production_inventory():
    """Read-only inventory of durable local production trees, not SQLite only.

    Large binary/CAS files use size, mtime and file identity; authority JSON/DB
    files also get content hashes. This is an observation, not a data backup.
    """
    project = Path(__file__).resolve().parents[1]
    files = {}
    for name in ('data', 'config', 'mod'):
        for path in (project / name).rglob('*'):
            if not path.is_file() or path.name == 'app_instance.lock':
                continue
            info = path.stat()
            record = {'size':info.st_size, 'mtime_ns':info.st_mtime_ns, 'inode':info.st_ino}
            if path.suffix.lower() in ('.json','.db','.ini','.xml'):
                record['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            files[str(path)] = record
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--init', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--expect-failure', action='store_true')
    parser.add_argument('--scenario', choices=('folder_copy', 'wh3'), default='folder_copy')
    parser.add_argument('--production-evidence', help='Hold the shared app lock for a short before/after observation')
    args = parser.parse_args()
    if args.expect_failure and not (args.init and args.verify):
        parser.error('--expect-failure requires --init --verify')
    if args.verify and not args.init:
        parser.error('--verify requires a fresh --init environment')
    if args.production_evidence and not args.verify:
        parser.error('--production-evidence is only allowed during bounded --verify')
    from PySide6.QtCore import QTimer, QLockFile
    from PySide6.QtWidgets import QApplication
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources, checked_sandbox_root, checked_resource_path
    from services.mutation_context import activate_test_context, reset_mutation_context
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    from tools.isolated_deploy_validation import initialize, snapshot
    if args.scenario == 'wh3':
        from tools.wh3_deploy_validation import initialize, verify_state
    game_id = '1142710' if args.scenario == 'wh3' else '42'
    root = checked_sandbox_root(args.root)
    production_before = None
    if args.production_evidence:
        from services.app_instance import acquire_instance_lock
        evidence_root = checked_sandbox_root(args.production_evidence)
        evidence_root.mkdir(parents=True, exist_ok=True)
        if not acquire_instance_lock():
            raise RuntimeError('旧版 SMM 仍在运行，拒绝生产对照采集')
        production_before = production_inventory()
        (evidence_root/'production-before.json').write_text(json.dumps(production_before), encoding='utf8')
        print(f'Production baseline captured: {len(production_before)} files', flush=True)
    token = activate_test_context([root])
    db = None
    window = None
    lock = None
    app = QApplication(sys.argv[:1])
    try:
        if args.init:
            resources, source, target, identity = initialize(root)
            db = resources.db
            if args.expect_failure:
                if args.scenario == 'wh3':
                    projection = root/'games/Warhammer3/used_mods.txt'
                    projection.unlink()
                    projection.mkdir()  # Real projection write failure.
                else:
                    (source / 'payload.txt').rename(source / '.info/held-payload.txt')
        else:
            marker = root / '.smm-deploy-sandbox'
            checked_resource_path(root, marker)
            if not marker.is_file():
                raise PermissionError('缺少部署隔离环境标记')
            database = root / 'data/mod_manager.db'
            checked_resource_path(root, database)
            db = DatabaseManager.open_existing(database, read_only=False)
            resources = BusinessResources(root, db)
        lock = QLockFile(str(root / 'data/app_instance.lock'))
        if not lock.tryLock(0):
            raise RuntimeError('该隔离部署窗口已运行')
        window = WebLibraryWindow(LibraryAdapter(db, writable=True), resources=resources)
        window.show()
        if not args.verify:
            # The fixture includes an empty second game for stale-request checks.
            # Open the populated game without changing subsequent user choices.
            startup = QTimer(window)
            def choose_initial_game():
                window.page.runJavaScript(f"typeof bridge!=='undefined'&&!!bridge&&!!document.querySelector('#game option[value=\"{game_id}\"]')", selected)
            def selected(ready):
                if ready:
                    startup.stop()
                    window.page.runJavaScript(f"$('game').value='{game_id}';load();")
            startup.timeout.connect(choose_initial_game)
            startup.start(250)
        if args.verify:
            if not args.init:
                raise PermissionError('自动验收必须使用新隔离环境')
            output = root / 'evidence'
            output.mkdir()
            report = {'scenario':args.scenario, 'dpi':app.primaryScreen().devicePixelRatio(), 'stages':{}, 'responses':[]}
            window.bridge.response.connect(lambda raw: report['responses'].append(json.loads(raw)) if 'deployment' in raw or 'error' in raw else None)
            phase = {'value':-1,'busy':False}
            started = time.monotonic()
            def capture(name):
                from PySide6.QtTest import QTest
                # State callbacks arrive before Chromium has painted the frame.
                # Suppress the polling timer during the short paint settle.
                phase['busy'] = True
                QTest.qWait(250)
                window.grab().save(str(output / (name+'.png')))
                report['stages'][name] = snapshot(resources, identity)
                if args.scenario == 'wh3' and not args.expect_failure:
                    report['stages'][name]['wh3'] = verify_state(resources, source, target, identity,
                        name in ('02-deployed', '03-redeployed'))
                phase['busy'] = False
            def tick():
                if phase['busy']:
                    return
                if time.monotonic()-started > 90:
                    report['failure'] = 'UI roundtrip timed out'
                    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
                    timer.stop()
                    window.close()
                    return
                phase['busy'] = True
                window.page.runJavaScript("JSON.stringify({connected:typeof bridge!=='undefined'&&!!bridge,loaded:typeof S!=='undefined'&&S.assets.length>0,details:typeof S!=='undefined'&&!!S.details,busy:typeof S!=='undefined'&&!!S.busy,state:typeof S!=='undefined'?S.details?.actions?.state:null,message:document.getElementById('results').textContent})", advance)
            def advance(raw):
                phase['busy'] = False
                if not raw:
                    return
                state = json.loads(raw)
                p = phase['value']
                if p==-1 and state['connected']:
                    window.page.runJavaScript(f"$('game').value='{game_id}';load();")
                    phase['value']=0
                elif p==0 and state['loaded']:
                    window.page.runJavaScript('inspect(S.assets[0]);')
                    phase['value']=1
                elif p==1 and state['details']:
                    capture('01-undeployed')
                    window.page.runJavaScript("document.querySelector('[data-command=deploy]').click();")
                    phase['value']=2
                elif p==2 and args.expect_failure and not state['busy'] and state['message'].startswith('操作失败：'):
                    assert target.read_text('utf8')==('WORKSHOP-ORIGINAL' if args.scenario=='wh3' else 'ORIGINAL')
                    capture('05-failed')
                    report['expected_failure_verified']=True
                    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
                    phase['value']=5
                    app.quit()
                elif p==2 and not state['busy'] and state['state']=='deployed':
                    assert target.read_text('utf8')==('WORKSHOP-ORIGINAL' if args.scenario=='wh3' else 'MOD-v1')
                    capture('02-deployed')
                    if args.scenario == 'wh3':
                        (resources.root/'games/Warhammer3/used_mods.txt').write_text('STALE',encoding='utf8')
                        report['stages']['before-redeploy'] = snapshot(resources, identity)
                        report['stages']['before-redeploy']['projection'] = 'STALE'
                    else:
                        (source/'payload.txt').write_text('MOD-v2',encoding='utf8')
                    window.page.runJavaScript("document.querySelector('[data-command=redeploy]').click();")
                    phase['value']=3
                elif p==3 and not state['busy'] and state['state']=='deployed' and target.read_text('utf8')==('WORKSHOP-ORIGINAL' if args.scenario=='wh3' else 'MOD-v2'):
                    capture('03-redeployed')
                    window.page.runJavaScript("document.querySelector('[data-command=undeploy]').click();")
                    phase['value']=4
                elif p==4 and not state['busy'] and state['state']=='not_deployed':
                    assert target.read_text('utf8')==('WORKSHOP-ORIGINAL' if args.scenario=='wh3' else 'ORIGINAL')
                    if args.scenario == 'folder_copy':
                        assert (target.parent/'untouched.txt').read_text('utf8')=='KEEP'
                    capture('04-restored')
                    report['success']=True
                    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
                    phase['value']=5
                    app.quit()
            timer = QTimer(window)
            timer.timeout.connect(tick)
            timer.start(250)
        result = app.exec()
        return 1 if args.verify and report.get('failure') else result
    finally:
        idle = window is None or window.bridge.shutdown()
        while not idle:
            # Keep both the DB and mutation context until the real writer ends.
            # Never terminate a thread during copying or original-file restore.
            time.sleep(0.05)
            app.processEvents()
            idle = window.bridge.shutdown()
        if db is not None and idle:
            db.close()
        if lock is not None:
            lock.unlock()
        reset_mutation_context(token)
        if production_before is not None:
            from services.app_instance import release_instance_lock
            try:
                production_after = production_inventory()
                changed = [p for p in production_before.keys()|production_after.keys() if production_before.get(p)!=production_after.get(p)]
                (evidence_root/'production-after.json').write_text(json.dumps(production_after), encoding='utf8')
                (evidence_root/'production-comparison.json').write_text(json.dumps({'unchanged':not changed,'changed_paths':changed,'files':len(production_before)},indent=2),encoding='utf8')
                print(f'Production comparison unchanged: {not changed}', flush=True)
            finally:
                release_instance_lock()
        if not idle:
            raise RuntimeError('部署线程尚未退出，未关闭数据库')


if __name__ == '__main__':
    raise SystemExit(main())
