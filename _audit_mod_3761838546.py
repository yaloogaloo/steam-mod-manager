"""Read-only forensic dump for Mod 3761838546. Do not modify business logic."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from services.file_ops import ModFileManager
from services.sync import ModSyncService, SyncOptions

ROOT = Path(r"E:\project\steam-mod-manager")
FOLDER = (
    ROOT
    / "mod"
    / "Palworld"
    / "[Palworld 1.0] 4x Storage - Bigger Chests and Containers"
)
PUB_ID = "3761838546"


def fmt(p: Path) -> str:
    if not p.exists():
        return f"MISSING: {p}"
    st = p.stat()
    mtime = datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ", timespec="seconds")
    return f"{p} | size={st.st_size} | mtime={mtime}"


def main() -> None:
    info = FOLDER / ".info"
    legacy = FOLDER / "info"
    index = info / "index.html"
    meta_path = info / "mod.json"
    assets = info / "assets"

    print("=== LOCAL FS ===")
    print("FOLDER", FOLDER)
    print("EXISTS", FOLDER.is_dir())
    print("FOLDER_ISDIGIT", FOLDER.name.isdigit())
    print("INFO", fmt(info))
    print("LEGACY", fmt(legacy) if legacy.exists() else "no legacy info/")
    print("INDEX", fmt(index))
    print("META", fmt(meta_path))
    print("ASSETS_EXISTS", assets.exists())
    if assets.exists():
        files = sorted(assets.iterdir())
        print("ASSETS_COUNT", len(files))
        print("ASSETS_SAMPLE", [f.name for f in files[:12]])

    data = {}
    if meta_path.is_file():
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        print("=== MOD.JSON ===")
        for key in (
            "published_file_id",
            "title",
            "game_name",
            "app_id",
            "offline_page_path",
            "cover_path",
            "preview_url",
        ):
            print(key, data.get(key))

    if index.is_file():
        text = index.read_text(encoding="utf-8", errors="replace")
        print("=== INDEX.HTML FORENSICS ===")
        print("INDEX_IS_STUB", ("未能下载完整" in text) or ("Offline (stub)" in text))
        print("INDEX_HAS_BANNER", "smm-offline-banner" in text)
        print("INDEX_HAS_CURL35", ("curl: (35)" in text) or ("Connection was reset" in text))
        print("INDEX_HAS_HTTPSConnectionPool", "HTTPSConnectionPool" in text)
        print("INDEX_HAS_REQUESTS", "requests" in text.lower())
        for line in text.splitlines():
            if any(
                token in line
                for token in (
                    "原因",
                    "curl",
                    "Connection",
                    "Failed",
                    "HTTPSConnection",
                    "Recv failure",
                    "proxy",
                )
            ):
                print("LINE", line.strip()[:400])

    print("=== FILE MANAGER / SYNC HELPERS ===")
    mgr = ModFileManager(ROOT / "mod")
    svc = object.__new__(ModSyncService)
    svc.files = mgr
    print("metadata_path", mgr.metadata_path(FOLDER))
    print("info_dir", mgr.info_dir(FOLDER))
    print("info_dir_for_write", mgr.info_dir_for_write(FOLDER))
    print("has_valid_offline_page", svc._has_valid_offline_page(FOLDER))
    print("is_fully_synced_mod", svc._is_fully_synced_mod(FOLDER))

    loaded = mgr.load_metadata(FOLDER)
    print("loaded_title", loaded.title if loaded else None)
    print("loaded_offline_page_path", loaded.offline_page_path if loaded else None)

    print("=== SQLITE ===")
    db_path = ROOT / "data" / "mod_manager.db"
    print("DB", fmt(db_path) if db_path.exists() else "MISSING")
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT mod_id, app_id, title, preview_url, updated_at FROM mods WHERE mod_id=?",
            (PUB_ID,),
        ).fetchone()
        print("DB_ROW", row)
        conn.close()

    print("=== DELTA CLASSIFICATION UNDER DEFAULT OPTIONS ===")
    opts = SyncOptions(skip_existing=True, overwrite_files=False, archive_pages=True)
    print("overwrite_files", opts.overwrite_files)
    print("skip_existing", opts.skip_existing)
    print(
        "would_early_skip",
        bool(
            opts.skip_existing
            and not opts.overwrite_files
            and svc._is_fully_synced_mod(FOLDER)
        ),
    )
    print(
        "would_need_archive_in_network_enrich",
        not (not opts.overwrite_files and svc._has_valid_offline_page(FOLDER)),
    )


if __name__ == "__main__":
    main()
