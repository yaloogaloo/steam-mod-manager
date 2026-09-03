#!/usr/bin/env python3
"""Post-hygiene data/ usage report. Read-only."""

from __future__ import annotations

from pathlib import Path

DATA = Path(r"D:\project\steam-mod-manager\data")


def main() -> None:
    files = 0
    dirs = 0
    total = 0
    top: list[tuple[int, str]] = []
    for child in sorted(DATA.iterdir(), key=lambda p: p.name.lower()):
        if child.is_file():
            files += 1
            sz = child.stat().st_size
            total += sz
            top.append((sz, child.name))
            continue
        if not child.is_dir():
            continue
        dirs += 1
        cfiles = 0
        cdirs = 0
        csize = 0
        for p in child.rglob("*"):
            if p.is_dir():
                cdirs += 1
                dirs += 1
            elif p.is_file():
                cfiles += 1
                files += 1
                csize += p.stat().st_size
        total += csize
        top.append((csize, f"{child.name}/ ({cfiles} files, {cdirs} dirs)"))
        print(f"DIR {child.name} size={csize} files={cfiles} dirs={cdirs}")
    print("TOTAL_SIZE", total)
    print("FILE_COUNT", files)
    print("DIR_COUNT", dirs)
    print("TOP")
    for sz, name in sorted(top, reverse=True)[:12]:
        print(f"  {sz:15}  {name}")
    checks = {
        "identity_repair_production_backup": (DATA / "identity_repair_production_backup").exists(),
        "lifecycle_20260903T053206Z": (
            DATA / "identity_lifecycle_production_backup" / "20260903T053206Z"
        ).exists(),
        "lifecycle_parent": (DATA / "identity_lifecycle_production_backup").is_dir(),
        "mod_manager.db": (DATA / "mod_manager.db").is_file(),
        "asset_cache": (DATA / "asset_cache").exists(),
        "mod_backup": (DATA / "mod_backup").exists(),
        "browser_profile": (DATA / "browser_profile").exists(),
        "identity_repair_quarantine": (DATA / "identity_repair_quarantine").exists(),
        "import_cache": (DATA / "import_cache").exists(),
    }
    print("CHECKS", checks)


if __name__ == "__main__":
    main()
