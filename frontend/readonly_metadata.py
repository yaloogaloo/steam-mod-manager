"""Read-only projection of legacy metadata precedence, with an explicit DAL.

Never uses the legacy resolver's identity normalization, global DB or materializer.
"""
import json
from pathlib import Path
from services.metadata_cache import load_metadata
from services.metadata_owner_guard import metadata_payload_is_foreign
from services.backup_identity import backup_read_root


def _owned(payload, asset, app_id):
    proof = str(payload.get('internal_id') or payload.get('entity_key') or '').strip()
    return not (proof and proof not in (asset.internal_id, str(asset.mod_pk))) and not metadata_payload_is_foreign(payload, entity_app_id=app_id)


def _sources(db, asset, info):
    root = Path(asset.managed_path) if asset.managed_path else None
    if root is not None and str(root).startswith(('\\\\', '//')):
        root = None
    exists = root is not None and root.is_dir()
    sidecar = dict(load_metadata(root) or {}) if exists else {}
    if not _owned(sidecar, asset, info.app_id):
        sidecar = {}
        root = None  # A foreign folder is not a source of cover bytes either.
    backup_root = backup_read_root(asset.internal_id, mod_pk=asset.mod_pk,
                                  db=db, base=Path(db.db_path).parent / 'mod_backup')
    backup = {}
    if backup_root is not None:
        try:
            parsed = json.loads((backup_root / 'metadata.json').read_text('utf-8'))
            if isinstance(parsed, dict) and _owned(parsed, asset, info.app_id):
                backup = parsed
            else:
                backup_root = None
        except (OSError, ValueError):
            backup_root = None
    return root, exists, sidecar, backup_root, backup


class DetailProjection:
    def __init__(self, info, description, source):
        self._info = info
        self.description = description
        self.description_source = source

    def __getattr__(self, key):
        return getattr(self._info, key)


def resolve_detail(db, asset, info):
    _, exists, sidecar, _, backup = _sources(db, asset, info)
    choices = [('sidecar.description', sidecar.get('description')),
               ('sidecar.custom_description', sidecar.get('custom_description')),
               ('backup.description', backup.get('description'))]
    if not exists:
        choices.append(('backup.custom_description', backup.get('custom_description')))
    choices += [('db.custom_description', info.custom_description),
                ('db.steam_description', info.steam_description)]
    for source, value in choices:
        if str(value or '').strip():
            return DetailProjection(info, str(value).strip(), source)
    return DetailProjection(info, '', 'empty')


def _file(ref, root=None):
    if not ref:
        return None
    path = Path(ref)
    if not path.is_absolute():
        if root is None:
            return None
        path = root / path
    # UNC paths can cause implicit network authentication; readonly preview is local.
    if str(path).startswith(('\\\\', '//')):
        return None
    return path.resolve() if path.is_file() and path.stat().st_size else None


def resolve_cover(db, asset):
    info = db.get_mod_display_info(asset.mod_pk)
    if info is None:
        return None
    root, exists, sidecar, backup_root, _ = _sources(db, asset, info)
    candidates = []
    # Canonical live/backup projection remains the first candidate, as on ModCard.
    if root is not None or not exists:
        candidates.append(_file(asset.cover, root))
    if root is not None and exists:
        for pattern in ('cover.*', 'preview.*'):
            candidates.extend(_file(p) for p in sorted((root / '.info').glob(pattern)))
        candidates.append(_file(sidecar.get('cover_path'), root))
    if backup_root is not None:
        candidates.extend(_file(p) for p in sorted(backup_root.glob('cover.*')))
    row = db.get_mod_backup_row(asset.mod_pk) or {}
    candidates.append(_file(row.get('backup_cover_path')))
    return next((p for p in candidates if p is not None), None)
