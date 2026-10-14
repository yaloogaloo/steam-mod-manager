"""Readonly presentation contracts; all filesystem inputs are synthetic."""
import json
from types import SimpleNamespace
from uuid import uuid4
from pathlib import Path
from frontend.library_adapter import Asset
from frontend.readonly_metadata import resolve_detail, resolve_cover
from frontend.description_view import render_description, SafeDescriptionBrowser
from frontend.asset_grid import AssetGrid
from frontend.asset_model import AssetModel, AssetDelegate
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


def fixture(tmp_path):
    uid = str(uuid4())
    root = tmp_path / 'mod'
    (root / '.info').mkdir(parents=True)
    info = SimpleNamespace(app_id=42, custom_description='custom', steam_description='steam')
    db = SimpleNamespace(db_path=tmp_path / 'test.db', get_mod_display_info=lambda pk: info,
                         get_mod_backup_row=lambda pk: {})
    asset = Asset(uid, 'external', 17, str(root), 'Synthetic', 'Steam', False, False, '', '', '')
    return db, asset, info, root


def test_description_priority_and_foreign_identity(tmp_path):
    db, asset, info, root = fixture(tmp_path)
    path = root / '.info/metadata.json'
    path.write_text(json.dumps({'internal_id': asset.internal_id, 'description': 'sidecar'}))
    assert resolve_detail(db, asset, info).description == 'sidecar'
    path.write_text(json.dumps({'internal_id': str(uuid4()), 'description': 'foreign-long'}))
    assert resolve_detail(db, asset, info).description == 'custom'


def test_backup_and_missing_folder_custom_description(tmp_path):
    db, asset, info, root = fixture(tmp_path)
    backup = tmp_path / 'mod_backup' / asset.internal_id
    backup.mkdir(parents=True)
    (backup / 'metadata.json').write_text(json.dumps({'description': 'backup'}))
    assert resolve_detail(db, asset, info).description == 'backup'
    (backup / 'metadata.json').write_text(json.dumps({'custom_description': 'backup custom'}))
    from dataclasses import replace
    missing = replace(asset, managed_path=str(tmp_path / 'absent'))
    assert resolve_detail(db, missing, info).description == 'backup custom'


def test_cover_read_does_not_materialize(tmp_path):
    db, asset, info, root = fixture(tmp_path)
    cover = root / '.info/cover.png'
    cover.write_bytes(b'synthetic')
    before = sorted(str(p) for p in tmp_path.rglob('*'))
    assert resolve_cover(db, asset) == cover.resolve()
    assert sorted(str(p) for p in tmp_path.rglob('*')) == before


def test_richtext_no_resources_or_active_content():
    raw = '<script>secret()</script><img src="file:///private"><b onclick="x">Bold</b>[i]Italic[/i]'
    html = render_description(raw)
    assert 'secret' not in html and 'src=' not in html and 'onclick' not in html
    assert '<b>Bold</b>' in html and '<i>Italic</i>' in html
    app = QApplication.instance() or QApplication([])
    view = SafeDescriptionBrowser()
    view.set_description(raw)
    assert 'Bold' in view.toPlainText()
    from PySide6.QtCore import QUrl
    assert view.loadResource(2, QUrl('file:///private')) is None
    view.close()


def test_grid_reflows_and_late_result_is_ignored():
    app = QApplication.instance() or QApplication([])
    view = AssetGrid()
    model = AssetModel(SimpleNamespace(cover_file=lambda asset: None), view)
    view.setModel(model)
    view.setItemDelegate(AssetDelegate(view))
    view.setViewMode(view.ViewMode.IconMode)
    view.resize(905, 600)
    view.show()
    QTest.qWait(30)
    view.reflow()
    assert 0 <= view.viewport().width() - view.columns * view.gridSize().width() < view.columns
    generation = model.generation
    model.replace([])
    model._ready(generation, 'old', object())
    assert not model.images
    view.close()
    assert model.shutdown()

