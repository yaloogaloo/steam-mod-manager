#!/usr/bin/env python3
"""P0 read-only forensics. Must not mutate production DB or library."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.paths import database_path, default_mod_library  # noqa: E402

GHOSTS = [str(i) for i in range(9_000_000_000_003_438, 9_000_000_000_003_451)]
CANONICALS = [
    "3591453758",
    "3592539424",
    "3781246892",
    "3783660244",
    "3784396849",
    "3784602736",
    "3785095584",
    "3785271947",
    "3786388428",
    "3786411372",
    "3787384780",
    "3789395672",
    "3790849356",
]
DUPES = [
    "9000000000000349",
    "9000000000000351",
]
CONFLICT_WS = "17863521013284165"
CONFLICT_MID = "9000000000000362"
PARTNER_MID = "9000000000000360"
ARCHIVE_MID = "3786388428"


def utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def main() -> int:
    out: dict = {
        "generated_at": utc(),
        "readonly": True,
        "production_mutated": False,
        "apply_executed": False,
    }
    db_path = database_path()
    library = default_mod_library()
    out["db_path"] = str(db_path)
    out["db_exists"] = db_path.is_file()
    out["db_size"] = db_path.stat().st_size if db_path.is_file() else 0
    out["library"] = str(library)

    conn = open_ro(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
        n = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
        out["mod_row_count"] = int(n)

        def fetch_ids(ids: list[str]) -> list[dict]:
            rows = []
            for mid in ids:
                row = conn.execute(
                    """
                    SELECT mod_id, app_id, title, platform, external_id, workspace_id,
                           source_url, last_known_path, conflict_status, conflict_note,
                           library_status, content_status, deploy_status, deploy_path,
                           deploy_time, offline_status, updated_at, created_at
                    FROM mods WHERE CAST(mod_id AS TEXT) = ?
                    """,
                    (mid,),
                ).fetchone()
                cols = [r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()]
                # created_at may not exist
                if row is None:
                    rows.append({"mod_id": mid, "present": False})
                    continue
                d = row_dict(row)
                d["present"] = True
                d["columns_available"] = cols
                rows.append(d)
            return rows

        # Detect columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()]
        out["mods_columns"] = cols
        select_cols = [
            c
            for c in [
                "mod_id",
                "app_id",
                "title",
                "platform",
                "external_id",
                "workspace_id",
                "source_url",
                "last_known_path",
                "conflict_status",
                "conflict_note",
                "library_status",
                "content_status",
                "deploy_status",
                "deploy_path",
                "deploy_time",
                "offline_status",
                "updated_at",
                "is_enabled",
                "enabled",
            ]
            if c in cols
        ]
        col_sql = ", ".join(select_cols)

        def get_mod(mid: str) -> dict:
            row = conn.execute(
                f"SELECT {col_sql} FROM mods WHERE CAST(mod_id AS TEXT) = ?",
                (mid,),
            ).fetchone()
            if row is None:
                return {"mod_id": mid, "present": False}
            d = row_dict(row)
            d["present"] = True
            return d

        out["ghosts"] = [get_mod(g) for g in GHOSTS]
        out["ghosts_present"] = sum(1 for g in out["ghosts"] if g.get("present"))
        out["canonicals"] = [get_mod(c) for c in CANONICALS]
        out["canonicals_present"] = sum(1 for c in out["canonicals"] if c.get("present"))

        ws_row = conn.execute(
            f"SELECT {col_sql} FROM mods WHERE CAST(workspace_id AS TEXT) = ?",
            (CONFLICT_WS,),
        ).fetchone()
        out["conflict_by_workspace_id"] = row_dict(ws_row) if ws_row else None
        out["conflict_mod"] = get_mod(CONFLICT_MID)
        out["conflict_partner"] = get_mod(PARTNER_MID)
        out["archive_mod"] = get_mod(ARCHIVE_MID)

        conflict_rows = conn.execute(
            f"""
            SELECT {col_sql} FROM mods
            WHERE conflict_status IS NOT NULL AND TRIM(conflict_status) != ''
              AND TRIM(conflict_status) != 'none'
            """
        ).fetchall()
        out["conflict_status_nonzero"] = [row_dict(r) for r in conflict_rows]
        out["conflict_status_nonzero_count"] = len(conflict_rows)

        # references to ghosts
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        ghost_refs: dict[str, int] = {}
        for gid in GHOSTS[:1]:
            total = 0
            for table in tables:
                tcols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                for col in tcols:
                    try:
                        n = conn.execute(
                            f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                            (gid,),
                        ).fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if n:
                        ghost_refs[f"{table}.{col}"] = int(n)
                        total += int(n)
            break
        out["ghost_3438_exact_column_hits"] = ghost_refs

        # identity pollution counts
        if "source_url" in cols:
            polluted = conn.execute(
                """
                SELECT COUNT(*) FROM mods
                WHERE source_url LIKE '%900000000000%'
                   OR source_url LIKE '%/id/9000%'
                """
            ).fetchone()[0]
            out["source_url_contains_9000_count"] = int(polluted)

        # 9 invalid duplicates from preflight
        preflight = ROOT / "data" / "identity_repair_preflight.json"
        if preflight.is_file():
            pf = json.loads(preflight.read_text(encoding="utf-8"))
            invalids = pf.get("invalid_entities") or []
            dup_ids = [
                e["invalid_mod_id"]
                for e in invalids
                if e.get("finding_class") == "duplicate_source_url"
            ]
            out["preflight_duplicate_ids"] = dup_ids
            out["duplicates_still_present"] = [
                get_mod(i) for i in dup_ids
            ]
            out["duplicates_present_count"] = sum(
                1 for d in out["duplicates_still_present"] if d.get("present")
            )
            out["preflight_READY_FOR_APPLY"] = pf.get("READY_FOR_APPLY")
            out["preflight_apply_executed"] = pf.get("apply_executed")
            out["preflight_production_mutated"] = pf.get("production_mutated")

    finally:
        conn.close()

    # Filesystem ghosts
    duck = library / "逃离鸭科夫"
    unknown = []
    if duck.is_dir():
        for child in duck.iterdir():
            if child.is_dir() and child.name.startswith("Unknown"):
                unknown.append(child.name)
    out["unknown_mod_folders_duckov"] = unknown
    out["ghost_folder_exists"] = {}
    for gid, steam in zip(GHOSTS, CANONICALS):
        folder = duck / f"Unknown Mod {steam}"
        out["ghost_folder_exists"][gid] = {
            "path": str(folder),
            "exists": folder.is_dir(),
        }
    canon_folder = duck / "Duck Tracks"
    out["canonical_duck_tracks_exists"] = canon_folder.is_dir()
    stub = canon_folder / ".info" / "index.html"
    out["duck_tracks_stub_exists"] = stub.is_file()
    if stub.is_file():
        text = stub.read_text(encoding="utf-8", errors="replace")
        out["duck_tracks_stub_has_curl28"] = "curl: (28)" in text
        out["duck_tracks_stub_bytes"] = stub.stat().st_size

    # Conflict manifests
    def load_manifest(folder: Path) -> dict:
        p = folder / ".info" / "deploy_manifest.json"
        if not p.is_file():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    a = library / "Anno 1800" / "游戏初期的【布局模板】"
    b = library / "Anno 1800" / "全产业模板"
    ma = load_manifest(a)
    mb = load_manifest(b)
    ta = {str(Path(f["target"]).expanduser()) for f in ma.get("files") or [] if f.get("target")}
    tb = {str(Path(f["target"]).expanduser()) for f in mb.get("files") or [] if f.get("target")}
    # also resolved
    def resolved_set(files: list) -> set[str]:
        s: set[str] = set()
        for f in files:
            t = f.get("target") or ""
            if not t:
                continue
            try:
                s.add(str(Path(t).expanduser().resolve()))
            except OSError:
                s.add(str(Path(t)))
        return s

    ra = resolved_set(ma.get("files") or [])
    rb = resolved_set(mb.get("files") or [])
    overlap = ra & rb
    out["conflict_trace"] = {
        "mod_a": {
            "folder": str(a),
            "mod_id": ma.get("mod_id"),
            "deploy_time": ma.get("deploy_time"),
            "file_count": len(ma.get("files") or []),
            "unique_resolved_targets": len(ra),
        },
        "mod_b": {
            "folder": str(b),
            "mod_id": mb.get("mod_id"),
            "deploy_time": mb.get("deploy_time"),
            "file_count": len(mb.get("files") or []),
            "unique_resolved_targets": len(rb),
        },
        "overlap_resolved_count": len(overlap),
        "overlap_sample": sorted(overlap)[:8],
        "conflict_type": "FILE_OVERWRITE",
        "rule": "identical Path.resolve() deploy target claimed by >=2 enabled mods",
        "identity_conflict": False,
        "workspace_id_user_cited": CONFLICT_WS,
        "internal_mod_id": CONFLICT_MID,
    }

    # zip sizes
    zips = list(a.glob("*.zip")) + list(a.glob("*.7z"))
    out["layout_template_archives"] = [
        {"path": str(p), "bytes": p.stat().st_size} for p in zips
    ]

    # Library-wide manifest walk timing (read-only, no DB persist)
    t0 = time.perf_counter()
    manifest_files = 0
    target_entries = 0
    mods_with_manifest = 0
    resolve_ms_acc = 0.0
    for folder in library.iterdir() if library.is_dir() else []:
        if not folder.is_dir():
            continue
        for mod_folder in folder.iterdir():
            man = mod_folder / ".info" / "deploy_manifest.json"
            if not man.is_file():
                continue
            mods_with_manifest += 1
            manifest_files += 1
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            files = data.get("files") or []
            target_entries += len(files)
            r0 = time.perf_counter()
            for f in files:
                t = f.get("target") or ""
                if t:
                    try:
                        Path(t).expanduser().resolve()
                    except OSError:
                        pass
            resolve_ms_acc += (time.perf_counter() - r0) * 1000.0
    walk_ms = (time.perf_counter() - t0) * 1000.0
    out["manifest_scan"] = {
        "mods_with_manifest": mods_with_manifest,
        "manifest_files": manifest_files,
        "target_entries": target_entries,
        "walk_and_resolve_ms": round(walk_ms, 1),
        "path_resolve_ms": round(resolve_ms_acc, 1),
    }

    # Simulate persist hashing: hash layout zip N times (current code path)
    if zips:
        zip_path = zips[0]
        n = len(ma.get("files") or []) or 1
        t0 = time.perf_counter()
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        once_ms = (time.perf_counter() - t0) * 1000.0
        t0 = time.perf_counter()
        for _ in range(n):
            hashlib.sha256(zip_path.read_bytes()).hexdigest()
        n_ms = (time.perf_counter() - t0) * 1000.0
        out["hash_simulation"] = {
            "zip": str(zip_path),
            "zip_bytes": zip_path.stat().st_size,
            "sha256": digest,
            "hash_once_ms": round(once_ms, 1),
            "hash_n_times": n,
            "hash_n_times_ms": round(n_ms, 1),
        }

    # Environment / archive
    env: dict = {}
    env["python"] = sys.executable
    env["python_version"] = sys.version
    env["cwd"] = os.getcwd()
    env["project_root"] = str(ROOT)
    try:
        import curl_cffi

        env["curl_cffi_version"] = getattr(curl_cffi, "__version__", "unknown")
        env["curl_cffi_file"] = getattr(curl_cffi, "__file__", "")
    except Exception as exc:  # noqa: BLE001
        env["curl_cffi_error"] = str(exc)
    try:
        from curl_cffi import requests as curl_requests

        env["curl_cffi_requests"] = str(curl_requests)
    except Exception as exc:  # noqa: BLE001
        env["curl_cffi_requests_error"] = str(exc)

    try:
        from PySide6.QtCore import QSettings

        qs = QSettings("SteamModManager", "WorkshopLibrary")
        env["qsettings_proxy_url"] = str(qs.value("network/proxy_url", "") or "")
        env["qsettings_file"] = str(qs.fileName()) if hasattr(qs, "fileName") else ""
        env["qsettings_keys_sample"] = [str(k) for k in list(qs.allKeys())[:40]]
    except Exception as exc:  # noqa: BLE001
        env["qsettings_error"] = str(exc)

    proxy_env = {
        k: os.environ.get(k, "")
        for k in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
        )
    }
    env["process_proxy_env"] = proxy_env

    # DNS + TCP
    host = "steamcommunity.com"
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        env["dns_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        env["dns_addrs"] = sorted({i[4][0] for i in infos})
    except Exception as exc:  # noqa: BLE001
        env["dns_error"] = str(exc)
        env["dns_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    def tcp_probe(addr: str, port: int, timeout: float = 5.0) -> dict:
        t1 = time.perf_counter()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((addr, port))
            ok = True
            err = ""
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = str(exc)
        finally:
            s.close()
        return {
            "addr": addr,
            "port": port,
            "ok": ok,
            "error": err,
            "elapsed_ms": round((time.perf_counter() - t1) * 1000.0, 1),
        }

    addrs = env.get("dns_addrs") or []
    env["tcp_443"] = [tcp_probe(a, 443, 5.0) for a in addrs[:3]]
    env["socks5_7897"] = tcp_probe("127.0.0.1", 7897, 1.0)
    env["socks5_7890"] = tcp_probe("127.0.0.1", 7890, 1.0)
    env["http_7897"] = tcp_probe("127.0.0.1", 1080, 1.0)

    # Optional: curl_cffi GET with current timeout, only if we can do a short probe
    # Use 8s timeout to classify; do not write any files.
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={ARCHIVE_MID}"
    proxy = str(env.get("qsettings_proxy_url") or "").strip()
    env["steam_url"] = url
    try:
        from curl_cffi import requests as curl_requests

        def try_get(label: str, proxies: dict | None, timeout: int = 8) -> dict:
            t1 = time.perf_counter()
            rec: dict = {"label": label, "proxies": proxies, "timeout": timeout}
            try:
                kw = {"timeout": timeout, "impersonate": "chrome131", "allow_redirects": True}
                if proxies:
                    kw["proxies"] = proxies
                resp = curl_requests.get(url, **kw)
                rec["status"] = int(getattr(resp, "status_code", 0) or 0)
                rec["bytes"] = len(resp.content or b"")
                rec["error"] = ""
            except Exception as exc:  # noqa: BLE001
                rec["status"] = None
                rec["bytes"] = 0
                rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["elapsed_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
            return rec

        probes = [try_get("direct", None, 8)]
        if proxy:
            probes.append(try_get("qsettings_proxy", {"http": proxy, "https": proxy}, 8))
        if env["socks5_7897"]["ok"]:
            p = "socks5://127.0.0.1:7897"
            probes.append(try_get("socks5_7897", {"http": p, "https": p}, 8))
        env["http_probes"] = probes
    except Exception as exc:  # noqa: BLE001
        env["http_probe_error"] = str(exc)

    out["environment"] = env

    dest = ROOT / "data" / "p0_forensics_snapshot.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in out if k not in ("canonicals",)}, ensure_ascii=False, indent=2, default=str)[:12000])
    print("WROTE", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
