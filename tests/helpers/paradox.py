"""Explicit Launcher membership for valid temporary projection fixtures."""
import sqlite3
from contextlib import closing


def register_descriptor(user_dir, workshop_id):
    """Model an already registered Mod in the fixture's active Playset."""
    with closing(sqlite3.connect(user_dir / 'launcher-v2.sqlite')) as con, con:
        playset = con.execute('SELECT id FROM playsets WHERE isActive=1').fetchone()
        if not playset:
            return
        mod_id = f'mod-{workshop_id}'
        con.execute(
            "INSERT OR IGNORE INTO mods(id,steamId,gameRegistryId,displayName,status,source) "
            "VALUES (?,?,?,?,'ready_to_play','steam')",
            (mod_id, workshop_id, f'mod/ugc_{workshop_id}.mod', workshop_id),
        )
        con.execute(
            'INSERT INTO playsets_mods(playsetId,modId,enabled,position) VALUES (?,?,0,0)',
            (playset[0], mod_id),
        )
