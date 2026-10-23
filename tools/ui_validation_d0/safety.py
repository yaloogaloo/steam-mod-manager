"""Fail-closed admission and Python I/O guard; NOT a native-code sandbox."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import stat


class IsolationError(RuntimeError):
    pass


class FixtureDatabase:
    """Explicit fixture-only connector. Not installed into production DAL aliases.

    Environment is revalidated on every connection; no default path or creation.
    Caller capabilities constrain this API, not arbitrary native SQLite callers.
    """
    def __init__(self, boundary, path, environment, caller_capability):
        self.boundary = boundary
        self.path = boundary.target(path)
        self.environment = environment
        self.capability = caller_capability
        if self.path != boundary.root / "data" / "fixture.db":
            raise IsolationError("Only canonical fixture DB allowed")

    def connect(self, caller_capability):
        import sqlite3
        if caller_capability is not self.capability:
            raise IsolationError("Unregistered database caller")
        configured = self.environment.get("SMM_TEST_DB")
        if not configured:
            raise IsolationError("Missing explicit SMM_TEST_DB; no fallback")
        actual = self.boundary.target(configured)
        if actual != self.path or not actual.is_file():
            raise IsolationError("Fixture DB missing or mismatched")
        # Resolve again to reject replaced symlinks; native TOCTOU remains a
        # sandbox requirement. mode=rw never silently creates a missing DB.
        self.boundary.target(self.path)
        connection = sqlite3.connect(actual.as_uri() + "?mode=rw", uri=True)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def local_absolute(path: str | Path) -> Path:
    candidate = Path(path)
    value = str(candidate)
    if not candidate.is_absolute() or value.startswith(("\\\\", "//")):
        raise IsolationError("Only absolute local paths allowed; UNC/device/relative paths refused")
    if os.name == "nt" and ":" in value[2:]:
        raise IsolationError("Alternate data streams refused")
    # Reject existing reparse ancestors even when their resolved destination is
    # within root. This is not a native handle-based TOCTOU sandbox.
    for part in (candidate, *candidate.parents):
        try:
            attributes = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
            raise IsolationError("Reparse point / symbolic link path refused")
    return candidate


class Boundary:
    def __init__(self, root: Path, repository: Path):
        self.root = local_absolute(root).resolve()
        self.repository = local_absolute(repository).resolve()
        workspace = self.repository / "_tmp"
        if (self.root == Path(self.root.anchor) or under(self.repository, self.root)
                or (under(self.root, self.repository) and not under(self.root, workspace))
                or self.root == workspace or under(self.root, workspace / "protected_recovery")):
            raise IsolationError("Test root must use a disposable _tmp child or an external isolated directory")

    def target(self, path: str | Path) -> Path:
        resolved = local_absolute(path).resolve()
        if not under(resolved, self.root):
            raise IsolationError("Target outside test root")
        return resolved

    def read(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if under(resolved, self.root):
            return resolved
        if under(resolved, self.repository):
            if resolved.suffix == ".py":
                return resolved
            raise IsolationError("Production data/resource read refused")
        if any(under(resolved, Path(prefix)) for prefix in (sys.prefix, sys.base_prefix)):
            return resolved
        raise IsolationError("Read outside synthetic/code/runtime roots refused")

    def audit(self, event: str, args: tuple) -> None:
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2"}:
            raise IsolationError("External capability refused: " + event)
        if event.startswith("winreg."):
            raise IsolationError("Registry access refused")
        if event == "sqlite3.connect":
            self.target(args[0])
        elif event == "open" and not isinstance(args[0], int):
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(x in mode for x in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            )
            (self.target if writing else self.read)(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
            self.target(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            self.target(args[0])
            self.target(args[1])

    def install(self) -> None:
        """Child process only: audit hooks cannot be uninstalled."""
        sys.addaudithook(self.audit)


def suspects(processes: list[dict], repository: Path) -> list[dict]:
    token = str(repository.resolve()).lower().replace("/", "\\")
    found = []
    for p in processes:
        name = (p.get("Name") or "").lower()
        command = (p.get("CommandLine") or "").lower().replace("/", "\\")
        if name in {"powershell.exe", "pwsh.exe", "cmd.exe"}:
            continue
        if name in {"smm.exe", "steam-mod-manager.exe", "steammodmanager.exe"} or (
            token in command and ("main.py" in command or "worker" in command)
        ):
            found.append(p)
    # A child may omit repository/main.py in its command line. Retain every
    # descendant of known production processes, including native workers.
    ids = {p.get("ProcessId") for p in found if p.get("ProcessId") is not None}
    while True:
        children = [p for p in processes if p.get("ParentProcessId") in ids and p.get("ProcessId") not in ids]
        if not children:
            break
        found.extend(children)
        ids.update(p.get("ProcessId") for p in children)
    return found


# These require actual integration evidence, not a caller-supplied PASS file.
# No switch / environment variable can waive them.
NATIVE_BLOCKERS = (
    "Native Qt/curl filesystem and network paths are not confined by Python audit hooks",
    "All QSettings imported aliases and native fallback locations are not integrated",
    "Production worker scheduling / game discovery / shutdown replacements not installed",
)


def launch_checks(boundary: Boundary, active_processes: list[dict], environment: dict) -> list[dict]:
    """Every condition is independently reported before any Qt import.

    No caller supplied PASS flag or TEST_MODE grants OS/native admission.
    Native verification is deliberately unavailable until a real backend exists.
    """
    checks = []

    def add(name, ready, reason):
        checks.append({"check": name, "status": "PASS" if ready else "NOT CLOSED", "reason": reason})

    add("production_exit", not active_processes,
        "Known production processes/descendants running" if active_processes else "No known production entrypoints running; not proof against unidentified processes")
    add("test_root", True, "Boundary rejects repository, ancestor, drive root, UNC, relative and reparse paths")
    try:
        configured = environment.get("SMM_TEST_DB")
        if not configured:
            raise IsolationError("SMM_TEST_DB absent; no fallback")
        path = boundary.target(configured)
        if path != boundary.root / "data" / "fixture.db" or not path.is_file():
            raise IsolationError("Canonical fixture DB missing or mismatched")
        add("sqlite_path", True, "Existing explicit synthetic path; production DAL aliases still unintegrated")
    except (IsolationError, OSError) as exc:
        add("sqlite_path", False, str(exc))
    for name in ("data", "cache", "temp", "assets", "cas", "manifest", "backup", "mods", "games", "logs", "settings"):
        try:
            path = boundary.target(boundary.root / name)
            add("root_" + name, path.is_dir(), "Local resolved directory exists" if path.is_dir() else "Required isolated directory missing")
        except (IsolationError, OSError) as exc:
            add("root_" + name, False, str(exc))
    for name, reason in (
        ("sqlite_aliases", "Real DatabaseManager/get_db call chains not confined"),
        ("qsettings", "Explicit INI factory exists; default/native/imported aliases and app namespace unverified"),
        ("os_boundary", "No implemented, verified OS sandbox/restricted identity backend"),
        ("native_filesystem", "Qt native I/O and TOCTOU not confined by Python audit"),
        ("network", "No verified OS egress denial; Python hooks cannot confine curl/Qt"),
        ("workers", "Real page business dispatch has not been replaced/closed"),
        ("startup_shutdown", "Native page startup/settings/flush/exit paths unverified"),
        ("production_baseline", "No confirmed post-exit stable baseline admission protocol"),
    ):
        add(name, False, reason)
    return checks


def require_ui_admission(active_processes: list[dict], *, boundary=None, environment=None) -> None:
    if active_processes:
        raise IsolationError("Production SMM is running; UI admission refused")
    if boundary is None:
        raise IsolationError("SAFE UI LAUNCH: BLOCKED; explicit verified path context required")
    rejected = [item for item in launch_checks(boundary, active_processes, environment or {}) if item["status"] != "PASS"]
    if rejected:
        raise IsolationError("SAFE UI LAUNCH: BLOCKED; " + "; ".join(item["check"] + ": " + item["reason"] for item in rejected))
