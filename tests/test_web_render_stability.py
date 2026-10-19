"""Real Qt node/image continuity across virtual ranges, selection and density."""
from PySide6.QtTest import QTest
from frontend.web_window import WebLibraryWindow
from tools.cover_visual_probe import evaluate, wait_until
from tests.test_web_library import bridge


def test_surviving_cards_keep_decoded_images_focus_and_visual_order(bridge):
    window=WebLibraryWindow(bridge.adapter);window.resize(1728,900);window.show()
    js=lambda code:evaluate(window,code)
    wait=lambda code:wait_until(window,code)
    try:
        wait("typeof S!=='undefined'&&S.snapshot&&S.assets.length===1")
        js("""globalThis.fixture=S.assets[0];
          S.assets=Array.from({length:200},(_,i)=>({...fixture,token:'render-'+i,name:'Asset '+i,search_fields:['Asset '+i]}));
          for(const a of S.assets)S.covers.set(a.token,'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7');
          filter();""")
        wait("[...document.querySelectorAll('.cover img')].every(i=>i.complete&&i.naturalWidth===1)")
        js("""globalThis.originalCards=new Map([...document.querySelectorAll('.card')].map(c=>[c.dataset.token,{card:c,img:c.querySelector('img'),rect:c.getBoundingClientRect().toJSON()}]));
          globalThis.keptToken=S.rows[S.columns*2].token;
          globalThis.keptCard=originalCards.get(keptToken).card;
          keptCard.focus({preventScroll:true});
          $('viewport').scrollTop=S.cell+2;renderGrid();""")
        assert js("keptCard.isConnected&&document.activeElement===keptCard")
        assert js("[...document.querySelectorAll('.card')].every(c=>!originalCards.has(c.dataset.token)||(originalCards.get(c.dataset.token).card===c&&originalCards.get(c.dataset.token).img===c.querySelector('img')))")
        assert js("Math.abs(keptCard.getBoundingClientRect().width-originalCards.get(keptToken).rect.width)<.01")
        js("toggleSelection(keptToken);renderGrid()")
        assert js("keptCard===document.querySelector('[data-token=\"'+keptToken+'\"]')&&keptCard.querySelector('.check').checked&&keptCard.classList.contains('checked')")
        # Reverse scrolling retains overlap and restores visual keyboard tab order.
        js("$('viewport').scrollTop=0;renderGrid()")
        assert js("[...$('grid').children].every((c,i)=>c.dataset.token===S.rows[i].token)")
        assert js("keptCard.isConnected&&keptCard.querySelector('img')===originalCards.get(keptToken).img")
        js("""globalThis.productRender=renderGrid;globalThis.renderCount=0;
          renderGrid=function(...args){renderCount++;return productRender(...args);};
          $('compact').click();""")
        assert js("renderCount===1")
        assert js("keptCard.isConnected&&keptCard.querySelector('img')===originalCards.get(keptToken).img")
        js("""$('viewport').scrollTop=S.cell*5+26;renderGrid();
          globalThis.savedAnchor=browseAnchor();renderCount=0;$('standard').click();""")
        assert js("renderCount===1&&Math.abs($('viewport').scrollTop-(Math.floor(S.rows.findIndex(a=>a.token===savedAnchor.token)/S.columns)+savedAnchor.offset)*S.cell)<1")
        js("renderCount=0;closeInspector()")
        assert js("renderCount===1")
        js("""globalThis.movedCard=$('grid').lastElementChild;movedCard.focus({preventScroll:true});
          const [first,last]=S.renderRange;S.rows.splice(first,last-first,...S.rows.slice(first,last).reverse());renderGrid();""")
        assert js("movedCard.isConnected&&document.activeElement===movedCard")
        # A filter and game snapshot must not leave foreign rows or duplicate ids.
        js("$('search').value='Asset 19';filter()")
        assert js("[...$('grid').children].every(c=>S.rows.some(a=>a.token===c.dataset.token))")
        assert js("new Set([...$('grid').children].map(c=>c.dataset.token)).size===$('grid').children.length")
        js("$('search').value='';S.assets=[{...fixture,token:'new-snapshot'}];filter()")
        assert js("$('grid').children.length===1&&$('grid').firstElementChild.dataset.token==='new-snapshot'")
    finally:
        window.bridge.shutdown();window.close();QTest.qWait(20)
