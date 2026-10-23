"""Library grid keeps its scroll position and session order across refreshes."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    SOURCE_TYPE_NEXUS,
    ModFileEntry,
    ModFilesBundle,
)
from tests.helpers.identity import prove_managed_folder
from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow
from services.business_resources import BusinessResources

EVIDENCE = Path(__file__).resolve().parents[1] / '_tmp' / 'agent_runs' / 'library-scroll-stability'
COUNT = 30


def _js(window, code):
    result = []
    window.page.runJavaScript(code, result.append)
    for _ in range(400):
        if result:
            return result[0]
        QTest.qWait(10)
        time.sleep(0.002)
    raise AssertionError('JavaScript callback timed out')


def _wait(window, code, rounds=800):
    for _ in range(rounds):
        if _js(window, 'Boolean(' + code + ')'):
            return
        QTest.qWait(10)
        time.sleep(0.002)
    raise AssertionError(code)


def _sample(window):
    return json.loads(_js(window, """JSON.stringify({
      scroll: $('viewport').scrollTop,
      order: S.rows.map(a => a.internal_id),
      current: S.current && S.current.internal_id,
      names: S.rows.map(a => a.name),
      rect: (document.querySelector('.card.current') || document.querySelector('.card'))?.getBoundingClientRect().toJSON() || null
    })"""))


def _seed(path: Path):
    db = DatabaseManager(path)
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Browse Game','2024-01-01')")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    identities = []
    middle = None
    for index in range(COUNT):
        identity = str(uuid4())
        stamp = (base + timedelta(days=index)).isoformat()
        platform = 'nexus' if index == 15 else 'steam'
        db._conn.execute(
            """INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,updated_at,platform,deploy_status,local_size_bytes,local_size_status)
               VALUES(?,?,?,?,?,?,?,'not_deployed',0,'ok')""",
            (1000 + index, 42, identity, f'browse-{index:02d}', f'稳定卡片 {index:02d}', stamp, platform),
        )
        identities.append(identity)
        if index == 15:
            middle = identity
    db._conn.commit()
    db.set_mod_files(1015, ModFilesBundle(files=[
        ModFileEntry(id='Main-A.zip', filename='Main-A.zip', path='Main-A.zip', file_role=FILE_ROLE_NEXUS_MAIN,
                     source_type=SOURCE_TYPE_NEXUS, type=FILE_TYPE_MAIN, selected_for_deploy=True, enabled=True),
        ModFileEntry(id='Opt-B.zip', filename='Opt-B.zip', path='Opt-B.zip', file_role=FILE_ROLE_NEXUS_OPTIONAL,
                     source_type=SOURCE_TYPE_NEXUS, type=FILE_TYPE_OPTIONAL, selected_for_deploy=False, enabled=False),
    ]))
    folder = path.parents[1] / 'mod' / 'BrowseGame' / 'Stable15'
    prove_managed_folder(db, folder, handle=1015, title='稳定卡片 15', app_id=42, game_name='Browse Game', platform='nexus')
    db._conn.execute('UPDATE mods SET updated_at=? WHERE mod_id=1015', ((base + timedelta(days=15)).isoformat(),))
    db._conn.commit()
    return db, identities, middle


@pytest.mark.timeout(180)
def test_library_scroll_and_order_stay_until_the_user_changes_browse(tmp_path):
    app = QApplication.instance() or QApplication([])
    root = tmp_path / 'browse'
    (root / 'data').mkdir(parents=True)
    (root / 'mod').mkdir()
    db, identities, middle = _seed(root / 'data' / 'library.db')
    resources = BusinessResources(root, db)
    window = WebLibraryWindow(LibraryAdapter(db, writable=True), resources=resources)
    window.resize(1100, 720)
    window.show()
    evidence = {'middle': middle}
    try:
        _wait(window, "typeof S!=='undefined'&&S.snapshot&&S.assets.length===" + str(COUNT))
        _js(window, """
          const index = S.rows.findIndex(a => a.internal_id === %s);
          const row = Math.floor(index / S.columns);
          $('viewport').scrollTop = Math.max(0, row * S.cell - S.cell);
          renderGrid();
          inspect(S.rows[index]);
        """ % json.dumps(middle))
        _wait(window, "S.details&&S.details.internal_id===" + json.dumps(middle))
        QTest.qWait(150)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        before = _sample(window)
        evidence['before'] = before
        window.grab().save(str(EVIDENCE / 'before-middle.png'))
        assert before['current'] == middle
        assert before['scroll'] > 0
        assert middle in before['order']
        middle_index = before['order'].index(middle)
        assert 0 < middle_index < COUNT - 1

        _js(window, "document.querySelector('[data-command=edit]').click()")
        _wait(window, "$('editor').open")
        _js(window, "$('edit-name').value='已编辑卡片';$('edit-form').requestSubmit()")
        _wait(window, "!$('editor').open&&!S.busy&&S.rows.some(a=>a.name==='已编辑卡片')")
        edited = _sample(window)
        evidence['after_edit'] = edited
        window.grab().save(str(EVIDENCE / 'after-edit.png'))
        assert edited['scroll'] == before['scroll']
        assert edited['order'] == before['order']
        assert edited['current'] == middle
        assert edited['names'][middle_index] == '已编辑卡片'
        assert abs(edited['rect']['top'] - before['rect']['top']) < 2
        assert abs(edited['rect']['left'] - before['rect']['left']) < 2

        _js(window, "$('inspector-file-toggle').click()")
        _wait(window, "!$('inspector-files').hidden")
        _js(window, "document.querySelector('[data-file-note]').click()")
        _wait(window, "!!document.querySelector('#inspector-files textarea')")
        _js(window, "document.querySelector('#inspector-files textarea').value='位置说明';document.querySelector('[data-file-note-save]').click()")
        _wait(window, "!S.busy&&document.querySelector('.inspector-file-note')&&document.querySelector('.inspector-file-note').textContent==='位置说明'")
        noted = _sample(window)
        evidence['after_file_note'] = noted
        assert noted['scroll'] == before['scroll']
        assert noted['order'] == before['order']
        assert noted['current'] == middle

        _js(window, "$('refresh').click()")
        _wait(window, "!$('refresh').disabled&&!S.busy&&S.details&&S.details.internal_id===" + json.dumps(middle))
        refreshed = _sample(window)
        evidence['after_refresh'] = refreshed
        assert refreshed['scroll'] == before['scroll']
        assert refreshed['order'] == before['order']
        assert refreshed['current'] == middle
        assert refreshed['names'][middle_index] == '已编辑卡片'
        assert db._conn.execute('SELECT updated_at FROM mods WHERE internal_id=?', (middle,)).fetchone()[0] > '2024-01-16'

        _js(window, "$('library-sort').value='name';$('library-sort').dispatchEvent(new Event('change'))")
        _wait(window, "S.rows.every((a,i)=>a.sort_ranks.name===i)")
        by_name = _sample(window)
        evidence['after_name_sort'] = {'scroll': by_name['scroll'], 'order': by_name['order']}
        assert by_name['scroll'] == 0
        assert by_name['order'] != before['order']
        assert by_name['names'] == sorted(by_name['names'], key=str.casefold)

        _js(window, "$('library-sort').value='mtime';$('library-sort').dispatchEvent(new Event('change'))")
        _wait(window, "S.rows[0].internal_id===" + json.dumps(middle) + "&&S.rows.every((a,i)=>a.sort_ranks.mtime===i)")
        by_mtime = _sample(window)
        evidence['after_mtime_resort'] = {'scroll': by_mtime['scroll'], 'first': by_mtime['order'][0]}
        assert by_mtime['scroll'] == 0
        assert by_mtime['order'][0] == middle

        _js(window, """
          const max = Math.max(0, $('viewport').scrollHeight - $('viewport').clientHeight);
          const jump = Math.min(max, S.cell * 3);
          $('viewport').scrollTop = jump;
          renderGrid();
          globalThis.browseProbe = '';
          const pending = load();
          $('viewport').scrollTop = jump;
          pending.then(() => { globalThis.browseProbe = String($('viewport').scrollTop); });
          globalThis.expectedJump = jump;
        """)
        _wait(window, "globalThis.browseProbe!==''")
        manual = float(_js(window, 'globalThis.browseProbe'))
        expected = float(_js(window, 'globalThis.expectedJump'))
        evidence['manual_during_refresh'] = {'scroll': manual, 'expected': expected}
        assert abs(manual - expected) < 2
        assert _js(window, 'S.current&&S.current.internal_id') == middle

        _js(window, "$('viewport').scrollTop=0;renderGrid();globalThis.browseProbe='';load().then(()=>{globalThis.browseProbe=String($('viewport').scrollTop)})")
        _wait(window, "globalThis.browseProbe!==''")
        assert float(_js(window, 'globalThis.browseProbe')) == 0
        evidence['top'] = 0

        _js(window, """
          $('viewport').scrollTop=$('viewport').scrollHeight;renderGrid();
          globalThis.bottomBefore=$('viewport').scrollTop;
          globalThis.browseProbe='';
          load().then(()=>{globalThis.browseProbe=String($('viewport').scrollTop)});
        """)
        _wait(window, "globalThis.browseProbe!==''")
        bottom_before = float(_js(window, 'globalThis.bottomBefore'))
        bottom_after = float(_js(window, 'globalThis.browseProbe'))
        evidence['bottom'] = {'before': bottom_before, 'after': bottom_after}
        assert bottom_before > 0
        assert abs(bottom_after - bottom_before) < 2

        order_now = _js(window, "S.rows.map(a=>a.internal_id).join('|')")
        scroll_now = float(_js(window, "$('viewport').scrollTop"))
        _js(window, """
          globalThis.browseProbe='';
          const order = S.rows.map(a=>a.internal_id).join('|');
          const scroll = $('viewport').scrollTop;
          Promise.all([load(), load()]).then(() => {
            globalThis.browseProbe = JSON.stringify({
              scroll: $('viewport').scrollTop,
              order: S.rows.map(a=>a.internal_id).join('|'),
              expectedScroll: scroll,
              expectedOrder: order
            });
          });
        """)
        _wait(window, "globalThis.browseProbe!==''")
        raced = json.loads(_js(window, 'globalThis.browseProbe'))
        evidence['concurrent_refresh'] = raced
        assert abs(raced['scroll'] - scroll_now) < 2
        assert raced['order'] == order_now
        assert raced['order'] == raced['expectedOrder']
        (EVIDENCE / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        assert identities
    finally:
        window.bridge.shutdown()
        window.close()
        app.processEvents()
        db.close()
