#!/usr/bin/env python3
"""Delete two approved P0 identity-repair backup trees. Does not touch production DB."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
DB = ROOT / "data" / "mod_manager.db"
REPAIR = ROOT / "data" / "identity_repair_production_backup"
LIFE = ROOT / "data" / "identity_lifecycle_production_backup" / "20260903T053206Z"
PARENT = ROOT / "data" / "identity_lifecycle_production_backup"
FLASH = (
    ROOT
    / "mod"
    / "巫师三"
    / "Flashbacks - Something you've already seen"
    / ".info"
    / "metadata.json"
)


def stat_file(p: Path) -> dict:
    if not p.exists():
        return {"path": str(p), "exists": False}
    st = p.stat()
    return {
        "path": str(p),
        "exists": True,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }


def main() -> int:
    sidecar_3054 = ROOT / "mod" / "博德之门Ⅲ" / "更好的角色装备展示界面" / ".info" / "metadata.json"
    before = {
        "db": stat_file(DB),
        "flash_sidecar": stat_file(FLASH),
        "sidecar_3054": stat_file(sidecar_3054),
    }
    print("BEFORE", json.dumps(before, ensure_ascii=False, indent=2))
    if not REPAIR.is_dir() or not LIFE.is_dir():
        print("STOP missing target", REPAIR.exists(), LIFE.exists())
        return 2
    life_ok = (LIFE / "mod_manager.db").is_file() and (LIFE / "apply_report.json").is_file()
    if not life_ok:
        print("STOP lifecycle artifacts mismatch")
        return 2
    # Resume-safe: first attempt already verified BACKUP_MANIFEST; tree may be partial.
    print("DELETING", REPAIR)
    shutil.rmtree(f"\\\\?\\{REPAIR.resolve()}", ignore_errors=False)
    print("DELETING", LIFE)
    shutil.rmtree(f"\\\\?\\{LIFE.resolve()}", ignore_errors=False)
    print("repair_absent", not REPAIR.exists())
    print("life_absent", not LIFE.exists())
    print(
        "parent_exists",
        PARENT.is_dir(),
        "parent_children",
        [p.name for p in PARENT.iterdir()] if PARENT.is_dir() else None,
    )
    after = {
        "db": stat_file(DB),
        "flash_sidecar": stat_file(FLASH),
        "sidecar_3054": stat_file(sidecar_3054),
    }
    print("AFTER", json.dumps(after, ensure_ascii=False, indent=2))
    print("DB_SIZE_UNCHANGED", before["db"].get("size") == after["db"].get("size"))
    print("DB_MTIME_UNCHANGED", before["db"].get("mtime_ns") == after["db"].get("mtime_ns"))
    print("FLASH_UNCHANGED", before["flash_sidecar"] == after["flash_sidecar"])
    print("S3054_UNCHANGED", before["sidecar_3054"] == after["sidecar_3054"])
    if before["db"].get("mtime_ns") != after["db"].get("mtime_ns"):
        return 3
    if REPAIR.exists() or LIFE.exists():
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
