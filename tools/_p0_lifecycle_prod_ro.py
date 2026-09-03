"""Read-only production lifecycle impact. No writes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.paths import database_path, default_mod_library
from services.identity_repair import open_readonly_sqlite
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

REMAINING_DUP_IDS = (
    "9000000000000394",
    "9000000000003381",
    "9000000000003091",
    "9000000000003222",
    "9000000000003204",
    "9000000000003331",
    "9000000000003456",
    "10782",
    "9000000000003054",
)


def main() -> int:
    db = open_readonly_sqlite(database_path())
    conn = db._conn  # noqa: SLF001
    try:
        print("=== 10782 / 3456 ===")
        for mid in ("10782", "9000000000003456"):
            row = conn.execute(
                "SELECT * FROM mods WHERE CAST(mod_id AS TEXT)=?", (mid,)
            ).fetchone()
            if row is None:
                print(mid, "ABSENT")
                continue
            keep = [
                "mod_id",
                "platform",
                "app_id",
                "workspace_id",
                "external_id",
                "source_url",
                "last_known_path",
                "title",
                "display_name",
                "updated_at",
                "folder_present",
                "internal_id",
            ]
            print({k: row[k] for k in keep if k in row.keys()})
        print("=== audit 3456 / 3054 / 10782 ===")
        for mid in ("9000000000003456", "9000000000003054", "10782"):
            logs = conn.execute(
                "SELECT created_at, field_name, old_value, new_value, source, reason "
                "FROM identity_audit_log WHERE CAST(mod_id AS TEXT)=? ORDER BY id DESC LIMIT 8",
                (mid,),
            ).fetchall()
            print(mid, [dict(x) for x in logs])
        print("=== 3054 sidecar ===")
        row = conn.execute(
            "SELECT last_known_path, source_url FROM mods WHERE CAST(mod_id AS TEXT)='9000000000003054'"
        ).fetchone()
        folder = Path(row["last_known_path"]) if row else None
        print("db_source_url", row["source_url"] if row else None)
        if folder and folder.is_dir():
            meta = folder / INFO_DIR_NAME / METADATA_FILENAME
            off = folder / INFO_DIR_NAME / "offline" / "metadata.json"
            if meta.is_file():
                data = json.loads(meta.read_text(encoding="utf-8"))
                print(
                    "sidecar",
                    {
                        k: data.get(k)
                        for k in (
                            "url",
                            "source_url",
                            "workspace_id",
                            "published_file_id",
                            "platform",
                            "source_type",
                            "internal_id",
                        )
                    },
                )
            if off.is_file():
                odata = json.loads(off.read_text(encoding="utf-8"))
                print("offline_original_url", odata.get("original_url"), "source_file", odata.get("source_file"))
        print("=== 3456 sidecar published_file_id ===")
        row2 = conn.execute(
            "SELECT last_known_path FROM mods WHERE CAST(mod_id AS TEXT)='9000000000003456'"
        ).fetchone()
        if row2:
            p = Path(row2["last_known_path"])
            meta = p / INFO_DIR_NAME / METADATA_FILENAME
            print("folder_exists", p.is_dir(), p)
            if meta.is_file():
                data = json.loads(meta.read_text(encoding="utf-8"))
                print(
                    "sidecar",
                    {
                        k: data.get(k)
                        for k in (
                            "url",
                            "workspace_id",
                            "published_file_id",
                            "platform",
                            "source_type",
                            "internal_id",
                            "external_id",
                        )
                    },
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
