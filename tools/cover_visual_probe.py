"""Read-only Qt cover measurements and pixel checks, shared by release probes/tests."""
import base64
import hashlib
import json
import math
import time

from PySide6.QtGui import QImage
from PySide6.QtTest import QTest


def evaluate(window, script):
    result = []
    window.page.runJavaScript(script, result.append)
    for _ in range(300):
        if result:
            return result[0]
        QTest.qWait(10)
    raise TimeoutError(script[:150])


def wait_until(window, condition):
    for _ in range(500):
        if evaluate(window, 'Boolean(' + condition + ')'):
            return
        QTest.qWait(15)
        time.sleep(.002)
    raise TimeoutError(condition)


def wait_for_paint(window):
    """DOM completion is not a compositor fence, especially in background Qt windows."""
    window.raise_()
    window.activateWindow()
    evaluate(window, "globalThis.__coverPaintReady=false;requestAnimationFrame(()=>requestAnimationFrame(()=>__coverPaintReady=true))")
    wait_until(window, '__coverPaintReady')
    previous = None
    stable = 0
    bounds = None
    for _ in range(30):
        QTest.qWait(50)
        region = json.loads(evaluate(window, "JSON.stringify({rect:document.querySelector('.card.current .cover').getBoundingClientRect().toJSON(),dpr:devicePixelRatio})"))
        r, d = region['rect'], region['dpr']
        current_bounds = (math.floor(r['left'] * d), math.floor(r['top'] * d),
                          math.ceil(r['right'] * d), math.ceil(r['bottom'] * d))
        frame = window.grab().toImage()
        l, t, rr, b = current_bounds
        # Inspect the presented cover, not unrelated blinking carets/status areas.
        frame = frame.copy(l + 3, t + 3, rr - l - 6, b - t - 6)
        stable = stable + 1 if previous is not None and bounds == current_bounds and frame == previous else 0
        if stable >= 2:
            return
        previous = frame
        bounds = current_bounds
    raise TimeoutError('Qt painted frame did not stabilize; do not accept this capture')


MEASURE = r"""JSON.stringify((()=>{
 const card=document.querySelector('.card.current'),cover=card.querySelector('.cover');
 const img=cover.querySelector('img'),inspector=document.querySelector('#inspector-image img');
 const rect=e=>e.getBoundingClientRect().toJSON(),cr=rect(cover),dpr=devicePixelRatio;
 const result={uuid:S.current.internal_id,token:S.current.token,viewport:[innerWidth,innerHeight],dpr,
  card:rect(card),container:cr,inspectorRect:rect(document.querySelector('#inspector-image')),
  columns:S.columns,rowStep:S.cell,placeholder:!!cover.querySelector('.cover-placeholder'),
  masks:[...cover.querySelectorAll('button,input')].map(rect)};
 if(!img)return result;
 const ir=rect(img),css=getComputedStyle(img),border=parseFloat(css.borderBottomWidth)||0;
 result.image={rect:ir,natural:[img.naturalWidth,img.naturalHeight],fit:css.objectFit,
  position:css.objectPosition,minHeight:css.minHeight,transform:css.transform,borderBottom:border,
  gridRow:getComputedStyle(img.parentElement).gridTemplateRows,src:img.currentSrc};
 result.inspector=inspector?{rect:rect(inspector),natural:[inspector.naturalWidth,inspector.naturalHeight],
  fit:getComputedStyle(inspector).objectFit,position:getComputedStyle(inspector).objectPosition,
  transform:getComputedStyle(inspector).transform,borderBottom:parseFloat(getComputedStyle(inspector).borderBottomWidth)||0,
  container:rect(inspector.closest('.inspector-cover-wrap')||inspector.parentElement),
  src:inspector.currentSrc}:null;
 // Reference is the declared centered-cover policy in the CONTAINER, independent of IMG layout.
 const c=document.createElement('canvas'),x=Math.floor(cr.left*dpr),y=Math.floor(cr.top*dpr);
 c.width=Math.ceil(cr.right*dpr)-x;c.height=Math.ceil(cr.bottom*dpr)-y;
 const ctx=c.getContext('2d');ctx.fillStyle=getComputedStyle(cover).backgroundColor;
 ctx.fillRect(0,0,c.width,c.height);ctx.setTransform(dpr,0,0,dpr,cr.left*dpr-x,cr.top*dpr-y);
 ctx.imageSmoothingQuality='high';
 const s=Math.max(cr.width/img.naturalWidth,(cr.height-border)/img.naturalHeight);
 const w=img.naturalWidth*s,h=img.naturalHeight*s;
 ctx.drawImage(img,(cr.width-w)/2,(cr.height-border-h)/2,w,h);
 result.oracle=c.toDataURL('image/png');
 return result;
})())"""


