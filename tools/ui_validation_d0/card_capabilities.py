"""Synthetic capabilities only. NOT connected to a production Card or worker."""
from __future__ import annotations
import functools
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import weakref
from .safety import Boundary, IsolationError


class SyntheticCardDTO(SimpleNamespace):
    @property
    def internal_id(self):
        return self.id

    @property
    def mod_id(self):
        return str(self.mod_pk) if self.mod_pk is not None else ""


def synthetic_dto(repository: Path, root: Path, number=1):
    """AST-derived field/default contract; never imports the business DTO module."""
    import ast
    tree = ast.parse((repository / "services/mod_library_cache.py").read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ModCardData")
    required = dict(id=str(uuid.UUID(int=number, version=4)), title="Synthetic " + str(number),
                    platform="steam", cover="", description="", tags="", size=None,
                    updated_time=0.0, managed_path=str(root / "mods" / str(number)), game_folder="synthetic")
    fields = {}
    for field in node.body:
        if isinstance(field, ast.AnnAssign):
            fields[field.target.id] = ast.literal_eval(field.value) if field.value else required[field.target.id]
    fields.update(workspace_id="synthetic-workspace-" + str(number), mod_pk=100 + number)
    return SyntheticCardDTO(**fields)


class Resources:
    def __init__(self, boundary: Boundary):
        self.boundary = boundary

    def path(self, value):
        p = Path(value)
        if ".." in p.parts:
            raise IsolationError("Traversal refused")
        return self.boundary.target(p if p.is_absolute() else self.boundary.root / p)

    def read(self, value):
        path = self.path(value)
        if not path.is_file():
            raise IsolationError("Synthetic resource missing")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise IsolationError("Synthetic read failed") from exc


class ControlledCoverProvider:
    """Deterministic event queue; no Qt/pool/thread/network/native image decoding.

    Events carry a request sequence: duplicate same-token requests remain
    distinguishable in the provider. This is NOT a production guard fix.
    """
    def __init__(self, resources: Resources):
        self.resources = resources
        self.pending = {}
        self.events = []
        self.sequence = 0
        self.closed = False

    def request(self, token, managed_path, *, cover_ref="", width, height, on_ready):
        if self.closed or not token or width <= 0 or height <= 0:
            raise IsolationError("Invalid/closed request")
        path = self.resources.path(managed_path)
        data = self.resources.read(cover_ref) if cover_ref else None
        try:
            callback = weakref.WeakMethod(on_ready)
        except TypeError:
            callback = on_ready
        self.sequence += 1
        self.pending[str(token)] = (self.sequence, str(path), data, callback)
        self.events.append(("request", str(token), self.sequence))
        return self.sequence

    def cancel(self, token):
        self.pending.pop(str(token), None)
        self.events.append(("cancel", str(token)))

    def release_path(self, value):
        path = str(self.resources.path(value))
        for token, entry in list(self.pending.items()):
            if entry[1] == path:
                self.cancel(token)
        self.events.append(("release", path))

    def complete(self, token, sequence):
        token = str(token)
        entry = self.pending.get(token)
        if self.closed or entry is None or entry[0] != sequence:
            self.events.append(("drop", token, sequence))
            return False
        self.pending.pop(token)
        callback = entry[3]() if isinstance(entry[3], weakref.WeakMethod) else entry[3]
        if callback is None:
            self.events.append(("dead_receiver", token))
            return False
        try:
            callback(token, entry[2])
        except Exception:
            self.events.append(("callback_error", token, sequence))
            raise
        self.events.append(("deliver", token, sequence))
        return True

    def close(self):
        self.closed = True
        self.pending.clear()
        self.events.append(("close",))


class SyntheticDiagnosticSink:
    def __init__(self, resources: Resources, config="settings/debug.json", log="logs/errors.jsonl"):
        self.resources = resources
        self.config_path = resources.path(config)
        self.log_path = resources.path(log)
        self.records = []
        self.cache = None

    def settings(self):
        if self.cache is None:
            if not self.config_path.is_file():
                raise IsolationError("Missing synthetic diagnostic config; no fallback")
            self.cache = json.loads(self.resources.read(self.config_path))
        return self.cache

    def popup(self, text):
        # No environment flag or production configuration fallback.
        if self.settings().get("ui_trace", False):
            self.record("popup", text)

    def record(self, kind, detail):
        value = {"kind": kind, "detail": str(detail)}
        self.records.append(value)  # Evidence retained even if log write fails.
        path = self.resources.path(self.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def traced(self, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as original:
                try:
                    self.record("exception", repr(original))
                except (OSError, IsolationError) as failure:
                    self.records.append({"kind": "sink_failure", "detail": repr(failure)})
                raise
        return wrapper


def card_preflight(*, production_running, imports_proven=False, diagnostics_connected=False,
                   covers_connected=False, native_io_confined=False):
    requirements = {"production_exit": not production_running, "imports_proven": imports_proven,
                    "diagnostics_connected": diagnostics_connected, "covers_connected": covers_connected,
                    "native_io_confined": native_io_confined, "real_card_binding": False}
    return {"admitted": all(requirements.values()), "requirements": requirements,
            "scope": "No loader or production Card binding has been implemented"}
