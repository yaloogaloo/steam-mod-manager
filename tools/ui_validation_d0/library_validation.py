"""Isolated P4 Qt exercise. Synthetic database; never launches production main."""
import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.agent_workspace import require_workspace
    parser.add_argument('--root', type=require_workspace, required=True)
    parser.add_argument('--platform', choices=['windows', 'offscreen'], default='windows')
    args = parser.parse_args()
    root = args.root.resolve()
    repo = Path(__file__).resolve().parents[2]
    if root == repo or repo in root.parents or root.exists():
        raise ValueError('Require a new output directory outside repository')
    root.mkdir(parents=True)
    os.environ['SMM_TEST_DB'] = str(root / 'fixture.db')
    os.environ['QT_QPA_PLATFORM'] = args.platform
    os.environ['QT_SCALE_FACTOR'] = '1'
    for name in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA'):
        folder = root / name.lower()
        folder.mkdir()
        os.environ[name] = str(folder)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))
    denied = []
    def audit(event, values):
        if event.startswith(('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.startfile', 'os.system')):
            denied.append(event)
            raise PermissionError(event)
        if event == 'sqlite3.connect' and values[0] != ':memory:':
            value = str(values[0])
            from urllib.parse import unquote, urlparse
            path = Path(unquote(urlparse(value).path).lstrip('/') if value.startswith('file:') else value).resolve()
            if root not in path.parents:
                raise PermissionError('SQLite outside fixture root')
        if event == 'open' and isinstance(values[0], (str, bytes)):
            flags = values[2] or 0
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                path = Path(os.fsdecode(values[0])).resolve()
                if root not in path.parents:
                    raise PermissionError('Write outside fixture root: ' + str(path))
    sys.addaudithook(audit)
    from PySide6.QtCore import Qt, QTimer, QItemSelectionModel
    from PySide6.QtGui import QImage, QColor, QPainter, QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QPushButton
    from core import paths
    paths._PROJECT_ROOT = root
    from core.db_manager import DatabaseManager
    from services.mutation_context import activate_test_context
    activate_test_context([root])
    app = QApplication([])
    app.setOrganizationName('SMM_P4_FIXTURE')
    app.setApplicationName('LibraryValidation')
    app.setStyle('Fusion')
    if args.platform == 'offscreen':
        QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc')
    db = DatabaseManager(root / 'fixture.db')
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'合成测试游戏 · 北境编年史','2026-01-01')")
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(43,'合成空资产游戏','2026-01-01')")
    for i in range(48):
        folder = root / 'mod' / str(i)
        folder.mkdir(parents=True)
        identity = str(uuid4())
        info = folder / '.info'
        info.mkdir()
        (info / 'metadata.json').write_text(json.dumps({'internal_id': identity}), 'utf-8')
        image = QImage(640, 360, QImage.Format_RGB32)
        image.fill(QColor.fromHsv((i*23)%360, 85, 85))
        painter = QPainter(image)
        painter.setPen(QColor('#dce7e5'))
        painter.setFont(QFont('Microsoft YaHei UI', 32))
        painter.drawText(image.rect(), Qt.AlignCenter, f'SYNTHETIC\nMOD {i+1:02d}')
        painter.end()
        cover = folder / 'cover.png'
        image.save(str(cover))
        db._conn.execute('''INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,description,
            platform,source_url,last_known_path,folder_present,cover_path,favorite,deploy_status,category,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (1000+i,42,identity,'fixture-'+str(i),
             ['北境：地形与季节', '精细角色外观', '界面与字体扩展', '跨越千年的编年史与文化体系：超长名称布局验证'][i%4]+f' {i+1:02d}',
             ('[b]合成描述[/b]\n仅验证真实数据库与 UI 数据合同，不是用户 Mod。\n'*80),
             'steam','https://example.invalid/mod/'+str(i),str(folder),1,
             str(cover) if i%7 else '', i%5==0,'deployed' if i%3==0 else 'not_deployed',
             '界面' if i%2 else '内容','2026-01-01'))
    db._conn.commit()
    from frontend.library_adapter import LibraryAdapter
    from frontend.shell import WorkbenchWindow
    adapter = LibraryAdapter(db, writable=True)
    window = WorkbenchWindow(adapter.games(), library_adapter=adapter)
    page = window.library_view
    window.game_selector.setCurrentIndex(window.game_selector.findData('42'))
    window.show()
    QTest.qWait(500)
    result = {'data': 'synthetic SQLite fixture', 'platform': app.platformName(), 'tests': [], 'captures': []}
    def check(name, value):
        result['tests'].append({'name': name, 'pass': bool(value)})
        (root/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        assert value, name
    check('real DAL game load', len(adapter.games()) >= 2)
    check('real DAL asset rows', page.model.rowCount() == 48)
    page.grid.setCurrentIndex(page.model.index(0))
    QTest.qWait(100)
    check('Inspector description', bool(page.description.toPlainText()))
    first = page.current_asset
    QTest.mouseClick(page.favorite_button, Qt.LeftButton)
    QTest.qWait(100)
    check('favorite persisted', adapter.detail(first).favorite != first.favorite)
    collection = adapter.create_collection(42, '合成精选')
    adapter.add_members(collection.collection_id, adapter.assets(42)[:2])
    check('collection real FK membership', len(adapter.members(collection.collection_id)) == 2)
    page.reload()
    QTest.qWait(100)
    page.search.setText('字体')
    QTest.qWait(60)
    check('search filters', page.model.rowCount() == 12)
    page.density.setCurrentIndex(1)
    check('density preserves query', page.search.text() == '字体' and page.model.rowCount() == 12)
    page.search.clear()
    page.grid.setCurrentIndex(page.model.index(0))
    page.grid.selectionModel().select(page.model.index(1), QItemSelectionModel.Select)
    check('multiple selection', len(page.grid.selectedIndexes()) == 2)
    selected_id = page.current_asset.internal_id
    QTest.mouseClick(page.inspector_button, Qt.LeftButton)
    check('Inspector collapsed', page.inspector.isHidden())
    QTest.mouseClick(page.inspector_button, Qt.LeftButton)
    check('Inspector restored', not page.inspector.isHidden() and page.current_asset.internal_id == selected_id)
    def close_detail():
        dialogs = [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]
        check('full detail opens', len(dialogs) == 1)
        dialogs[0].grab().save(str(root/'full_detail.png'))
        dialogs[0].accept()
    QTimer.singleShot(150, close_detail)
    page.full_detail()
    check('full detail return selection', page.current_asset.internal_id == selected_id)
    for width, height in [(1920,1080),(1520,900)]:
        window.resize(width, height)
        QTest.qWait(250)
        for compact in (False, True):
            page.density.setCurrentIndex(int(compact))
            page.grid.verticalScrollBar().setValue(0)
            QTest.qWait(350)
            filename = f'library_{width}_{height}_{"compact" if compact else "standard"}.png'
            pix = window.grab()
            check('capture '+filename, pix.save(str(root/filename)))
            viewport = page.grid.viewport().rect()
            rects = [page.grid.visualRect(page.model.index(i)) for i in range(page.model.rowCount())]
            visible = [r for r in rects if viewport.contains(r)]
            screen = app.primaryScreen()
            result['captures'].append({'file':filename,'requested':[width,height],
                'logical':[window.width(),window.height()], 'pixels':[pix.width(),pix.height()],
                'dpr':window.devicePixelRatioF(),'available_screen':screen.availableGeometry().getRect(),
                'grid_viewport':viewport.getRect(),'card':rects[0].getRect(),
                'fully_visible':len(visible),'rows':len({r.y() for r in visible}),
                'inspector_reading':page.description.viewport().rect().getRect()})
    page.status.setCurrentIndex(1)
    check('canonical deployed filter', all(a.deployed for a in page.model.rows) and len(page.model.rows)==16)
    page.status.setCurrentIndex(0)
    window.game_selector.setCurrentIndex(window.game_selector.findData('43'))
    QTest.qWait(80)
    check('empty game clears selection', page.model.rowCount()==0 and page.current_asset is None)
    window.game_selector.setCurrentIndex(window.game_selector.findData('42'))
    QTest.qWait(80)
    check('game switch reload', page.model.rowCount()==48)
    check('no real dangerous workers', not any(name.startswith('ui.') for name in sys.modules))
    window.close()
    QTest.qWait(80)
    check('close drains workers', page.model.pool.activeThreadCount()==0 and not window.isVisible())
    db.close()
    check('database closed', db.is_closed())
    check('network/external calls absent', not denied)
    result['denied'] = denied
    (root/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),'utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
