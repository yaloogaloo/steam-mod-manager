"""Best-effort Windows diagnostics: which process holds a path lock.

Used only for logging after rename WinError 5 — does not change rename behavior.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Common lock holders we care about in reports.
_KNOWN_EXTERNAL = {
    "explorer.exe",
    "msmpeng.exe",
    "msmpengcp.exe",
    "searchindexer.exe",
    "searchprotocolhost.exe",
    "msedge.exe",
    "chrome.exe",
    "firefox.exe",
    "code.exe",
    "cursor.exe",
}


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve()).lower()
    except OSError:
        return str(path).lower().replace("/", "\\")


def _classify_process(name: str, pid: int) -> str:
    """Return ``internal`` / ``external`` / ``unknown``."""
    low = (name or "").strip().lower()
    if pid == os.getpid():
        return "internal"
    if low in {"python.exe", "pythonw.exe"}:
        # Other Python processes are still "this stack" from the user's view,
        # but not *this* PID — treat as internal-ish sibling.
        return "internal"
    if low in _KNOWN_EXTERNAL:
        return "external"
    if low.endswith(".exe"):
        return "external"
    return "unknown"


def _via_psutil(path: str | Path) -> list[dict[str, Any]]:
    """
    Lightweight psutil probe: cwd + known lock-holder process names only.

    Full ``open_files()`` scans across all processes are too slow / often
    empty without elevation on Windows, so they are skipped here.
    """
    try:
        import psutil
    except ImportError:
        return []

    needle = _norm(path)
    interesting = {n.lower() for n in _KNOWN_EXTERNAL} | {
        "python.exe",
        "pythonw.exe",
    }
    hits: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            info = proc.info
            pid = int(info.get("pid") or 0)
            name = str(info.get("name") or "")
            low = name.lower()
            if low not in interesting and pid != os.getpid():
                continue
            reasons: list[str] = []
            try:
                cwd = proc.cwd()
                if cwd and _norm(cwd).startswith(needle):
                    reasons.append(f"cwd={cwd}")
            except (psutil.Error, OSError):
                pass
            # Only open_files for our own PID (fast, no privilege issues).
            if pid == os.getpid():
                try:
                    for of in proc.open_files() or []:
                        opath = str(getattr(of, "path", "") or "")
                        if opath and _norm(opath).startswith(needle):
                            reasons.append(f"open_file={opath}")
                except (psutil.Error, OSError):
                    pass
            if reasons:
                hits.append(
                    {
                        "pid": pid,
                        "name": name,
                        "source": "psutil",
                        "detail": "; ".join(reasons[:5]),
                        "class": _classify_process(name, pid),
                    }
                )
        except (psutil.Error, OSError):
            continue
    return hits


def _via_handle_exe(path: str | Path) -> list[dict[str, Any]]:
    """Optional Sysinternals ``handle.exe -accepteula -nobanner <path>``."""
    exe = shutil.which("handle64") or shutil.which("handle")
    if not exe:
        return []
    try:
        completed = subprocess.run(
            [exe, "-accepteula", "-nobanner", str(path)],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("handle.exe probe failed: %s", exc)
        return []

    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    # Example: "explorer.exe     pid: 1234  type: File  ...  E:\\path\\file"
    pattern = re.compile(
        r"(?P<name>[\w.\-]+)\s+pid:\s*(?P<pid>\d+).*?(?P<path>[A-Za-z]:\\[^\r\n]*)",
        re.IGNORECASE,
    )
    needle = _norm(path)
    hits: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for match in pattern.finditer(text):
        opath = match.group("path").strip()
        if not _norm(opath).startswith(needle):
            continue
        pid = int(match.group("pid"))
        name = match.group("name")
        key = (pid, name.lower())
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {
                "pid": pid,
                "name": name,
                "source": "handle.exe",
                "detail": opath,
                "class": _classify_process(name, pid),
            }
        )
    return hits


def _via_restart_manager(path: str | Path) -> list[dict[str, Any]]:
    """
    Windows Restart Manager (RmGetList) — best built-in lock holder API.

    Registers the directory (and common children) as resources.
    """
    if sys.platform != "win32":
        return []

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    rstrtmgr = ctypes.windll.rstrtmgr
    CCH_RM_SESSION_KEY = 32
    CCH_RM_MAX_APP_NAME = 255
    CCH_RM_MAX_SVC_NAME = 63

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [
            ("dwProcessId", wintypes.DWORD),
            ("ProcessStartTime", FILETIME),
        ]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
            ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
            ("ApplicationType", wintypes.UINT),
            ("AppStatus", wintypes.ULONG),
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    session = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
        return []

    hits: list[dict[str, Any]] = []
    try:
        root = Path(path)
        resources: list[str] = [str(root)]
        info_meta = root / ".info" / "metadata.json"
        if info_meta.is_file():
            resources.append(str(info_meta))
        cover_dir = root / ".info"
        if cover_dir.is_dir():
            for child in cover_dir.iterdir():
                if child.is_file() and child.name.lower().startswith("cover"):
                    resources.append(str(child))
                    break
        # Limit registration size.
        resources = resources[:8]
        arr = (wintypes.LPCWSTR * len(resources))(*resources)
        if (
            rstrtmgr.RmRegisterResources(
                session, len(resources), arr, 0, None, 0, None
            )
            != 0
        ):
            return []

        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reboot = wintypes.DWORD()
        # First call to get required size.
        rstrtmgr.RmGetList(
            session,
            ctypes.byref(needed),
            ctypes.byref(count),
            None,
            ctypes.byref(reboot),
        )
        if needed.value == 0:
            return []
        infos = (RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        rc = rstrtmgr.RmGetList(
            session,
            ctypes.byref(needed),
            ctypes.byref(count),
            infos,
            ctypes.byref(reboot),
        )
        if rc not in (0, 234):  # ERROR_MORE_DATA tolerated if partial
            return []
        for i in range(count.value):
            info = infos[i]
            pid = int(info.Process.dwProcessId)
            name = str(info.strAppName or "").strip() or f"pid:{pid}"
            # Prefer executable basename when app name is a display string.
            exe_name = name
            try:
                import psutil

                exe_name = Path(psutil.Process(pid).name() or name).name
            except Exception:  # noqa: BLE001
                exe_name = Path(name).name if name else f"pid:{pid}"
            hits.append(
                {
                    "pid": pid,
                    "name": exe_name,
                    "source": "restart_manager",
                    "detail": name,
                    "class": _classify_process(exe_name, pid),
                }
            )
    finally:
        rstrtmgr.RmEndSession(session)

    return hits


def audit_self_open_files(path: str | Path) -> list[str]:
    """
    List this process's open files under *path* (in-process handle audit).

    Focus: metadata.json, cover.*, offline/index.html, and any other open path.
    """
    needle = _norm(path)
    hits: list[str] = []
    try:
        import psutil
    except ImportError:
        return ["psutil_unavailable"]
    try:
        proc = psutil.Process(os.getpid())
        try:
            cwd = proc.cwd()
            if cwd and _norm(cwd).startswith(needle):
                hits.append(f"cwd={cwd}")
        except (psutil.Error, OSError):
            pass
        try:
            for of in proc.open_files() or []:
                opath = str(getattr(of, "path", "") or "")
                if not opath:
                    continue
                if _norm(opath).startswith(needle):
                    hits.append(opath)
        except (psutil.Error, OSError) as exc:
            hits.append(f"open_files_error={type(exc).__name__}")
    except (psutil.Error, OSError) as exc:
        hits.append(f"process_error={type(exc).__name__}")
    return hits


def find_processes_locking_path(path: str | Path) -> list[dict[str, Any]]:
    """
    Aggregate lock-holder probes (Restart Manager → handle.exe → psutil).

    Returns de-duplicated list of ``{pid, name, source, detail, class}``.
    Always includes a synthetic row when *this* process has open files under
    the path (in-process audit).
    """
    folder = Path(path)
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for probe in (_via_restart_manager, _via_handle_exe, _via_psutil):
        try:
            rows = probe(folder)
        except Exception:  # noqa: BLE001
            logger.warning(
                "lock probe %s failed: %s",
                probe.__name__,
                type(sys.exc_info()[1]).__name__,
                exc_info=True,
            )
            rows = []
        for row in rows:
            key = (int(row.get("pid") or 0), str(row.get("name") or "").lower())
            if key not in merged:
                merged[key] = row

    self_files = audit_self_open_files(folder)
    file_hits = [h for h in self_files if not h.startswith(("psutil_", "open_files_", "process_"))]
    if file_hits:
        key = (os.getpid(), "python.exe")
        detail = "; ".join(file_hits[:8])
        merged[key] = {
            "pid": os.getpid(),
            "name": Path(sys.executable).name or "python.exe",
            "source": "self_open_files",
            "detail": detail,
            "class": "internal",
        }
    return list(merged.values())


def summarize_lock_holders(holders: list[dict[str, Any]]) -> str:
    """One-line summary for logs / UI error detail."""
    if not holders:
        return (
            "no lock holder identified "
            "(try Sysinternals handle.exe as Admin, or check Explorer/AV)"
        )
    parts: list[str] = []
    for row in holders[:8]:
        parts.append(
            f"{row.get('name')} pid={row.get('pid')} "
            f"[{row.get('class')}/{row.get('source')}]"
        )
    return "; ".join(parts)


def log_path_lock_holders(
    path: str | Path,
    *,
    prefix: str = "Rename WinError5",
    attempt: int | None = None,
    log: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """
    Run probes and log results (always, even when empty).

    *log* defaults to this module logger; pass the caller's logger so the
    line is visible in the same channel as rename failures.
    """
    import time as _time

    out = log or logger
    ts = _time.strftime("%H:%M:%S")
    holders = find_processes_locking_path(path)
    summary = summarize_lock_holders(holders)
    self_audit = audit_self_open_files(path)
    attempt_bit = f" attempt={attempt}" if attempt is not None else ""
    out.error(
        "%s holders%s ts=%s path=%s => %s",
        prefix,
        attempt_bit,
        ts,
        path,
        summary,
    )
    out.error(
        "%s self_open_files%s => %s",
        prefix,
        attempt_bit,
        self_audit or "(none)",
    )
    for row in holders:
        out.error(
            "%s detail: name=%s pid=%s class=%s source=%s detail=%s",
            prefix,
            row.get("name"),
            row.get("pid"),
            row.get("class"),
            row.get("source"),
            row.get("detail"),
        )
    return holders