def _decode(data_url):
    return base64.b64decode(data_url.split(',', 1)[1])


def source_window(image, container):
    """Normalized source rectangle actually exposed through the ancestor clip."""
    box = image['rect']
    nw, nh = image['natural']
    width, height = box['width'], box['height'] - image['borderBottom']
    scale = max(width / nw, height / nh)
    left = box['left'] + (width - nw * scale) / 2
    top = box['top'] + (height - nh * scale) / 2
    return [max(0, (max(container['left'], box['left']) - left) / scale / nw),
            max(0, (max(container['top'], box['top']) - top) / scale / nh),
            min(1, (min(container['right'], box['left'] + width) - left) / scale / nw),
            min(1, (min(container['bottom'], box['top'] + height) - top) / scale / nh)]


def pixel_difference(actual, expected, masks=(), margin=3):
    """Compare native Qt pixels; ignore edges and explicit overlaid controls only."""
    if actual.size() != expected.size():
        raise AssertionError('Cover reference and actual pixel dimensions differ')
    actual = actual.convertToFormat(QImage.Format_RGBA8888)
    expected = expected.convertToFormat(QImage.Format_RGBA8888)
    a, e = bytes(actual.constBits()), bytes(expected.constBits())
    width, height, stride = actual.width(), actual.height(), actual.bytesPerLine()
    diff = bytearray(width * height * 4)
    total = changed = count = 0
    blocks = {}
    for y in range(margin, height - margin):
        for x in range(margin, width - margin):
            if any(l <= x < rr and t <= y < b for l, t, rr, b in masks):
                continue
            pos = y * stride + x * 4
            errors = [abs(a[pos + k] - e[pos + k]) for k in range(3)]
            total += sum(errors)
            changed += max(errors) > 24
            count += 1
            block = blocks.setdefault((x // 8, y // 8), [0, 0, 0])
            for k in range(3):
                block[k] += a[pos + k] - e[pos + k]
            q = (y * width + x) * 4
            diff[q:q + 4] = bytes([min(255, v * 3) for v in errors] + [255])
    picture = QImage(bytes(diff), width, height, width * 4, QImage.Format_RGBA8888).copy()
    return {'mean_absolute_error_255': total / max(1, count * 3),
            'fraction_over_24': changed / max(1, count), 'compared_pixels': count,
            'block_mean_error_255': sum(abs(v) for b in blocks.values() for v in b) / max(1, count * 3)}, picture


def measure(window):
    wait_for_paint(window)
    data = json.loads(evaluate(window, MEASURE))
    frame = window.grab().toImage()
    c, dpr = data['container'], data['dpr']
    left, top = math.floor(c['left'] * dpr), math.floor(c['top'] * dpr)
    actual = frame.copy(left, top, math.ceil(c['right'] * dpr) - left,
                        math.ceil(c['bottom'] * dpr) - top)
    artifacts = {'actual': actual, 'full': frame}
    if 'image' in data:
        raw = _decode(data['image'].pop('src'))
        data['image']['resource_sha256'] = hashlib.sha256(raw).hexdigest()
        if data['inspector']:
            raw_inspector = _decode(data['inspector'].pop('src'))
            data['inspector']['resource_sha256'] = hashlib.sha256(raw_inspector).hexdigest()
            data['inspector']['visible_source_fraction'] = source_window(data['inspector'], data['inspector']['container'])
        data['visible_source_fraction'] = source_window(data['image'], c)
        expected = QImage.fromData(_decode(data.pop('oracle')))
        masks = [(math.floor(m['left'] * dpr) - left - 3, math.floor(m['top'] * dpr) - top - 3,
                  math.ceil(m['right'] * dpr) - left + 3, math.ceil(m['bottom'] * dpr) - top + 3)
                 for m in data['masks']]
        data['pixel_difference'], difference = pixel_difference(actual, expected, masks, math.ceil(3 * dpr))
        artifacts.update(expected=expected, difference=difference)
    return data, artifacts


def contract_failures(data, sample):
    if data['placeholder']:
        return [] if sample.get('key') == 'no-cover' else ['unexpected placeholder']
    image, container = data['image'], data['container']
    rect = image['rect']
    failures = []
    if any(abs(rect[k] - container[k]) > 1 for k in ['left', 'top', 'width', 'height']):
        failures.append('image box exceeds or misses its cover container')
    if (image['fit'], image['position'], image['transform']) != ('cover', '50% 50%', 'none'):
        failures.append('presentation policy differs from the declared centered-cover contract')
    if image['resource_sha256'] != sample['input_sha256']:
        failures.append('rendered resource differs from frozen input')
    if not data['inspector'] or image['resource_sha256'] != data['inspector']['resource_sha256']:
        failures.append('card and Inspector do not share the same preview')
    elif (data['inspector']['natural'] != image['natural'] or
          (data['inspector']['fit'], data['inspector']['position'], data['inspector']['transform']) !=
          ('cover', '50% 50%', 'none') or
          abs(data['inspector']['rect']['width'] - 92) > 1 or
          abs(data['inspector']['rect']['height'] - 72) > 1):
        failures.append('Inspector resource geometry or presentation policy changed')
    nw, nh = image['natural']
    ch = container['height'] - image['borderBottom']
    scale = max(container['width'] / nw, ch / nh)
    fw, fh = container['width'] / scale / nw, ch / scale / nh
    expected_source = [(1 - fw) / 2, (1 - fh) / 2, (1 + fw) / 2, (1 + fh) / 2]
    if any(abs(a - b) > .005 for a, b in zip(data['visible_source_fraction'], expected_source)):
        failures.append('actual visible source rectangle is not the declared centered cover')
    roi = sample.get('required_visible_source_fraction')
    visible = data['visible_source_fraction']
    if roi and not (visible[0] <= roi[0] and visible[1] <= roi[1] and
                    visible[2] >= roi[2] and visible[3] >= roi[3]):
        failures.append('required caption region is clipped')
    pixel = data['pixel_difference']
    # CSS image and Canvas use different subpixel/downsampling paths. Compare
    # 8x8 physical-pixel block colors for this mathematical oracle; retain raw
    # per-pixel errors/heatmaps for review instead of accepting aliased pixel noise.
    if pixel['block_mean_error_255'] > 6:
        failures.append('native Qt pixels differ from centered-cover reference')
    return failures


def compare_approved(data, actual, sample_key, manifest, fixture_root, difference_path=None):
    """No auto-promotion: a missing approved reference always remains pending."""
    refs = manifest['user_visual_review']['approved_references']
    match = next((r for r in refs if r['key'] == sample_key and r['viewport'] == data['viewport']
                  and abs(r['dpr'] - data['dpr']) < .01), None)
    if match is None:
        return {'status': 'USER VISUAL REVIEW PENDING'}
    path = fixture_root / match['file']
    if not match.get('approval_evidence') or hashlib.sha256(path.read_bytes()).hexdigest() != match['sha256']:
        raise AssertionError('Approved baseline missing approval evidence or hash mismatch')
    c, dpr = data['container'], data['dpr']
    left, top = math.floor(c['left'] * dpr), math.floor(c['top'] * dpr)
    masks = [(math.floor(m['left'] * dpr) - left - 3, math.floor(m['top'] * dpr) - top - 3,
              math.ceil(m['right'] * dpr) - left + 3, math.ceil(m['bottom'] * dpr) - top + 3)
             for m in data['masks']]
    metrics, difference = pixel_difference(actual, QImage(str(path)), masks, math.ceil(3 * dpr))
    if difference_path is not None and not difference.save(str(difference_path)):
        raise RuntimeError('Cannot encode approved-reference difference')
    return {'status': 'PASS' if metrics['block_mean_error_255'] <= 6 else 'FAIL', **metrics}


def _run_formal():
    """Canonical main.py composition, production reads only; artifacts stay under _tmp."""
    import argparse
    import sys
    from pathlib import Path
    from PySide6.QtCore import QTimer
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from services.business_resources import checked_sandbox_root
    from frontend.web_window import WebLibraryWindow, OfflinePage
    import main

    parser = argparse.ArgumentParser(description=__doc__)
    from tools.agent_workspace import require_workspace
    parser.add_argument('--output', type=require_workspace, required=True, help='Evidence directory under project _tmp')
    parser.add_argument('--viewport', default='1728x900')
    parser.add_argument('--expected-dpr', type=float)
    args = parser.parse_args()
    viewport = tuple(map(int, args.viewport.split('x')))
    if len(viewport) != 2 or min(viewport) < 500:
        parser.error('Expected a desktop viewport such as 1728x900')
    output = checked_sandbox_root(Path(args.output))
    output.mkdir(parents=True, exist_ok=True)
    fixtures = project / 'tests/fixtures/cover_visual'
    manifest = json.loads((fixtures / 'manifest.json').read_text(encoding='utf8'))
    samples = manifest['samples'] + [manifest['no_cover']]
    # Read-only runtime denies new Python file opens for writing. Open only this
    # explicit _tmp report before entering it; screenshots use Qt's encoder.
    stream = (output / 'results.json').open('x', encoding='utf8')
    report = {'mode': 'canonical main.py --read-only; real production assets',
              'viewport': viewport, 'cases': [], 'console': [], 'automatic_pass': False}
    original = WebLibraryWindow.__init__
    OfflinePage.javaScriptConsoleMessage = lambda self, level, message, line, source: report['console'].append(message)

    def init(window, *a, **kw):
        original(window, *a, **kw)
        window.setFixedSize(*viewport)
        QTimer.singleShot(500, lambda: collect(window))

    def collect(window):
        try:
            window.raise_()
            window.activateWindow()
            wait_until(window, "typeof S!=='undefined'&&S.snapshot")
            for sample in samples:
                iid = json.dumps(sample['internal_id'])
                evaluate(window, "$('game').value=" + json.dumps(str(sample['app_id'])) + ';load()')
                wait_until(window, "!$('refresh').disabled&&S.assets.some(a=>a.internal_id===" + iid + ')')
                evaluate(window, 'inspect(S.assets.find(a=>a.internal_id===' + iid + '))')
                wait_until(window, 'S.details&&S.current.internal_id===' + iid)
                evaluate(window, "$('viewport').scrollTop=Math.floor(S.rows.findIndex(a=>a.internal_id===S.current.internal_id)/S.columns)*S.cell;renderGrid()")
                wait_until(window, "S.coverPending.size===0&&!S.coverActive&&[...document.querySelectorAll('.card.current img,#inspector-image img')].every(i=>i.complete&&i.naturalWidth>0)")
                QTest.qWait(120)
                data, images = measure(window)
                data['sample'] = sample['key']
                data['failures'] = contract_failures(data, sample)
                if args.expected_dpr is not None and abs(data['dpr'] - args.expected_dpr) > .01:
                    data['failures'].append('actual Qt DPI differs from requested validation DPI')
                data['approved_baseline'] = compare_approved(data, images['actual'], sample['key'], manifest, fixtures,
                                                            output / (sample['key'] + '-approved-difference.png'))
                if data['approved_baseline']['status'] == 'FAIL':
                    data['failures'].append('approved visual reference differs')
                for name, image in images.items():
                    path = output / (sample['key'] + '-' + name + '.png')
                    if not image.save(str(path)):
                        raise RuntimeError('Cannot encode evidence screenshot')
                data['candidate_screenshot_sha256'] = hashlib.sha256((output / (sample['key'] + '-actual.png')).read_bytes()).hexdigest()
                if sample['key'] == 'square-caption':
                    evaluate(window, "$('viewport').scrollTop=$('viewport').scrollHeight;renderGrid()")
                    wait_until(window, "!document.querySelector('.card.current')&&S.coverPending.size===0&&!S.coverActive")
                    evaluate(window, "$('viewport').scrollTop=0;renderGrid()")
                    wait_until(window, "document.querySelector('.card.current img')?.complete&&document.querySelector('.card.current img').naturalWidth>0")
                    QTest.qWait(100)
                    returned, _ = measure(window)
                    data['warm_cache_return'] = {'resource_sha256':returned['image']['resource_sha256'],
                                                 'failures':contract_failures(returned, sample)}
                    data['failures'].extend('warm-cache return: ' + f for f in data['warm_cache_return']['failures'])
                report['cases'].append(data)
            report['automatic_pass'] = not report['console'] and all(not x['failures'] for x in report['cases'])
            report['visual_review'] = ('PASS' if all(x['approved_baseline']['status'] == 'PASS' for x in report['cases'])
                                       else 'USER VISUAL REVIEW PENDING')
        except Exception:
            import traceback
            report['error'] = traceback.format_exc()
        finally:
            window.close()

    WebLibraryWindow.__init__ = init
    main._notify_already_running = lambda: report.update(error='Existing SMM instance lock; close normally before retry')
    report['exit'] = main.main(['--read-only'])
    # A screenshot candidate is not an approved visual reference. Exit 2 keeps
    # this distinction actionable for release automation instead of implying PASS.
    report['result_exit'] = (1 if not report['automatic_pass'] else
                             0 if report.get('visual_review') == 'PASS' else 2)
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.close()
    print(json.dumps({k: v for k, v in report.items() if k not in ['cases']}, ensure_ascii=False))
    return report['result_exit']


if __name__ == '__main__':
    raise SystemExit(_run_formal())
