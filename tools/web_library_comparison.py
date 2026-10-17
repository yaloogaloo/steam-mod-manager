"""Comparable onscreen captures of pre-WebEngine Widgets and P2.3.

Uses the same read-only DAL and real game as the new page. P2.3 is a design
comparison with real projected inputs, never a production acceptance result.
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer, QUrl, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from core.paths import project_root, DATA_DIR_NAME, DATABASE_FILENAME
from frontend.library_adapter import LibraryAdapter
from services.app_instance import acquire_instance_lock, release_instance_lock
from services.mutation_context import suspend_mutation_context, reset_mutation_context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['widgets', 'p23'], required=True)
    parser.add_argument('--game', default='916440')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    database = project_root() / DATA_DIR_NAME / DATABASE_FILENAME
    if output.is_relative_to(database.parent):
        raise ValueError('Evidence must be outside production data')
    output.mkdir(parents=True, exist_ok=True)
    if not acquire_instance_lock():
        raise RuntimeError('Existing SMM instance; comparison refused')
    from frontend.readonly_runtime import install_readonly_runtime
    install_readonly_runtime(database)
    token = suspend_mutation_context()
    app = QApplication([])
    db = DatabaseManager.open_existing(database)
    adapter = LibraryAdapter(db)
    games = adapter.games()
    game = next(g for g in games if g.key == args.game)
    ratio = app.primaryScreen().devicePixelRatio()
    if args.mode == 'widgets':
        from frontend.shell import WorkbenchWindow
        window = WorkbenchWindow(games, library_adapter=adapter)
        window.game_selector.setCurrentIndex(next(i for i, g in enumerate(games) if g.key == args.game))
        window.resize(round(1600/ratio), round(900/ratio))
        window.show()
        def capture():
            window.grab().save(str(output / 'widgets-library.png'))
            page = window.library_view
            if page.model.rows:
                page.grid.setCurrentIndex(page.model.index(0))
                QTimer.singleShot(700, capture_selected)
            else:
                app.quit()
        def capture_selected():
            window.grab().save(str(output / 'widgets-inspector.png'))
            app.quit()
        QTimer.singleShot(2000, capture)
    else:
        from frontend.web_window import WebLibraryWindow
        from frontend.web_bridge import LibraryBridge
        root = Path('D:/smm-product-redesign-p23/prototype')
        css = '\n'.join((root / name).read_text('utf-8') for name in ['tokens.css', 'styles.css', 'assets.css', 'responsive.css'])
        script = (root / 'app.js').read_text('utf-8')
        bridge = LibraryBridge(adapter)
        snapshot = bridge.execute('library', {'game': int(args.game)})
        rows = []
        for index, row in enumerate(snapshot['assets']):
            cover = bridge.execute('cover', {'token': row['token']})
            if cover:
                image = QImage.fromData(base64.b64decode(cover.split(',', 1)[1]))
                image = image.scaled(240, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                buffer = QBuffer()
                buffer.open(QIODevice.WriteOnly)
                image.save(buffer, 'JPEG', 65)
                cover = 'data:image/jpeg;base64,'+base64.b64encode(bytes(buffer.data())).decode('ascii')
            detail = bridge.execute('detail', {'token': row['token']})
            rows.append({'id': index+1, 'internal_id': row['internal_id'], 'workspace_id': row['workspace_id'],
                'mod_pk': detail['mod_pk'], 'game': 'stellaris', 'name': row['name'], 'source': row['platform'],
                'type': row['type'], 'tags': [row['category']] if row['category'] else [],
                'favorite': row['favorite'], 'status': 'missing' if row['status'] else 'deployed' if row['deployed'] else 'ready',
                'deployed': row['deployed'], 'cover': cover, 'size': round(row['size']/1024**2, 1) if row['size'] is not None else '未知',
                'description': detail['description']})
        script = re.sub(r'^const games=.*$', lambda _: 'const games='+json.dumps({'stellaris': {'name': game.name, 'letter': 'A', 'app': game.app_id, 'backend': None, 'strategy': '只读对照'}})+';', script, flags=re.M)
        # Callable replacement keeps literal backslashes in descriptions intact.
        script = re.sub(r'^const mods=.*$', lambda _: 'const mods='+json.dumps(rows, ensure_ascii=False)+';', script, flags=re.M)
        script = script.replace('inspectorOpen:true', 'inspectorOpen:false')
        script = script.replace("collections:[{id:1,name:'沉浸与探索',desc:'探索、音景与事件扩展。整理维度，不参与部署。',ids:[1,2,4,5,7],seed:3},{id:2,name:'界面与可读性',desc:'界面框架与字体。成员可以属于多个合集。',ids:[3,8,12,16],seed:1}]", 'collections:[]')
        # Static comparison has no bridge registered; only the read-only offline view.
        window = WebLibraryWindow(adapter)
        window.page.setWebChannel(None)
        window.channel.deregisterObject(window.bridge)
        # setHtml has a 2MB encoded URL limit; verify this comparison fits explicitly.
        from urllib.parse import quote
        html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; connect-src \'none\'"><style>'+css+'</style><body><div id="app"></div><div id="detail-root"></div><div id="modal-root"></div><div id="toast"></div><script>'+script.replace('</script', '<\\/script')+'</script></body></html>'
        if len(quote(html)) > 1900000:
            raise RuntimeError('P2.3 comparable data exceeds setHtml size limit; refuse partial dataset')
        window.view.setHtml(html, QUrl('about:blank'))
        window.show()
        def caption():
            window.page.runJavaScript("document.querySelector('.demo-mark').textContent='P2.3 对照 · 真实数据投影';document.querySelector('.footer-note').textContent='仅设计对照；原型功能不代表真实后端能力。';")
        def capture():
            caption()
            QTimer.singleShot(300, save_library)
        def save_library():
            window.grab().save(str(output / 'p23-library.png'))
            window.page.runJavaScript('S.inspectorOpen=true;S.inspect=1;render();')
            QTimer.singleShot(600, save_inspector)
        def save_inspector():
            caption()
            window.grab().save(str(output / 'p23-inspector.png'))
            app.quit()
        QTimer.singleShot(2500, capture)
    try:
        return app.exec()
    finally:
        if args.mode == 'widgets':
            assert window.library_view.shutdown_workers()
        else:
            assert window.bridge.shutdown() and bridge.shutdown()
        db.close()
        reset_mutation_context(token)
        release_instance_lock()


if __name__ == '__main__':
    raise SystemExit(main())
