"""Small coverage gate: preserve legacy inventory and executable entry references."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_legacy_migration_records_and_declared_wiring_remain_present():
    document=json.loads((ROOT/'docs/migration/frontend_coverage.json').read_text('utf8'))
    from tools.render_frontend_coverage import render
    assert (ROOT/'docs/migration/legacy_frontend_parity.md').read_text('utf8')==render(document)
    rows=document['features']
    assert {row['id'] for row in rows}==set(range(1,110)) and len(rows)==109
    states={'EQUIVALENT','MISSING','MISWIRED','PARTIAL','OVERBLOCKED','UI DEFECT','BACKEND UNSUPPORTED'}
    handlers=document['legacy_handlers']
    assert len(handlers)==93 and len({row['id'] for row in handlers})==93
    for row in rows:
        assert row['status'] in states
        assert row['legacy_entry_and_service'] and row['contract']
        if row['status']=='EQUIVALENT':
            assert row['checks'] and row['formal_result'] and '待' not in row['formal_result']
        if row['status']=='BACKEND UNSUPPORTED':assert row.get('backend_evidence')
        for check in row['checks']:
            path=ROOT/check['file']
            assert path.is_file(),(row['id'],check)
            assert check['symbol'] in path.read_text('utf8'),(row['id'],check)
