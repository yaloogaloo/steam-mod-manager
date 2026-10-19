"""Frozen real cover previews in Qt WebEngine, including the actual painted pixels."""
import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from frontend.web_window import WebLibraryWindow
from tools.cover_visual_probe import evaluate, wait_until, measure, contract_failures, compare_approved


FIXTURES = Path(__file__).parent / 'fixtures/cover_visual'


@pytest.mark.parametrize('viewport', [(1728, 900), (1180, 760)])
def test_actual_cover_pixels_and_visible_source(tmp_path, monkeypatch, viewport):
    manifest = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf8'))
    samples = manifest['samples'] + [manifest['no_cover']]
    urls = {}
    for sample in manifest['samples']:
        raw = (FIXTURES / sample['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == sample['input_sha256']
        urls[sample['key']] = 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / 'fixture.db'
    db = DatabaseManager(db_path)
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Cover fixture','2026-10-19')")
    identities = []
    for i in range(30):
        sample = samples[i % len(samples)]
        iid = str(uuid4()); identities.append(iid)
        db._conn.execute("INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,platform,updated_at) "
                         "VALUES(?,42,?,?,?,?,'2026-10-19')",
                         (i + 1, iid, str(i + 1), sample['key'] + ' ' + str(i), sample.get('platform', 'steam')))
    db._conn.commit(); db.close()
    db = DatabaseManager.open_existing(db_path, read_only=True)
    original = LibraryBridge.execute

    def frozen_read(bridge, operation, args):
        result = original(bridge, operation, args)
        if operation == 'cover':
            # Keep original identity/read admission; replace only the isolated image input.
            assert result is None
            return urls.get(bridge._assets[args['token']].name.split()[0])
        return result

    monkeypatch.setattr(LibraryBridge, 'execute', frozen_read)
    window = WebLibraryWindow(LibraryAdapter(db)); window.setFixedSize(*viewport); window.show()
    output = tmp_path / 'cover-artifacts'; output.mkdir()
    results = []; failures = []
    try:
        wait_until(window, "typeof S!=='undefined'&&S.assets.length===30")

        def select(iid):
            window.raise_(); window.activateWindow()
            value = json.dumps(iid)
            evaluate(window, 'inspect(S.assets.find(a=>a.internal_id===' + value + '))')
            wait_until(window, 'S.details&&S.current.internal_id===' + value)
            evaluate(window, "$('viewport').scrollTop=Math.floor(S.rows.findIndex(a=>a.internal_id===S.current.internal_id)/S.columns)*S.cell;renderGrid()")
            wait_until(window, "S.coverPending.size===0&&!S.coverActive&&[...document.querySelectorAll('.card.current img,#inspector-image img')].every(i=>i.complete&&i.naturalWidth>0)")
            QTest.qWait(80)

        for index, sample in enumerate(samples):
            select(identities[index])
            record, pictures = measure(window)
            record['sample'] = sample['key']
            record['failures'] = contract_failures(record, sample)
            if sample['key'] == 'no-cover':
                assert evaluate(window, "!document.querySelector('#inspector-image img')&&!!document.querySelector('#inspector-image .cover-placeholder')")
            record['approved_baseline'] = compare_approved(record, pictures['actual'], sample['key'], manifest, FIXTURES)
            if record['approved_baseline']['status'] == 'FAIL':
                record['failures'].append('approved visual reference differs')
            for name, picture in pictures.items():
                assert picture.save(str(output / (sample['key'] + '-' + name + '.png')))
            failures.extend((sample['key'], f) for f in record['failures'])
            results.append(record)
            if index == 0:
                # Exercise approval/diff handling using a TEST-ONLY reference;
                # this never promotes a screenshot in the real fixture manifest.
                reference = output / 'test-only-reference.png'
                assert pictures['actual'].save(str(reference))
                approval = {'user_visual_review':{'approved_references':[{
                    'key':sample['key'],'viewport':record['viewport'],'dpr':record['dpr'],
                    'file':reference.name,'sha256':hashlib.sha256(reference.read_bytes()).hexdigest(),
                    'approval_evidence':'TEST ONLY: local comparator fixture, not user approval'}]}}
                assert compare_approved(record,pictures['actual'],sample['key'],approval,output)['status']=='PASS'
                wrong = pictures['actual'].copy(); wrong.fill(0xffffffff)
                assert compare_approved(record,wrong,sample['key'],approval,output)['status']=='FAIL'
                approval['user_visual_review']['approved_references'][0]['sha256']='wrong'
                with pytest.raises(AssertionError,match='hash mismatch'):
                    compare_approved(record,pictures['actual'],sample['key'],approval,output)
        # Scroll out, select another resource, then return with the cover cache warm.
        select(identities[-2]); select(identities[0])
        returned, _ = measure(window)
        assert returned['image']['resource_sha256'] == samples[0]['input_sha256']
        failures.extend(('warm-cache-return', f) for f in contract_failures(returned, samples[0]))
        # Negative control: reintroduce the original Grid minimum in memory.
        evaluate(window, "document.querySelector('.card.current img').style.minHeight='auto'")
        QTest.qWait(100)
        broken, broken_pictures = measure(window)
        detected = contract_failures(broken, samples[0])
        assert 'required caption region is clipped' in detected
        assert 'native Qt pixels differ from centered-cover reference' in detected
        for name, picture in broken_pictures.items():
            assert picture.save(str(output / ('negative-control-' + name + '.png')))
        (output / 'negative-control.json').write_text(json.dumps({'measurements':broken,'detected':detected},indent=2),encoding='utf8')
        (output / 'results.json').write_text(json.dumps(results,indent=2),encoding='utf8')
        assert not failures, failures
    finally:
        assert window.bridge.shutdown()
        window.close(); app.processEvents(); db.close()
