"""Negative Python capability probes in a sacrificial child, never real targets."""
from pathlib import Path
from contextlib import closing
import json
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_validation_d0.safety import Boundary, IsolationError


def main():
    root, repository = Path(sys.argv[1]), Path(sys.argv[2])
    boundary = Boundary(root, repository)
    root.mkdir(parents=True, exist_ok=True)
    boundary.install()
    observations = []
    operations = {
        "outside_write": lambda: (root.parent / "denied-write.txt").write_text("denied"),
        "outside_read": lambda: (root.parent / "denied-read.txt").read_bytes(),
        "outside_sqlite": lambda: sqlite3.connect(root.parent / "denied.db"),
        "dns": lambda: socket.getaddrinfo("localhost", 80),
        "subprocess": lambda: subprocess.run([sys.executable, "-c", "pass"]),
    }
    for name, operation in operations.items():
        try:
            operation()
        except IsolationError:
            observations.append({"probe": name, "blocked": True})
        else:
            observations.append({"probe": name, "blocked": False})
    with closing(sqlite3.connect(root / "safe.db")) as db:
        db.execute("CREATE TABLE synthetic(id INTEGER)")
        db.execute("INSERT INTO synthetic VALUES (1)")
        db.commit()
    (root / "allowed.txt").write_text("synthetic")
    print(json.dumps({"observations": observations, "allowed_root_write": True, "native_sandbox_proven": False}))
    return 0 if all(item["blocked"] for item in observations) else 1


if __name__ == "__main__":
    raise SystemExit(main())
