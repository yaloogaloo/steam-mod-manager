"""Diagnostics mutate only an explicit temporary root. REAL_DATA_GATE: read-only hash."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from services.mutation_context import MutationBoundaryError
from tests.diagnostics.run_transient_popup_forensic import (
    main as forensic_main,
    require_temporary_roots,
)

ROOT = Path(__file__).resolve().parents[1]
PROD_DB = ROOT / "data" / "mod_manager.db"
FORENSIC_SHA = "b519f59ef62cd731c45f51297c9ae75c8888cc6a49e8980344fbef2fb68bdcd1"
VICTIMS = (
    "551f7eef-ddfe-4ffa-8b38-b96b14e97567",
    "2f51c59b-1ad3-4914-a520-1ba2449892a9",
    "8eb9d480-cf4b-43ab-83db-f6da003d5b7f",
)


def _sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _production_cover_paths() -> list[Path]:
    if not PROD_DB.is_file():
        return []
    uri = PROD_DB.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT internal_id, last_known_path FROM mods WHERE internal_id IN (?, ?, ?)",
            VICTIMS,
        ).fetchall()
    finally:
        conn.close()
    paths: list[Path] = []
    for internal_id, live in rows:
        live_root = Path(str(live or ""))
        info = live_root / ".info"
        if info.is_dir():
            paths.extend(info.glob("cover.*"))
            meta = info / "metadata.json"
            if meta.is_file():
                paths.append(meta)
        backup = ROOT / "data" / "mod_backup" / str(internal_id)
        if backup.is_dir():
            paths.extend(backup.glob("cover.*"))
    return paths


def test_missing_fixture_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORENSIC_FIXTURE_ROOT", raising=False)
    monkeypatch.delenv("FORENSIC_DATA_ROOT", raising=False)
    with pytest.raises(MutationBoundaryError):
        require_temporary_roots()
    assert forensic_main() == 2


def test_forensic_run_leaves_production_bytes_unchanged(tmp_path: Path) -> None:
    """Real safety gate: forensic flow on a temp fixture, production bytes unchanged."""
    watched = [PROD_DB, *_production_cover_paths()]
    before = {str(path): _sha(path) for path in watched}
    env = os.environ.copy()
    env.pop("SMM_TEST_DB", None)
    env.pop("FORENSIC_SKIP_A", None)
    env.pop("FORENSIC_INTERNAL_ID", None)
    env["FORENSIC_FIXTURE_ROOT"] = str(tmp_path / "library")
    env["FORENSIC_DATA_ROOT"] = str(tmp_path / "data")
    env["FORENSIC_SKIP_B"] = "1"
    env["FORENSIC_REFRESH_TIMEOUT_MS"] = "8000"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "diagnostics" / "run_transient_popup_forensic.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    after = {str(path): _sha(path) for path in watched}
    assert before == after, proc.stderr[-2000:]
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    fixture_covers = list((tmp_path / "library").rglob("cover.*"))
    assert fixture_covers
    assert all(tmp_path in path.parents for path in fixture_covers)
    for path in watched:
        if path.name.startswith("cover.") and _sha(path) == FORENSIC_SHA:
            continue


def test_static_diagnostics_have_no_production_fallback() -> None:
    root = ROOT / "tests"
    allow = {
        line.strip().replace("\\", "/")
        for line in (root / "allowlists" / "real_data_read_gates.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    banned_calls = ("_pick_steam_card", "activate_production_context(")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel.startswith("tests/diagnostics/"):
            for token in banned_calls + ("default_mod_library()",):
                if token in text:
                    offenders.append(f"{rel}: {token}")
            if "require_temporary_roots" not in text and path.name.startswith("run_"):
                offenders.append(f"{rel}: missing require_temporary_roots")
        if rel in allow:
            continue
        for needle in ("data/mod_manager.db", "data/mod_backup", "data\\\\mod_backup"):
            if needle in text:
                offenders.append(f"{rel}: {needle}")
        # Exact library root used as a path, not a longer example such as E:\\mods.
        if 'Path(r"E:\\mod")' in text or 'Path(r"E:/mod")' in text:
            offenders.append(f"{rel}: production library path")
    assert offenders == []
