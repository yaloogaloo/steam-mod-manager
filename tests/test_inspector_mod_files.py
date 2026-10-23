"""Inspector file view reuses the existing mod_files commands and deploy chain.

Isolated database and archive only. Screenshots go to ``_tmp/agent_runs``.
"""
import json
import time
import zipfile
from pathlib import Path

import pytest

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.game_info import GameInfo
from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_UNKNOWN,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_NEXUS,
    ModFileEntry,
    ModFilesBundle,
)
from frontend.library_adapter import LibraryAdapter
from frontend.web_window import WebLibraryWindow
from services.business_resources import BusinessResources
from tests.helpers.identity import create_steam_test_mod, create_test_mod_identity, prove_managed_folder


APP_ID = 424242
OTHER_APP = 424243
HD_NAME = 'Optional-HD-texture-pack-with-a-very-long-archive-name-that-must-ellipsis.zip'
EVIDENCE = Path(__file__).resolve().parents[1] / '_tmp' / 'agent_runs' / 'inspector-mod-files'


def _zip(path: Path, name: str, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(name, payload.encode('utf-8'))


def _entry(filename: str, *, role: str, source: str, selected: bool, category: str = '', description: str = '') -> ModFileEntry:
    meta = {'category': category} if category else {}
    if description:
        meta['description'] = description
    return ModFileEntry(
        id=filename,
        filename=filename,
        path=filename,
        file_role=role,
        source_type=source,
        type=FILE_TYPE_MAIN if selected and role in (FILE_ROLE_NEXUS_MAIN, FILE_ROLE_GITHUB_RELEASE_ASSET) else FILE_TYPE_OPTIONAL,
        selected_for_deploy=selected,
        enabled=selected,
        metadata=meta,
    )


def _seed(root: Path) -> tuple[BusinessResources, dict[str, str], Path]:
    (root / 'data').mkdir(parents=True)
    (root / 'config').mkdir()
    library = root / 'mod' / 'FileGame'
    library.mkdir(parents=True)
    game = root / 'games' / 'FileGame'
    mods = game / 'mods'
    mods.mkdir(parents=True)
    capabilities = Path(__file__).resolve().parents[1] / 'config' / 'game_capabilities.json'
    (root / 'config' / 'game_capabilities.json').write_bytes(capabilities.read_bytes())
    db = DatabaseManager(root / 'data' / 'mod_manager.db')
    db.upsert_game(GameInfo(app_id=APP_ID, name='File Game', folder_name='FileGame'))
    db.upsert_game(GameInfo(app_id=OTHER_APP, name='Other Game', folder_name='Other'))
    db.update_game_deploy_config(
        APP_ID, name='File Game', install_path=str(game), mod_path=str(mods), deploy_type='folder_copy',
    )
    handles: dict[str, str] = {}

    steam = library / 'SteamLoose'
    steam.mkdir()
    (steam / 'payload.txt').write_text('STEAM-PAYLOAD', encoding='utf-8')
    (steam / 'extra.json').write_text('{"k":1}', encoding='utf-8')
    (steam / 'module.pak').write_bytes(b'PAK')
    created = create_steam_test_mod(db, external_id='42424201', title='SteamLoose', app_id=APP_ID, game_name='File Game')
    prove_managed_folder(db, steam, handle=created.mod_id, title='SteamLoose', app_id=APP_ID, game_name='File Game')
    db.update_mod_user_metadata(created.mod_id, {'custom_description': 'STEAM-DESC-TOKEN'})
    handles['steam'] = str(created.mod_id)

    nexus = library / 'NexusPack'
    nexus.mkdir()
    _zip(nexus / 'Main-1.zip', 'main-1.txt', 'MAIN-1')
    _zip(nexus / 'Main-2.zip', 'main-2.txt', 'MAIN-2')
    _zip(nexus / 'Optional-Voice.zip', 'voice.txt', 'VOICE')
    _zip(nexus / HD_NAME, 'hd.txt', 'HD')
    for index in range(12):
        _zip(nexus / f'Extra-{index:02d}.zip', f'extra-{index:02d}.txt', f'EXTRA-{index}')
    created = create_test_mod_identity(
        db, platform=PLATFORM_NEXUS, title='NexusPack', external_id='42424202', app_id=APP_ID,
        game_name='File Game', source_url='https://www.nexusmods.com/filegame/mods/42424202',
    )
    prove_managed_folder(
        db, nexus, handle=created.mod_id, title='NexusPack', app_id=APP_ID,
        game_name='File Game', platform=PLATFORM_NEXUS,
    )
    files = [
        _entry('Main-1.zip', role=FILE_ROLE_NEXUS_MAIN, source=SOURCE_TYPE_NEXUS, selected=True, category='Main', description='主体文件，包含核心功能与基础资源。'),
        _entry('Main-2.zip', role=FILE_ROLE_NEXUS_MAIN, source=SOURCE_TYPE_NEXUS, selected=True, category='Main'),
        _entry('Optional-Voice.zip', role=FILE_ROLE_NEXUS_OPTIONAL, source=SOURCE_TYPE_NEXUS, selected=True, category='Optional'),
        _entry(HD_NAME, role=FILE_ROLE_NEXUS_OPTIONAL, source=SOURCE_TYPE_NEXUS, selected=False, category='Optional'),
    ]
    files.extend(
        _entry(
            f'Extra-{index:02d}.zip', role=FILE_ROLE_NEXUS_OPTIONAL, source=SOURCE_TYPE_NEXUS, selected=False, category='Optional',
            description='这是一段很长的文件说明，用来确认说明不会撑破 Inspector，悬停可以查看完整内容。' if index == 0 else '',
        )
        for index in range(12)
    )
    db.set_mod_files(created.mod_id, ModFilesBundle(files=files))
    db.update_mod_user_metadata(created.mod_id, {'custom_description': 'NEXUS-DESC-TOKEN <b>kept</b>'})
    handles['nexus'] = str(created.mod_id)

    github = library / 'GithubPack'
    github.mkdir()
    _zip(github / 'mod-release.zip', 'release.txt', 'RELEASE')
    _zip(github / 'mod-release-extra.zip', 'extra-release.txt', 'EXTRA')
    _zip(github / 'mod-source.zip', 'source.txt', 'SOURCE')
    created = create_test_mod_identity(
        db, platform=PLATFORM_GITHUB, title='GithubPack', external_id='owner/file-mod', app_id=APP_ID,
        game_name='File Game', source_url='https://github.com/owner/file-mod',
    )
    prove_managed_folder(
        db, github, handle=created.mod_id, title='GithubPack', app_id=APP_ID,
        game_name='File Game', platform=PLATFORM_GITHUB,
    )
    db.set_mod_files(created.mod_id, ModFilesBundle(files=[
        _entry('mod-release.zip', role=FILE_ROLE_GITHUB_RELEASE_ASSET, source=SOURCE_TYPE_GITHUB, selected=True),
        _entry('mod-release-extra.zip', role=FILE_ROLE_GITHUB_RELEASE_ASSET, source=SOURCE_TYPE_GITHUB, selected=False),
        _entry('mod-source.zip', role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE, source=SOURCE_TYPE_GITHUB, selected=False),
    ]))
    db.update_mod_user_metadata(created.mod_id, {'custom_description': 'GITHUB-DESC-TOKEN'})
    handles['github'] = str(created.mod_id)
    return BusinessResources(root, db), handles, mods


def _evaluate(window, script):
    result = []
    window.page.runJavaScript(script, result.append)
    for _ in range(150):
        if result:
            return result[0]
        QTest.qWait(20)
    raise AssertionError('JavaScript callback timed out')


def _wait(window, condition: str, rounds: int = 400) -> None:
    for _ in range(rounds):
        if _evaluate(window, condition):
            return
        # qWait can keep the GIL and stall the deploy QThread.
        time.sleep(0.01)
        QTest.qWait(20)
    state = _evaluate(window, "JSON.stringify({busy:S.busy,desc:($('inspector-description')||{}).textContent,status:($('inspector-files-status')||{}).textContent,results:$('results').textContent,pane:S.inspectorPane,name:S.current?.name,epoch:S.detailEpoch})")
    raise AssertionError(condition + ' :: ' + str(state))


def _open(window, name: str, rounds: int = 800) -> None:
    _evaluate(window, f"inspect(S.assets.find(a=>a.name==={json.dumps(name)}))")
    _wait(window, f"!!S.details && S.current?.name==={json.dumps(name)}", rounds)


def _shot(window, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    window.view.grab().save(str(EVIDENCE / f'{name}-window.png'))
    rect = json.loads(_evaluate(window, "JSON.stringify($('inspector').getBoundingClientRect())"))
    pixmap = window.view.grab()
    ratio = window.devicePixelRatioF()
    cropped = pixmap.copy(QRect(
        round(rect['x'] * ratio), round(rect['y'] * ratio),
        max(1, round(rect['width'] * ratio)), max(1, round(rect['height'] * ratio)),
    ))
    cropped.save(str(EVIDENCE / f'{name}.png'))
    assert cropped.width() > 80 and cropped.height() > 80


def _row_script(name: str, script: str) -> str:
    return (
        "(()=>{const row=[...document.querySelectorAll('.inspector-file-row')]"
        f".find(r=>r.dataset.fileName==={json.dumps(name)});{script}}})()"
    )


def _set_type(window, name: str, value: str) -> None:
    _evaluate(window, _row_script(name, "row.querySelector('.inspector-file-type').click();"))
    _wait(window, "$('file-type-menu').matches(':popover-open')")
    _evaluate(window, "document.querySelector('#file-type-menu [data-value=' + JSON.stringify(" + json.dumps(value) + ") + ']').click()")
    _wait(window, "!S.busy && !$('file-type-menu').matches(':popover-open') && !($('inspector-files-status') && $('inspector-files-status').textContent)")


def _toggle_check(window, name: str) -> None:
    _evaluate(window, _row_script(name, "row.querySelector('[data-file-select]').click();"))
    _wait(window, '!S.busy && !($(\'inspector-files-status\') && $(\'inspector-files-status\').textContent)')


REFERENCE = Path(r'C:\Users\king\.cursor\projects\d-project-steam-mod-manager\assets\c__Users_king_AppData_Roaming_Cursor_User_workspaceStorage_e901a7773adc5408939349d9f1c6a96f_images_a_wide_dark_themed_ui_mockup_screenshot_of_a_mod-cb64b59b-7218-4ec5-8a43-2934f8d267ae.jpg')


def _compare_reference() -> None:
    shot = QImage(str(EVIDENCE / 'nexus-files-window.png'))
    reference = QImage(str(REFERENCE))
    if shot.isNull() or reference.isNull():
        raise AssertionError('missing screenshot or approved reference')
    height = max(shot.height(), reference.height())
    canvas = QImage(shot.width() + reference.width() + 24, height, QImage.Format.Format_RGB32)
    canvas.fill(QColor('#101418'))
    painter = QPainter(canvas)
    painter.drawImage(0, 0, reference)
    painter.drawImage(reference.width() + 24, 0, shot)
    painter.end()
    canvas.save(str(EVIDENCE / 'reference-comparison.png'))


def _bundle(resources, handle: str) -> dict:
    return {entry.filename: entry for entry in resources.db.get_mod_files(handle).files}


def _deploy(window, name: str) -> None:
    _open(window, name)
    assert _evaluate(window, "!document.querySelector('[data-command=deploy]').disabled"), _evaluate(
        window, "document.querySelector('[data-command=deploy]')?.title || 'missing'",
    )
    _evaluate(window, "document.querySelector('[data-command=deploy]').click()")
    _wait(window, "!S.busy && $('results').textContent==='操作成功'", rounds=800)


@pytest.mark.timeout(240)
def test_inspector_file_management_persists_and_deploy_uses_selection(tmp_path):
    resources, handles, mods = _seed(tmp_path / 'business')
    app = QApplication.instance() or QApplication([])
    window = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
    window.resize(1440, 860)
    window.show()
    try:
        _wait(window, 'typeof S!=="undefined" && S.assets.length===3', rounds=800)
        _open(window, 'SteamLoose')
        assert _evaluate(window, "!$('inspector-file-toggle') && !$('inspector-files')")
        assert _evaluate(window, "$('inspector-description').textContent.includes('STEAM-DESC-TOKEN')")
        assert _evaluate(window, "document.querySelector('#inspector .facts').nextElementSibling.textContent==='描述摘要'")
        assert 'module.pak' not in _evaluate(window, "$('inspector').innerText")
        _shot(window, 'steam-single')
        _deploy(window, 'SteamLoose')
        steam_dest = mods / 'SteamLoose'
        assert (steam_dest / 'payload.txt').read_text(encoding='utf-8') == 'STEAM-PAYLOAD'
        assert (steam_dest / 'extra.json').is_file() and (steam_dest / 'module.pak').is_file()

        _open(window, 'NexusPack')
        assert _evaluate(window, "!!$('inspector-file-toggle') && $('inspector-file-toggle').getAttribute('aria-pressed')==='false'")
        assert _evaluate(window, "$('inspector-description').textContent.includes('NEXUS-DESC-TOKEN') && !$('inspector-description').innerHTML.includes('data-file-select')")
        assert not _evaluate(window, "inspectorContent(S.current,S.details,'deployment-',true).includes('inspector-file-toggle')")
        width = _evaluate(window, "$('inspector').getBoundingClientRect().width")
        footer = _evaluate(window, "JSON.stringify(document.querySelector('.inspector-footer').getBoundingClientRect())")
        _shot(window, 'nexus-description')
        _evaluate(window, "$('inspector-file-toggle').click()")
        _wait(window, "document.querySelector('#inspector-section-title .inspector-section-label').textContent==='文件管理' && document.querySelector('#inspector-section-title .inspector-file-count').textContent==='(16)' && !$('inspector-files').hidden")
        assert abs(_evaluate(window, "$('inspector').getBoundingClientRect().width") - width) < 1
        assert _evaluate(window, "JSON.stringify(document.querySelector('.inspector-footer').getBoundingClientRect())") == footer
        assert _evaluate(window, "$('inspector-description').hidden && $('inspector-description').textContent.includes('NEXUS-DESC-TOKEN') && !$('inspector-description').innerHTML.includes('data-file-select')")
        assert _evaluate(window, _row_script('Main-1.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Main' && row.querySelector('[data-file-select]').checked;"))
        assert _evaluate(window, _row_script('Main-2.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Main' && row.querySelector('[data-file-select]').checked;"))
        assert _evaluate(window, _row_script('Optional-Voice.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Opt' && row.querySelector('[data-file-select]').checked;"))
        assert _evaluate(window, _row_script(HD_NAME, "return row.querySelector('.inspector-file-type-label').textContent==='Opt' && !row.querySelector('[data-file-select]').checked && row.querySelector('.inspector-file-name').scrollWidth>row.querySelector('.inspector-file-name').clientWidth && row.querySelector('.inspector-file-name').title===row.querySelector('.inspector-file-name').textContent;"))
        assert _evaluate(window, _row_script('Main-1.zip', "const item=row.closest('.inspector-file');const type=row.querySelector('.inspector-file-type');const paint=getComputedStyle(type);return item.querySelector('.inspector-file-note').textContent==='主体文件，包含核心功能与基础资源。' && item.querySelector('.inspector-file-note').title===item.querySelector('.inspector-file-note').textContent && type.dataset.tone==='main' && paint.color==='rgb(143, 191, 164)' && paint.borderTopWidth==='0px' && paint.backgroundColor==='rgba(0, 0, 0, 0)' && type.querySelector('.inspector-file-caret') && row.querySelector('[data-file-note]').textContent.trim()==='';"))
        assert _evaluate(window, _row_script('Main-2.zip', "return !row.closest('.inspector-file').querySelector('.inspector-file-note') && row.querySelector('.inspector-file-type').dataset.tone==='main';"))
        assert _evaluate(window, _row_script('Optional-Voice.zip', "return row.querySelector('.inspector-file-type').dataset.tone==='opt';"))
        assert _evaluate(window, "['main','opt','misc','other','locale','release','src','development','old'].join()===[['Main','main'],['Opt','opt'],['Misc','misc'],['Other','other'],['汉化','locale'],['Release','release'],['Src','src'],['Development','development'],['Old','old']].map(([label,tone])=>fileTypeTone(label)===tone?tone:label).join()")
        _shot(window, 'nexus-files')
        before = _evaluate(window, _row_script('Extra-01.zip', "const rowBox=row.getBoundingClientRect();const type=row.querySelector('.inspector-file-type');type.click();return JSON.stringify({height:rowBox.height,width:$('inspector').getBoundingClientRect().width,top:type.getBoundingClientRect().top});"))
        _wait(window, "$('file-type-menu').matches(':popover-open')")
        menu = json.loads(_evaluate(window, "JSON.stringify({bg:getComputedStyle($('file-type-menu')).backgroundColor,labels:[...$('file-type-menu').querySelectorAll('[role=option]')].map(o=>o.textContent),selected:$('file-type-menu').querySelector('[aria-selected=true]').textContent,dots:[...$('file-type-menu').querySelectorAll('.file-type-dot')].map(d=>d.dataset.tone+':'+getComputedStyle(d).backgroundColor),check:getComputedStyle($('file-type-menu').querySelector('[aria-selected=true] svg')).color,insideScroll:!!$('file-type-menu').closest('.inspector-scroll'),rect:$('file-type-menu').getBoundingClientRect()})"))
        assert menu['bg'] == 'rgb(36, 48, 51)'
        assert menu['labels'] == ['Main', 'Opt', 'Misc', '汉化', 'Other']
        assert menu['selected'].startswith('Opt')
        assert menu['dots'] == [
            'main:rgb(94, 207, 154)', 'opt:rgb(176, 122, 223)', 'misc:rgb(90, 164, 232)',
            'locale:rgb(224, 106, 168)', 'other:rgb(226, 177, 90)',
        ]
        assert menu['check'] == 'rgb(112, 201, 178)'
        assert menu['insideScroll'] is False
        assert 0 <= menu['rect']['top'] and menu['rect']['bottom'] <= _evaluate(window, 'innerHeight')
        after = json.loads(before)
        assert _evaluate(window, _row_script('Extra-01.zip', "return row.getBoundingClientRect().height")) == after['height']
        assert _evaluate(window, "$('inspector').getBoundingClientRect().width") == after['width']
        _shot(window, 'nexus-type-menu')
        _evaluate(window, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        _wait(window, "!$('file-type-menu').matches(':popover-open')")
        _evaluate(window, _row_script('Extra-01.zip', "persistInspectorFile('file-category',{file_id:row.dataset.fileId,value:'Nope'});"))
        _wait(window, "!S.busy && ($('inspector-files-status')||{}).textContent")
        assert _evaluate(window, _row_script('Extra-01.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Opt' && !row.querySelector('[data-file-select]').checked;"))
        assert (_bundle(resources, handles['nexus'])['Extra-01.zip'].metadata or {}).get('category') == 'Optional'

        _evaluate(window, _row_script('Main-2.zip', "row.querySelector('[data-file-note]').click();"))
        _wait(window, "!!document.querySelector('#inspector-files textarea')")
        _evaluate(window, "document.querySelector('#inspector-files textarea').value='UNSAVED-NOTE'")
        _evaluate(window, _row_script('Main-2.zip', "row.querySelector('.inspector-file-name').click();"))
        assert _evaluate(window, "document.querySelector('#inspector-files textarea').value==='UNSAVED-NOTE'")
        assert not (_bundle(resources, handles['nexus'])['Main-2.zip'].metadata or {}).get('description')
        _shot(window, 'nexus-note-editor')
        _evaluate(window, "document.querySelector('[data-file-note-cancel]').click()")
        _wait(window, "!document.querySelector('#inspector-files textarea')")
        assert not (_bundle(resources, handles['nexus'])['Main-2.zip'].metadata or {}).get('description')
        assert _evaluate(window, _row_script('Main-2.zip', "return !row.closest('.inspector-file').querySelector('.inspector-file-note');"))
        long_note = '这是一段很长的文件说明，用来确认说明不会撑破 Inspector，悬停可以查看完整内容。'
        assert _evaluate(window, _row_script('Extra-00.zip', "const note=row.closest('.inspector-file').querySelector('.inspector-file-note');return note.textContent===" + json.dumps(long_note) + " && note.title===note.textContent && note.scrollWidth>note.clientWidth;"))
        _evaluate(window, _row_script('Optional-Voice.zip', "row.querySelector('[data-file-note]').click();"))
        _wait(window, "!!document.querySelector('#inspector-files textarea')")
        _evaluate(window, "document.querySelector('#inspector-files textarea').value='语音可选包，可单独决定是否部署。'")
        _evaluate(window, "document.querySelector('[data-file-note-save]').click()")
        _wait(window, "!S.busy && !document.querySelector('#inspector-files textarea') && !!document.querySelector('.inspector-file-note')")
        voice = _bundle(resources, handles['nexus'])['Optional-Voice.zip']
        assert voice.file_role == FILE_ROLE_NEXUS_OPTIONAL and voice.selected_for_deploy is True
        assert (voice.metadata or {}).get('description') == '语音可选包，可单独决定是否部署。'
        assert (_bundle(resources, handles['nexus'])['Main-1.zip'].metadata or {}).get('description') == '主体文件，包含核心功能与基础资源。'
        assert _evaluate(window, _row_script('Optional-Voice.zip', "const note=row.closest('.inspector-file').querySelector('.inspector-file-note');return note.textContent==='语音可选包，可单独决定是否部署。' && note.title===note.textContent;"))
        assert _evaluate(window, _row_script('Main-2.zip', "return !row.closest('.inspector-file').querySelector('.inspector-file-note');"))

        # The type setter rewrites selection; the menu restores the previous checkbox.
        _set_type(window, 'Main-1.zip', 'Optional')
        changed = _bundle(resources, handles['nexus'])
        assert changed['Main-1.zip'].file_role == FILE_ROLE_NEXUS_OPTIONAL
        assert changed['Main-1.zip'].selected_for_deploy is True
        assert changed['Main-2.zip'].file_role == FILE_ROLE_NEXUS_MAIN and changed['Main-2.zip'].selected_for_deploy is True
        _set_type(window, 'Main-1.zip', 'Main')
        restored = _bundle(resources, handles['nexus'])
        assert restored['Main-1.zip'].file_role == FILE_ROLE_NEXUS_MAIN
        assert restored['Main-1.zip'].selected_for_deploy is True
        assert (restored['Main-1.zip'].metadata or {}).get('description') == '主体文件，包含核心功能与基础资源。'
        _toggle_check(window, 'Main-2.zip')
        _toggle_check(window, 'Optional-Voice.zip')
        _set_type(window, HD_NAME, 'Other')
        edited = _bundle(resources, handles['nexus'])
        assert edited['Main-2.zip'].file_role == FILE_ROLE_NEXUS_MAIN and edited['Main-2.zip'].selected_for_deploy is False
        assert edited['Optional-Voice.zip'].file_role == FILE_ROLE_NEXUS_OPTIONAL and edited['Optional-Voice.zip'].selected_for_deploy is False
        assert edited[HD_NAME].file_role == FILE_ROLE_UNKNOWN and edited[HD_NAME].selected_for_deploy is False
        assert (edited[HD_NAME].metadata or {}).get('category') == 'Other'
        assert _evaluate(window, "document.querySelector('.inspector-scroll').scrollHeight>document.querySelector('.inspector-scroll').clientHeight")
        _evaluate(window, "document.querySelector('.inspector-scroll').scrollTop=document.querySelector('.inspector-scroll').scrollHeight")
        QTest.qWait(50)
        _shot(window, 'nexus-files-scrolled')
        _evaluate(window, _row_script(HD_NAME, "row.querySelector('.inspector-file-type').click();"))
        _wait(window, "$('file-type-menu').matches(':popover-open')")
        placed = json.loads(_evaluate(window, _row_script(HD_NAME, "const menu=$('file-type-menu').getBoundingClientRect();const trigger=row.querySelector('.inspector-file-type').getBoundingClientRect();return JSON.stringify({up:menu.bottom<=trigger.top+1,top:menu.top,bottom:menu.bottom,height:innerHeight,selected:$('file-type-menu').querySelector('[aria-selected=true]').textContent});")))
        assert placed['up'] and placed['top'] >= 0 and placed['bottom'] <= placed['height']
        assert placed['selected'].startswith('Other')
        _shot(window, 'nexus-type-menu-up')
        _evaluate(window, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        _wait(window, "!$('file-type-menu').matches(':popover-open')")
        assert _evaluate(window, "JSON.stringify(document.querySelector('.inspector-footer').getBoundingClientRect())") == footer
        _evaluate(window, "$('inspector-file-toggle').click()")
        _wait(window, "$('inspector-section-title').textContent==='描述摘要' && $('inspector-files').hidden && !$('inspector-description').hidden")
        assert _evaluate(window, "$('inspector-description').textContent.includes('NEXUS-DESC-TOKEN')")

        _evaluate(window, f"$('game').value={json.dumps(str(OTHER_APP))};$('game').onchange()")
        _wait(window, f"$('game').value==={json.dumps(str(OTHER_APP))} && S.assets.length===0")
        _evaluate(window, f"$('game').value={json.dumps(str(APP_ID))};$('game').onchange()")
        _wait(window, "S.assets.length===3")
        _open(window, 'NexusPack')
        _evaluate(window, "$('inspector-file-toggle').click()")
        _wait(window, "!$('inspector-files').hidden")
        assert _evaluate(window, _row_script('Main-1.zip', "return row.querySelector('[data-file-select]').checked && row.querySelector('.inspector-file-type').dataset.value==='Main' && row.closest('.inspector-file').querySelector('.inspector-file-note').textContent==='主体文件，包含核心功能与基础资源。';"))
        assert _evaluate(window, _row_script('Main-2.zip', "return !row.querySelector('[data-file-select]').checked && row.querySelector('.inspector-file-type').dataset.value==='Main' && !row.closest('.inspector-file').querySelector('.inspector-file-note');"))
        assert _evaluate(window, _row_script('Optional-Voice.zip', "return !row.querySelector('[data-file-select]').checked && row.closest('.inspector-file').querySelector('.inspector-file-note').textContent==='语音可选包，可单独决定是否部署。';"))
        assert _evaluate(window, _row_script(HD_NAME, "return !row.querySelector('[data-file-select]').checked && row.querySelector('.inspector-file-type').dataset.value==='Other';"))
        _deploy(window, 'NexusPack')
        nexus_dest = mods / 'NexusPack'
        assert (nexus_dest / 'main-1.txt').read_text(encoding='utf-8') == 'MAIN-1'
        assert not (nexus_dest / 'main-2.txt').exists()
        assert not (nexus_dest / 'voice.txt').exists()
        assert not (nexus_dest / 'hd.txt').exists()
        assert not (nexus_dest / 'Main-2.zip').exists()
        assert not (nexus_dest / HD_NAME).exists()

        _open(window, 'GithubPack')
        _evaluate(window, "$('inspector-file-toggle').click()")
        _wait(window, "!$('inspector-files').hidden")
        assert _evaluate(window, _row_script('mod-release.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Release' && row.querySelector('[data-file-select]').checked;"))
        assert _evaluate(window, _row_script('mod-release-extra.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Release' && !row.querySelector('[data-file-select]').checked;"))
        assert _evaluate(window, _row_script('mod-source.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Src' && !row.querySelector('[data-file-select]').checked;"))
        _shot(window, 'github-files')
        _toggle_check(window, 'mod-release-extra.zip')
        _toggle_check(window, 'mod-source.zip')
        github = _bundle(resources, handles['github'])
        assert github['mod-release.zip'].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET and github['mod-release.zip'].selected_for_deploy is True
        assert github['mod-release-extra.zip'].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET and github['mod-release-extra.zip'].selected_for_deploy is True
        assert github['mod-source.zip'].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE and github['mod-source.zip'].selected_for_deploy is True
        _deploy(window, 'GithubPack')
        github_dest = mods / 'GithubPack'
        assert (github_dest / 'release.txt').read_text(encoding='utf-8') == 'RELEASE'
        assert (github_dest / 'extra-release.txt').read_text(encoding='utf-8') == 'EXTRA'
        assert not (github_dest / 'source.txt').exists()
        assert _bundle(resources, handles['github'])['mod-source.zip'].selected_for_deploy is True

        assert window.bridge.shutdown()
        window.close()
        app.processEvents()
        again = WebLibraryWindow(LibraryAdapter(resources.db, writable=True), resources=resources)
        again.resize(1180, 640)
        again.show()
        try:
            _wait(again, 'typeof S!=="undefined" && S.assets.length===3')
            _open(again, 'NexusPack')
            _evaluate(again, "$('inspector-file-toggle').click()")
            _wait(again, "!$('inspector-files').hidden")
            assert _evaluate(again, _row_script('Main-2.zip', "return !row.querySelector('[data-file-select]').checked && row.querySelector('.inspector-file-type').dataset.value==='Main' && !row.closest('.inspector-file').querySelector('.inspector-file-note');"))
            assert _evaluate(again, _row_script('Main-1.zip', "return row.closest('.inspector-file').querySelector('.inspector-file-note').textContent==='主体文件，包含核心功能与基础资源。';"))
            assert _evaluate(again, _row_script('Optional-Voice.zip', "return row.closest('.inspector-file').querySelector('.inspector-file-note').textContent==='语音可选包，可单独决定是否部署。';"))
            assert _evaluate(again, _row_script(HD_NAME, "return row.querySelector('.inspector-file-type').dataset.value==='Other' && row.querySelector('.inspector-file-type').dataset.tone==='other';"))
            _open(again, 'GithubPack')
            _evaluate(again, "$('inspector-file-toggle').click()")
            _wait(again, "!$('inspector-files').hidden")
            assert _evaluate(again, _row_script('mod-source.zip', "return row.querySelector('.inspector-file-type-label').textContent==='Src' && row.querySelector('[data-file-select]').checked;"))
            _shot(again, 'github-restored')
        finally:
            assert again.bridge.shutdown()
            again.close()
            app.processEvents()
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / 'result.json').write_text(json.dumps({
            'dpr': window.devicePixelRatioF(),
            'nexus_selected': ['Main-1.zip'],
            'github_selected_roles': {
                'mod-release.zip': 'github_release_asset',
                'mod-release-extra.zip': 'github_release_asset',
                'mod-source.zip': 'github_source_archive',
            },
            'source_checkbox_persisted': True,
            'source_excluded_by_existing_deploy_rule': True,
            'type_change_keeps_selection': True,
            'file_note_persisted': True,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        _compare_reference()
    finally:
        if window.bridge:
            window.bridge.shutdown()
        window.close()
        app.processEvents()
        resources.db.close()
