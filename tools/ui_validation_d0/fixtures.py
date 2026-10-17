"""Synthetic contract fixture. AST reads constants, never imports business code."""
from __future__ import annotations

import ast
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import uuid
import zlib

from .safety import Boundary, IsolationError

SCENARIOS = (
    "empty", "small", "large", "long_name", "no_cover", "cover_ratios",
    "long_description", "bbcode", "states", "normal", "sorting", "collections",
    "loading", "error", "delayed",
)


def constants(source: Path) -> dict:
    values = {}
    for node in ast.parse(source.read_text(encoding="utf-8-sig")).body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target in {"_SCHEMA", "_GAMES_MIGRATIONS", "_MODS_MIGRATIONS"}:
            values[target] = ast.literal_eval(node.value)
    if len(values) != 3:
        raise IsolationError("Current schema constants not recognized")
    return values


def synthetic_png(width: int, height: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = (b"\x00" + b"\x40\x80\xc0" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def build(boundary: Boundary, count: int = 20) -> dict:
    if not 1 <= count <= 10000:
        raise IsolationError("Count must be 1..10000")
    root = boundary.root
    dbpath = boundary.target(root / "data" / "fixture.db")
    if dbpath.exists():
        raise IsolationError("Fixture DB exists; refusing replacement")
    for name in ("data", "settings", "cache", "temp", "assets", "mods", "games", "backup", "logs", "captures"):
        boundary.target(root / name).mkdir(parents=True, exist_ok=True)
    for name, dimensions in {"square": (128, 128), "wide": (256, 96), "portrait": (96, 256)}.items():
        path = root / "assets" / (name + ".png")
        path.write_bytes(synthetic_png(*dimensions))
    schema = constants(boundary.repository / "core" / "db_manager.py")
    identities = []
    with closing(sqlite3.connect(dbpath)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(schema["_SCHEMA"])
        # Populate a NEW synthetic DB from current column declarations. Never
        # call DatabaseManager._init_schema or a production migration/service.
        for table, key in (("games", "_GAMES_MIGRATIONS"), ("mods", "_MODS_MIGRATIONS")):
            columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
            for name, declaration in schema[key]:
                if name not in columns:
                    db.execute('ALTER TABLE ' + table + ' ADD COLUMN "' + name + '" ' + declaration)
                    columns.add(name)
        for aid, name in ((910001, "Synthetic Empty"), (910002, "Synthetic Mods")):
            game = root / "games" / str(aid)
            game.mkdir()
            db.execute("INSERT INTO games(app_id,name,install_path,mod_path,workshop_path,updated_at) VALUES(?,?,?,?,?,?)", (aid, name, str(game), str(game / "deploy"), str(game / "workshop"), "2026-01-01T00:00:00+00:00"))
        for i in range(count):
            frozen = str(uuid.uuid5(uuid.NAMESPACE_URL, "smm-ui-d0/synthetic/" + str(i)))
            workspace = str(9800000000 + i)
            managed = root / "mods" / "Synthetic Mods" / frozen
            # Missing-content fixture must actually lack its managed directory.
            # Other rows have synthetic directories, not copied Mod payloads.
            if i != 3:
                managed.mkdir(parents=True)
            title = ("Synthetic long name " * 30) if i == 0 else f"Synthetic Mod {i}"
            description = ("Synthetic description\n" * 1500) if i == 1 else ("[b]Synthetic BBCode[/b]\n[list][*]item[/list]" if i == 2 else "Synthetic description")
            cover = "" if i % 4 == 0 else str(root / "assets" / (["square", "wide", "portrait"][i % 3] + ".png"))
            deployed = "deployed" if i % 3 == 0 else "not_deployed"
            absent = i == 3
            cursor = db.execute("INSERT INTO mods(app_id,title,description,display_name,internal_id,workspace_id,external_id,platform,source_type,cover_path,deploy_status,folder_present,last_known_path,library_status,content_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (910002, title, description, title, frozen, workspace, workspace, "steam", "steam", cover, deployed, int(not absent), str(managed), "missing" if absent else "normal", "content_missing" if absent else "healthy", "2026-01-01T00:00:00+00:00"))
            identities.append({"internal_id": frozen, "workspace_id": workspace, "mod_pk": cursor.lastrowid, "managed_path": str(managed)})
        collection = db.execute("INSERT INTO collections(app_id,name,created_at,updated_at) VALUES(?,?,?,?)", (910002, "Synthetic Collection", "2026-01-01", "2026-01-01")).lastrowid
        for entity in identities[:5]:
            db.execute("INSERT INTO collection_mods(collection_id,mod_id,created_at) VALUES(?,?,?)", (collection, entity["mod_pk"], "2026-01-01"))
        db.commit()
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise IsolationError("Synthetic fixture FK contract failed")
    result = {"root": str(root), "db": str(dbpath), "count": count, "identities": identities, "collection_id": collection, "scenarios": list(SCENARIOS), "scenario_rendering": "NOT CLOSED: fixture data only; UI admission refused", "schema_source_sha256": hashlib.sha256((boundary.repository / "core" / "db_manager.py").read_bytes()).hexdigest()}
    (root / "fixture_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
