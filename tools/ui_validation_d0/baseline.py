"""Read-only host fingerprints. Never opens SQLite or instantiates QSettings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime


def snapshot(repository: Path, relative_paths: list[str]) -> dict:
    import winreg
    files = {}
    for relative in relative_paths:
        path = repository / relative
        if not path.resolve().is_relative_to(repository.resolve()):
            raise ValueError("Baseline path outside repository")
        record = {"exists": path.exists()}
        if path.is_file():
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            after = path.stat()
            record.update(size=after.st_size, mtime_ns=after.st_mtime_ns,
                          sha256=digest.hexdigest(),
                          changed_during_read=(before.st_size, before.st_mtime_ns) !=
                          (after.st_size, after.st_mtime_ns))
        files[relative] = record
    entries = []

    def walk(key, prefix):
        for index in range(winreg.QueryInfoKey(key)[1]):
            name, value, kind = winreg.EnumValue(key, index)
            entries.append((prefix, name, kind, repr(value)))
        for index in range(winreg.QueryInfoKey(key)[0]):
            name = winreg.EnumKey(key, index)
            with winreg.OpenKey(key, name, 0, winreg.KEY_READ) as child:
                walk(child, prefix + "/" + name)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\SteamModManager\WorkshopLibrary",
                            0, winreg.KEY_READ) as key:
            walk(key, "")
        settings_exists = True
    except FileNotFoundError:
        settings_exists = False
    # Export digest only, never registry values/credentials.
    settings = {"exists": settings_exists, "entry_count": len(entries),
                "sha256": hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=False).encode()).hexdigest()}
    raw = subprocess.check_output([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Depth 3"
    ], encoding="utf-8")
    from .safety import suspects
    processes = json.loads(raw)
    active = suspects(processes if isinstance(processes, list) else [processes], repository)
    return {"time": datetime.now().astimezone().isoformat(), "files": files,
            "qsettings": settings, "active_production_processes": active,
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
            "git": subprocess.check_output(["git", "status", "--short"], cwd=repository, text=True),
            "scope": "raw file hashes and KEY_READ registry fingerprint; no database connection; active writers may invalidate stability"}


def compare(before: dict, after: dict) -> dict:
    changed = [path for path in set(before["files"]) | set(after["files"])
               if before["files"].get(path) != after["files"].get(path)]
    unstable = [path for state in (before, after) for path, item in state["files"].items()
                if item.get("changed_during_read")]
    settings_changed = before["qsettings"] != after["qsettings"]
    return {"changed_files": sorted(changed), "unstable_reads": sorted(set(unstable)),
            "qsettings_changed": settings_changed,
            "status": "NOT CLOSED" if changed or unstable or settings_changed else "PASS",
            "scope": "these samples only; no integrity or writer attribution claim"}
