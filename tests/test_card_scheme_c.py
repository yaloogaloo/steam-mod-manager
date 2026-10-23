"""Real WebEngine two-row cards, all type/deployment/source combinations."""
import json
from uuid import uuid4

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow
from tests.test_web_deploy import evaluate, wait_ui


def test_scheme_c_has_one_metadata_row_and_uniform_height(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DatabaseManager(tmp_path/'fixture.db')
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Card fixture','2026-01-01')")
    for i in range(12):
        db._conn.execute(
            "INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,platform,type_id,deploy_status,content_status,updated_at) VALUES(?,42,?,?,?,?,?,?,?,'2026-01-01')",
            (i+1,str(uuid4()),str(i+100),'A long title '*15,['steam','nexus','modio'][i//4],
             (4 if i in (3,7) else 3) if i%2 else None,'deployed' if i%4>=2 else 'not_deployed',['content_missing','healthy','unknown'][i%3]))
    db._conn.commit();db.close()
    (tmp_path/'mod_types.json').write_text(json.dumps({'games':{'42':{'types':[{'id':3,'name':'Long type '*20},{'id':4,'name':'美化'}]}}}),encoding='utf8')
    db=DatabaseManager.open_existing(tmp_path/'fixture.db')
    window=WebLibraryWindow(LibraryAdapter(db));window.resize(1728,900);window.show()
    badge_layout=r"""[...document.querySelectorAll('.card-meta')].every(meta=>{
        const labels=[...meta.children],rects=labels.map(e=>{const r=document.createRange();r.selectNodeContents(e);return r.getBoundingClientRect()});
        return labels.every(e=>e.getBoundingClientRect().height===20)&&Math.max(...rects.map(r=>r.y))-Math.min(...rects.map(r=>r.y))<.1;
    })&&[...document.querySelectorAll('.type-label')].filter(e=>e.textContent==='美化').every(e=>{
        const s=getComputedStyle(e),c=document.createElement('canvas').getContext('2d');c.font=s.font;
        const natural=c.measureText(e.textContent).width+parseFloat(s.paddingLeft)+parseFloat(s.paddingRight)+parseFloat(s.borderLeftWidth)+parseFloat(s.borderRightWidth);
        return Math.abs(e.getBoundingClientRect().width-natural)<.1;
    })"""
    try:
        wait_ui(window,"typeof S!=='undefined'&&S.assets.length===12")
        assert evaluate(window,"document.querySelectorAll('.card').length===12")
        assert evaluate(window,"new Set([...document.querySelectorAll('.card')].map(e=>e.getBoundingClientRect().height)).size===1")
        assert evaluate(window,"[...document.querySelectorAll('.card')].every(e=>e.querySelector('.card-body').children.length===2&&e.querySelectorAll('.card-meta').length===1)")
        assert evaluate(window,"[...document.querySelectorAll('.card')].every(e=>{const a=S.assets.find(a=>a.token===e.dataset.token);return e.querySelectorAll('.platform').length===1&&e.querySelectorAll('.type-label').length===(a.type_id==null?0:1)&&e.querySelectorAll('.deployed').length===(a.deployed?1:0)})")
        assert evaluate(window,"[...document.querySelectorAll('.card')].every(e=>e.querySelectorAll('.content-missing').length===(S.assets.find(a=>a.token===e.dataset.token).content_status==='content_missing'?1:0))")
        assert evaluate(window,"S.assets.filter(a=>a.platform==='Nexus').length===4&&!document.querySelector('.card').innerText.includes('Nexus Mods')")
        assert evaluate(window,"![...document.querySelectorAll('.card')].some(e=>/类型未知|未部署|待补齐内容|本地资产|Workspace ID/.test(e.innerText))")
        assert evaluate(window,"[...document.querySelectorAll('.card-meta')].every(e=>[...e.children].every(c=>c.getBoundingClientRect().top===e.getBoundingClientRect().top&&c.getBoundingClientRect().right<=e.getBoundingClientRect().right+1))")
        assert evaluate(window,"[...document.querySelectorAll('.card-title')].every(e=>getComputedStyle(e).whiteSpace==='nowrap'&&getComputedStyle(e).textOverflow==='ellipsis')")
        assert evaluate(window,badge_layout)
        window.resize(1180,760);evaluate(window,"$('compact').click()");QTest.qWait(80)
        assert evaluate(window,"[...document.querySelectorAll('.card-meta')].every(e=>[...e.children].every(c=>c.getBoundingClientRect().right<=e.getBoundingClientRect().right+1))")
        assert evaluate(window,badge_layout)
    finally:
        window.bridge.shutdown();window.close();app.processEvents();db.close()
