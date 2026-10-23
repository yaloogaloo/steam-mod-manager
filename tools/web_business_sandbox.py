"""Launch actual editable WebEngine UI against an explicitly isolated fixture.

Never opens the production database or activates a production mutation context.
This launcher is temporary integration admission, not the formal product entry.
"""
import argparse
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def open_sandbox(root, *, initialize=False):
    from core.paths import project_root
    from core.db_manager import DatabaseManager
    root = Path(root).resolve()
    production = project_root().resolve()
    if root == Path(root.anchor) or root.is_relative_to(production) or production.is_relative_to(root):
        raise ValueError('隔离目录不能包含项目或位于项目内')
    from services.mutation_context import assert_mutation_allowed
    assert_mutation_allowed(root)
    marker = root / '.smm-business-sandbox'
    database = root / 'data' / 'mod_manager.db'
    if not database.resolve().is_relative_to(root):
        raise ValueError('数据库不能指向隔离目录之外')
    if initialize:
        if root.exists() and any(root.iterdir()):
            raise FileExistsError('初始化必须使用空的新隔离目录')
        database.parent.mkdir(parents=True)
        marker.write_text('isolated-fixture-v1', encoding='utf8')
        db = DatabaseManager(database)
        with db._lock:
            for game in (42, 43):
                db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(?,?,?)",
                                 (game, f'隔离验收游戏 {game}', '2026-10-15'))
                db._conn.execute("INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,description,updated_at) VALUES(?,?,?,?,?,?,?)",
                                 (game + 100, game, str(uuid4()), f'external-{game}',
                                  f'隔离 Mod {game}', '这是来源描述，用户编辑不能覆盖此字段。', '2026-10-15'))
            db._conn.commit()
        return db
    if not marker.is_file() or marker.read_text('utf8') != 'isolated-fixture-v1':
        raise ValueError('缺少隔离验收标记，拒绝打开')
    return DatabaseManager.open_existing(database, read_only=False)


def main():
    parser = argparse.ArgumentParser()
    from tools.agent_workspace import require_workspace
    parser.add_argument('--root', type=require_workspace, required=True)
    parser.add_argument('--init', action='store_true')
    parser.add_argument('--verify', action='store_true', help='Run actual UI writes and save Windows evidence')
    args = parser.parse_args()
    if args.verify and not args.init:
        parser.error('--verify requires --init and a fresh isolated directory')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QLockFile
    from services.mutation_context import activate_test_context, reset_mutation_context
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    token = activate_test_context([Path(args.root).resolve()])
    db = None
    window = None
    lock = None
    app = QApplication(sys.argv[:1])
    try:
        db = open_sandbox(args.root, initialize=args.init)
        lock = QLockFile(str(Path(args.root).resolve() / 'data' / 'app_instance.lock'))
        if not lock.tryLock(0):
            raise RuntimeError('该隔离窗口已在运行')
        window = WebLibraryWindow(LibraryAdapter(db, writable=True))
        window.show()
        if args.verify:
            verify_window(app, window, db, Path(args.root).resolve())
        return app.exec()
    finally:
        idle = window is None or window.bridge.shutdown()
        if db is not None and idle:
            db.close()
        if lock is not None:
            lock.unlock()
        reset_mutation_context(token)
        if not idle:
            raise RuntimeError('隔离业务任务尚未退出')


def verify_window(app, window, db, root):
    import json
    import time
    from PySide6.QtCore import QTimer
    output = root / 'evidence'
    output.mkdir(exist_ok=True)
    phase = {'value': 0, 'busy': False}
    started = time.monotonic()
    def tick():
        if phase['busy']:
            return
        if time.monotonic() - started > 45:
            (output / 'failure.txt').write_text('UI verification timed out', encoding='utf8')
            app.exit(1)
            return
        phase['busy'] = True
        window.page.runJavaScript("JSON.stringify({loaded:typeof S!=='undefined'&&S.assets.length>0,detail:typeof S!=='undefined'&&!!S.details,busy:typeof S!=='undefined'&&!!S.busy,favorite:typeof S!=='undefined'&&S.current?.favorite,name:typeof S!=='undefined'&&S.current?.name,editor:document.getElementById('editor').open})", advance)
    def advance(raw):
        phase['busy'] = False
        if not raw:
            return
        state = json.loads(raw)
        p = phase['value']
        if p == 0 and state['loaded']:
            window.page.runJavaScript('inspect(S.assets[0]);')
            phase['value'] = 1
        elif p == 1 and state['detail']:
            window.grab().save(str(output / '01-before.png'))
            window.page.runJavaScript("$('favorite-action').click();")
            phase['value'] = 2
        elif p == 2 and state['favorite'] and not state['busy']:
            window.grab().save(str(output / '02-favorite.png'))
            window.page.runJavaScript("document.querySelector('[data-command=edit]').click();$('edit-name').value='已保存的用户名称';$('edit-description').value='已通过真实 WebChannel 保存的用户描述。';$('edit-notes').value='隔离运行证据';")
            phase['value'] = 3
        elif p == 3 and state['editor']:
            window.grab().save(str(output / '03-editor.png'))
            window.page.runJavaScript("$('edit-form').requestSubmit();")
            phase['value'] = 4
        elif p == 4 and state['name'] == '已保存的用户名称' and not state['busy'] and not state['editor']:
            window.grab().save(str(output / '04-saved.png'))
            info = db.get_mod_display_info(142)
            report = {'favorite': info.favorite, 'display_name': info.user_display_name,
                      'custom_description': info.custom_description, 'notes': info.user_notes,
                      'source_description': info.steam_description, 'database': str(db.db_path),
                      'other_game_unchanged': not db.get_mod_display_info(143).favorite,
                      'dpi': app.primaryScreen().devicePixelRatio()}
            (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            phase['value'] = 5
            app.quit()
    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(200)


if __name__ == '__main__':
    raise SystemExit(main())
