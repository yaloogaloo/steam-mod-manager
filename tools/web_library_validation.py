"""Onscreen Windows read-only acceptance capture. No test/production mutation context.

Runs the real WebEngine window and real DAL with the shared instance lock.
Evidence goes only to an explicit output directory; production files are hashed.
No legacy MainWindow or business worker is constructed.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from core.paths import project_root, DATABASE_FILENAME, DATA_DIR_NAME
from services.app_instance import acquire_instance_lock, release_instance_lock
from services.mutation_context import suspend_mutation_context, reset_mutation_context
from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('--game', help='Explicit real game AppID; default is largest real library')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    database = project_root() / DATA_DIR_NAME / DATABASE_FILENAME
    if output.is_relative_to(database.parent):
        raise ValueError('Evidence must be outside production data')
    output.mkdir(parents=True, exist_ok=True)
    if not acquire_instance_lock():
        raise RuntimeError('Existing SMM instance owns the database; capture refused')
    files = [database, database.parent / 'mod_types.json']
    files.extend((project_root() / 'config').glob('*.json'))
    fingerprint = lambda: {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    before = fingerprint()
    def settings_fingerprint():
        import winreg
        values = []
        def read_key(path):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                    index = 0
                    while True:
                        try:
                            values.append((path, winreg.EnumValue(key, index)))
                            index += 1
                        except OSError:
                            break
                    index = 0
                    while True:
                        try:
                            child = winreg.EnumKey(key, index)
                            index += 1
                        except OSError:
                            break
                        read_key(path + '\\' + child)
            except FileNotFoundError:
                pass
        read_key(r'Software\SteamModManager\WorkshopLibrary')
        return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()
    settings_before = settings_fingerprint()
    from frontend.readonly_runtime import install_readonly_runtime
    install_readonly_runtime(database)
    token = suspend_mutation_context()
    app = QApplication([])
    db = DatabaseManager.open_existing(database)
    adapter = LibraryAdapter(db)
    counts = [(g.key, g.name, len(adapter.assets(int(g.key)))) for g in adapter.games()]
    game = next(g for g in counts if g[0] == args.game) if args.game else max(counts, key=lambda g: g[2])
    # Identify actual short and long descriptions, never generate example records.
    assets = adapter.assets(int(game[0]))
    # Hash exactly the local sources this visual probe reads, not the repository.
    from frontend.readonly_metadata import _sources
    source_files = set()
    for asset in assets:
        info = db.get_mod_display_info(asset.mod_pk)
        if info is None:
            continue
        root, _, _, backup_root, _ = _sources(db, asset, info)
        if root is not None:
            for folder in (root / '.info', root / 'info'):
                for name in ('metadata.json', 'mod.json', 'manifest.json'):
                    path = folder / name
                    if path.is_file():
                        source_files.add(path)
        if backup_root is not None:
            source_files.add(backup_root / 'metadata.json')
        cover = adapter.cover_file(asset)
        if cover:
            source_files.add(cover)
    files.extend(sorted(source_files))
    before = fingerprint()
    descriptions = []
    for asset in assets:
        try:
            detail = adapter.detail(asset)
            length = len(detail.description.strip())
            if length:
                descriptions.append((length, asset.internal_id))
        except (OSError, ValueError, LookupError):
            pass
    short = min(descriptions)[1] if descriptions else None
    long = max(descriptions)[1] if descriptions else None
    print(json.dumps({'games': counts, 'selected': game, 'description_lengths': [min(descriptions)[0], max(descriptions)[0]] if descriptions else [], 'fingerprinted_files': len(before), 'database_before': before[str(database)]}, ensure_ascii=False), flush=True)
    window = WebLibraryWindow(adapter)
    # Physical 1600x900 content at both 100% and 125%, native Windows rendering.
    ratio = app.primaryScreen().devicePixelRatio()
    window.resize(round(1600/ratio), round(900/ratio))
    window.show()
    window.raise_()
    started = time.perf_counter()
    report = {'dpi_ratio': ratio, 'game': game, 'captures': [], 'metrics': {}}
    phase = {'value': 0, 'busy': False, 'since': time.perf_counter()}

    def capture(name):
        path = output / (name + '.png')
        ok = window.grab().save(str(path))
        report['captures'].append({'name': name, 'path': str(path), 'saved': ok})

    def js(script, callback=lambda value: None):
        window.page.runJavaScript(script, callback)

    def tick():
        if phase['busy']:
            return
        phase['busy'] = True
        js("JSON.stringify({connected:typeof bridge!=='undefined'&&!!bridge,count:typeof S!=='undefined'?S.assets.length:0,game:document.getElementById('game').value,pending:typeof S!=='undefined'?S.coverActive:0,details:typeof S!=='undefined'&&!!S.details,dialog:document.getElementById('detail').open})", advance)

    def advance(raw):
        phase['busy'] = False
        if time.perf_counter()-started > 120 and phase['value'] < 6:
            report['failure'] = 'Acceptance capture timed out'
            print(json.dumps(report, ensure_ascii=False), flush=True)
            app.quit()
            return
        if not raw:
            return
        state = json.loads(raw)
        p = phase['value']
        if p == 0 and state['connected']:
            js("$('game').value="+json.dumps(game[0])+";load();")
            phase['value'] = 1
        elif p == 1 and state['game'] == game[0] and state['count'] == game[2] and state['pending'] == 0:
            capture('01-library')
            report['metrics']['load_seconds'] = round(time.perf_counter()-started, 3)
            js("JSON.stringify({cards:document.querySelectorAll('.card').length,total:S.rows.length,columns:S.columns,cell:S.cell,viewportHeight:$('viewport').clientHeight,bodyOverflow:document.body.scrollWidth>innerWidth})", lambda raw: report['metrics'].update(json.loads(raw)))
            js("inspect(S.assets.find(a=>a.internal_id==="+json.dumps(long)+")||S.assets[0]);$('viewport').scrollTop=Math.floor(S.rows.findIndex(a=>a.internal_id==="+json.dumps(long)+")/S.columns)*S.cell;renderGrid();")
            phase['value'] = 2
        elif p == 2 and state['details'] and state['pending'] == 0:
            capture('02-inspector')
            js('openDetail();')
            phase['value'] = 3
        elif p == 3 and state['dialog']:
            capture('04-long-description')
            js("$('detail').close();inspect(S.assets.find(a=>a.internal_id==="+json.dumps(short)+")||S.assets[0]);")
            phase['value'] = 4
        elif p == 4 and state['details']:
            js('openDetail();')
            phase['value'] = 5
        elif p == 5 and state['dialog']:
            capture('03-short-description')
            js("$('detail').close();closeInspector();$('viewport').scrollTop=5000;renderGrid();JSON.stringify({cards:document.querySelectorAll('.card').length,scroll:$('viewport').scrollTop})", lambda raw: report['metrics'].update({'scrolled': json.loads(raw)}))
            phase['value'] = 6
            phase['since'] = time.perf_counter()
        elif p == 6 and time.perf_counter()-phase['since'] > 2:
            capture('05-scroll')
            js("const t=performance.now();$('search').value=S.assets[0].name;filter();JSON.stringify({milliseconds:performance.now()-t,results:S.rows.length})", lambda raw: report['metrics'].update({'filter': json.loads(raw)}))
            phase['value'] = 7
            phase['since'] = time.perf_counter()
        elif p == 7 and time.perf_counter()-phase['since'] > 1:
            after = fingerprint()
            report['fingerprinted_files'] = len(before)
            report['production_hashes_unchanged'] = before == after
            report['changed_paths'] = [p for p in before.keys() | after.keys() if before.get(p) != after.get(p)]
            report['database_after'] = after[str(database)]
            report['production_settings_unchanged'] = settings_before == settings_fingerprint()
            print(json.dumps(report, ensure_ascii=False), flush=True)
            phase['value'] = 8
            if not args.hold:
                app.quit()
            else:
                js("$('search').value='';filter();")
                timer.stop()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(400)
    try:
        return app.exec()
    finally:
        assert window.bridge.shutdown(), 'Readers still active'
        db.close()
        reset_mutation_context(token)
        release_instance_lock()


if __name__ == '__main__':
    raise SystemExit(main())
