"""Controlled D:→F: copy matrix for workspace_id 3308841144. Profiling only.

Deploy identity = Frozen internal_id. mod_pk = DB handle only.
workspace_id = platform identity. No copy-API changes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHUNK = 1024 * 1024
SLOW_FILE_MS = 1000.0
DEEP_MS = 60_000.0
DEEPER_MS = 120_000.0
WS = "3308841144"
APP_ID = 262060
RUNS = 3

SOURCE = Path(
    r"D:\project\steam-mod-manager\mod\暗黑地牢\The Abigail Williams Class"
)
OUT_DIR = ROOT / "_tmp" / "copy_matrix"


def _skip_rel(rel: Path) -> bool:
    from services.importers.archive import is_archive_path
    from services.importers.local_scanner import is_skipped_mod_path_part

    if any(is_skipped_mod_path_part(part) for part in rel.parts):
        return True
    if is_archive_path(rel.name):
        return True
    return False


def list_source_files(source: Path) -> list[tuple[str, Path, int]]:
    out: list[tuple[str, Path, int]] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(source)
        except ValueError:
            continue
        if _skip_rel(rel):
            continue
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        out.append((rel.as_posix(), path, size))
    out.sort(key=lambda item: item[0].lower())
    return out


def extract_overlay(source: Path, dest: Path) -> list[tuple[str, Path, int]]:
    """Materialize archive overlay onto D: temp. Does not modify source."""
    from services.importers.archive import is_archive_path
    from services.deploy_apply import extract_archive_via_core

    archives = [
        p for p in source.rglob("*") if p.is_file() and is_archive_path(p.name)
    ]
    if not archives:
        return []
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        extract_archive_via_core(archive, dest)
    overlay: list[tuple[str, Path, int]] = []
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(dest)
        except ValueError:
            continue
        if _skip_rel(rel):
            continue
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        overlay.append((rel.as_posix(), path, size))
    overlay.sort(key=lambda item: item[0].lower())
    return overlay


def merge_payload(
    outer: list[tuple[str, Path, int]],
    overlay: list[tuple[str, Path, int]],
) -> list[tuple[str, Path, int]]:
    merged: dict[str, tuple[str, Path, int]] = {rel: (rel, path, size) for rel, path, size in outer}
    for rel, path, size in overlay:
        merged[rel] = (rel, path, size)
    return [merged[k] for k in sorted(merged, key=str.lower)]


def volume_id(path: Path) -> tuple[str, float]:
    t0 = time.perf_counter()
    from services.deploy_op_profile import volume_id as _volume_id

    value = _volume_id(path)
    return value, (time.perf_counter() - t0) * 1000.0


def rmtree_timed(path: Path) -> float:
    if not path.exists():
        return 0.0
    t0 = time.perf_counter()
    shutil.rmtree(path, ignore_errors=False)
    return (time.perf_counter() - t0) * 1000.0


def mutate_existing(root: Path, files: list[tuple[str, Path, int]]) -> int:
    """Change content, keep size, same names. Returns mutated file count."""
    changed = 0
    for rel, _src, size in files:
        if size < 1024:
            continue
        dest = root / rel
        if not dest.is_file():
            continue
        try:
            with dest.open("r+b") as handle:
                handle.seek(-1, 2)
                last = handle.read(1)
                if not last:
                    continue
                handle.seek(-1, 2)
                handle.write(bytes([last[0] ^ 0xFF]))
            changed += 1
        except OSError:
            continue
    return changed


def copy_payload(
    files: list[tuple[str, Path, int]],
    dest_root: Path,
    *,
    do_hash: bool,
) -> dict:
    dest_root.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()
    copyfile_ms = 0.0
    hash_ms = 0.0
    copystat_ms = 0.0
    stat_ms = 0.0
    mkdir_ms = 0.0
    volume_ms = 0.0
    loop_ms = 0.0
    bytes_copied = 0
    traces: list[dict] = []
    mkdir_cache: set[str] = set()
    first_slow_at_ms: float | None = None
    prev_slow_end: float | None = None
    gaps_between_slow: list[float] = []

    for rel, src, _hint in files:
        file_t0 = time.perf_counter()
        dst = dest_root / rel
        parent_key = str(dst.parent)
        this_mkdir = 0.0
        if parent_key not in mkdir_cache:
            t_mk = time.perf_counter()
            dst.parent.mkdir(parents=True, exist_ok=True)
            this_mkdir = (time.perf_counter() - t_mk) * 1000.0
            mkdir_cache.add(parent_key)
        mkdir_ms += this_mkdir

        t_st = time.perf_counter()
        try:
            size = int(src.stat().st_size)
        except OSError:
            size = 0
        this_stat = (time.perf_counter() - t_st) * 1000.0
        stat_ms += this_stat

        src_vol, src_vol_ms = volume_id(src)
        dst_vol, dst_vol_ms = volume_id(dst)
        this_vol = src_vol_ms + dst_vol_ms
        volume_ms += this_vol

        this_hash = 0.0
        digest = hashlib.sha256() if do_hash else None
        t_loop = time.perf_counter()
        with src.open("rb") as inf, dst.open("wb") as outf:
            while True:
                block = inf.read(CHUNK)
                if not block:
                    break
                if digest is not None:
                    t_h = time.perf_counter()
                    digest.update(block)
                    this_hash += (time.perf_counter() - t_h) * 1000.0
                outf.write(block)
        this_loop = (time.perf_counter() - t_loop) * 1000.0
        this_copyfile = max(0.0, this_loop - this_hash)
        loop_ms += this_loop
        copyfile_ms += this_copyfile
        hash_ms += this_hash

        t_cs = time.perf_counter()
        shutil.copystat(src, dst, follow_symlinks=True)
        this_cs = (time.perf_counter() - t_cs) * 1000.0
        copystat_ms += this_cs
        bytes_copied += size

        copy_end = time.perf_counter()
        elapsed = (copy_end - file_t0) * 1000.0
        start_from_run = (file_t0 - t_all) * 1000.0
        rec = {
            "path": rel,
            "size": size,
            "copy_start_ms": round(start_from_run, 3),
            "copy_end_ms": round((copy_end - t_all) * 1000.0, 3),
            "copyfile_ms": round(this_copyfile, 3),
            "hash_ms": round(this_hash, 3),
            "copystat_ms": round(this_cs, 3),
            "mkdir_ms": round(this_mkdir, 3),
            "stat_ms": round(this_stat, 3),
            "volume_ms": round(this_vol, 3),
            "elapsed_ms": round(elapsed, 3),
            "source_volume": src_vol,
            "target_volume": dst_vol,
        }
        traces.append(rec)
        if this_copyfile >= SLOW_FILE_MS or elapsed >= SLOW_FILE_MS:
            if first_slow_at_ms is None:
                first_slow_at_ms = start_from_run
            if prev_slow_end is not None:
                gaps_between_slow.append(start_from_run - prev_slow_end)
            prev_slow_end = rec["copy_end_ms"]

    total_ms = (time.perf_counter() - t_all) * 1000.0
    accounted = copyfile_ms + hash_ms + copystat_ms + stat_ms + mkdir_ms + volume_ms
    top20 = sorted(traces, key=lambda r: r["elapsed_ms"], reverse=True)[:20]
    result = {
        "copyfile_count": len(traces),
        "bytes": bytes_copied,
        "copyfile_ms": round(copyfile_ms, 3),
        "hash_ms": round(hash_ms, 3),
        "copystat_ms": round(copystat_ms, 3),
        "stat_ms": round(stat_ms, 3),
        "mkdir_ms": round(mkdir_ms, 3),
        "volume_ms": round(volume_ms, 3),
        "loop_ms_includes_hash": round(loop_ms, 3),
        "accounted_ms": round(accounted, 3),
        "unaccounted_ms": round(total_ms - accounted, 3),
        "total_ms": round(total_ms, 3),
        "first_slow_file_at_ms": None
        if first_slow_at_ms is None
        else round(first_slow_at_ms, 3),
        "slow_file_gaps_ms": [round(g, 3) for g in gaps_between_slow[:20]],
        "top20_slowest_files": top20,
    }
    if total_ms >= DEEP_MS:
        result["deep_trace"] = True
        result["all_files"] = traces
    return result


def summarize_t5(timing: dict, copy_plan: list[dict] | None) -> dict:
    stages = {s.get("stage"): s for s in (timing.get("stages") or [])}
    ops = {o.get("operation"): o for o in (timing.get("op_profile") or {}).get("operations") or []}
    copyfile_ms = None
    hash_ms = None
    copystat_ms = None
    mkdir_ms = None
    stat_ms = None
    if copy_plan:
        copyfile_ms = sum(float(r.get("read_write_ms") or 0.0) for r in copy_plan)
        hash_ms = sum(float(r.get("hash_ms") or 0.0) for r in copy_plan)
        copystat_ms = sum(float(r.get("copystat_ms") or 0.0) for r in copy_plan)
        mkdir_ms = sum(float(r.get("mkdir_ms") or 0.0) for r in copy_plan)
        stat_ms = sum(float(r.get("stat_ms") or 0.0) for r in copy_plan)
        top20 = sorted(copy_plan, key=lambda r: float(r.get("elapsed_ms") or 0.0), reverse=True)[:20]
    else:
        top20 = []
    total_ms = float(
        timing.get("copy_ms")
        or stages.get("copy", {}).get("elapsed_ms")
        or 0.0
    )
    return {
        "copyfile_count": int((timing.get("files") or 0) or (ops.get("copyfile") or {}).get("count") or 0),
        "bytes": int(timing.get("bytes") or 0),
        "copyfile_ms": None if copyfile_ms is None else round(copyfile_ms, 3),
        "hash_ms": None if hash_ms is None else round(hash_ms, 3),
        "copystat_ms": None if copystat_ms is None else round(copystat_ms, 3),
        "stat_ms": None if stat_ms is None else round(stat_ms, 3),
        "mkdir_ms": None if mkdir_ms is None else round(mkdir_ms, 3),
        "smm_copyfile_op_ms_includes_hash": round(
            float((ops.get("copyfile") or {}).get("total_ms") or 0.0), 3
        ),
        "smm_copy_stage_ms": round(float(stages.get("copy", {}).get("elapsed_ms") or 0.0), 3),
        "smm_total_ms": round(float(timing.get("total_ms") or 0.0), 3),
        "extract_ms": round(float(stages.get("extract", {}).get("elapsed_ms") or 0.0), 3),
        "manifest_ms": round(float(stages.get("manifest", {}).get("elapsed_ms") or 0.0), 3),
        "backup_ms": round(float(stages.get("backup", {}).get("elapsed_ms") or 0.0), 3),
        "total_ms": round(float(stages.get("copy", {}).get("elapsed_ms") or total_ms), 3),
        "top20_slowest_files": [
            {
                "path": r.get("relative") or r.get("source"),
                "size": r.get("size"),
                "copyfile_ms": r.get("read_write_ms"),
                "hash_ms": r.get("hash_ms"),
                "copystat_ms": r.get("copystat_ms"),
                "elapsed_ms": r.get("elapsed_ms"),
            }
            for r in top20
        ],
    }


def print_row(row: dict) -> None:
    print(
        f"{row['test']:<8} #{row['run']:<2} {row['target_state']:<18} "
        f"method={row['method']:<10} n={row['copyfile_count']:<5} "
        f"bytes={row['bytes']:<12} copyfile={row.get('copyfile_ms')} "
        f"copystat={row.get('copystat_ms')} stat={row.get('stat_ms')} "
        f"mkdir={row.get('mkdir_ms')} hash={row.get('hash_ms')} "
        f"total={row.get('total_ms')} prep={row.get('prepare_ms')}"
    )


def run_series(
    *,
    test: str,
    dest: Path,
    files: list[tuple[str, Path, int]],
    states: list[str],
    do_hash: bool,
    method: str,
) -> list[dict]:
    rows: list[dict] = []
    for i, state in enumerate(states, start=1):
        prepare_ms = 0.0
        existed = dest.is_dir()
        if state == "absent":
            prepare_ms = rmtree_timed(dest)
            existed = False
        row_src = {
            "test": test,
            "run": i,
            "target_state": "existing" if existed else "absent",
            "requested_state": state,
            "method": method,
            "target": str(dest),
            "prepare_ms": round(prepare_ms, 3),
        }
        timed = copy_payload(files, dest, do_hash=do_hash)
        row = {**row_src, **timed}
        if timed["total_ms"] >= DEEP_MS:
            dump = OUT_DIR / f"deep_{test}_{i}.json"
            dump.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            row["deep_dump"] = str(dump)
        rows.append(row)
        print_row(row)
        if timed["total_ms"] >= DEEPER_MS:
            print("  DEEP >120s top20:")
            for item in timed.get("top20_slowest_files") or []:
                print(
                    f"    {item['elapsed_ms']:8.1f}ms  copyfile={item['copyfile_ms']}  "
                    f"{item['path']}  size={item['size']}"
                )
    return rows


def run_t5(frozen: str, library: Path) -> list[dict]:
    from services.deploy import ModDeployer

    rows: list[dict] = []
    deployer = ModDeployer(library_root=library)
    plan_src = ROOT / "_tmp" / "copy_plan.json"
    for i in range(1, RUNS + 1):
        t0 = time.perf_counter()
        result = deployer.deploy_mod(frozen)
        wall_ms = (time.perf_counter() - t0) * 1000.0
        timing = result.get("deploy_timing") or {}
        copy_plan = None
        if plan_src.is_file():
            copy_plan = json.loads(plan_src.read_text(encoding="utf-8"))
            dest_plan = OUT_DIR / f"t5_run{i}_copy_plan.json"
            dest_plan.write_text(
                json.dumps(copy_plan, ensure_ascii=False), encoding="utf-8"
            )
        summary = summarize_t5(timing, copy_plan if isinstance(copy_plan, list) else None)
        row = {
            "test": "T5",
            "run": i,
            "target_state": "existing_real",
            "requested_state": "real_smm_target",
            "method": "smm_deploy_mod",
            "target": str(result.get("target") or ""),
            "prepare_ms": 0.0,
            "success": bool(result.get("success")),
            "error": str(result.get("error") or ""),
            "wall_ms": round(wall_ms, 3),
            **summary,
        }
        if float(row.get("total_ms") or 0.0) >= DEEP_MS or wall_ms >= DEEP_MS:
            dump = OUT_DIR / f"deep_T5_{i}.json"
            dump.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            row["deep_dump"] = str(dump)
            row["deep_trace"] = True
        rows.append(row)
        print_row(row)
        print(
            f"         smm_copy_stage={row.get('smm_copy_stage_ms')}  "
            f"smm_total={row.get('smm_total_ms')}  wall={row.get('wall_ms')}  "
            f"extract={row.get('extract_ms')}  manifest={row.get('manifest_ms')}"
        )
    return rows


def main() -> int:
    from core.db_manager import DatabaseManager
    from core.paths import database_path, default_mod_library

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not SOURCE.is_dir():
        raise SystemExit(f"source missing: {SOURCE}")

    db = DatabaseManager.instance(database_path())
    cfg = db.get_game_deploy_config(APP_ID)
    if cfg is None or not str(cfg.mod_path or "").strip():
        raise SystemExit("missing Darkest Dungeon mod_path")
    mods_root = Path(str(cfg.mod_path)).resolve()
    dest_a = mods_root / "_perf_test_A"
    dest_b = mods_root / "_perf_test_B"
    dest_c = mods_root / "_perf_test_C"
    dest_d = mods_root / "_perf_test_D"

    t_overlay = time.perf_counter()
    outer = list_source_files(SOURCE)
    overlay_root = OUT_DIR / "overlay"
    overlay = extract_overlay(SOURCE, overlay_root)
    files = merge_payload(outer, overlay)
    overlay_ms = (time.perf_counter() - t_overlay) * 1000.0
    payload_bytes = sum(size for _rel, _path, size in files)
    header = {
        "source": str(SOURCE),
        "mods_root": str(mods_root),
        "outer_files": len(outer),
        "outer_bytes": sum(s for _r, _p, s in outer),
        "overlay_files": len(overlay),
        "overlay_bytes": sum(s for _r, _p, s in overlay),
        "payload_files": len(files),
        "payload_bytes": payload_bytes,
        "overlay_prepare_ms": round(overlay_ms, 3),
        "note": (
            "copyfile_ms is read+write only. "
            "SMM record_op(copyfile) includes hash inside the same loop; "
            "loop_ms_includes_hash is that equivalent."
        ),
    }
    print(json.dumps(header, ensure_ascii=False, indent=2))
    print()

    all_rows: list[dict] = []
    print("--- T1 absent smm_loop ---")
    all_rows.extend(
        run_series(
            test="T1",
            dest=dest_a,
            files=files,
            states=["absent"] * RUNS,
            do_hash=True,
            method="smm_loop",
        )
    )
    print("--- T2 existing smm_loop ---")
    all_rows.extend(
        run_series(
            test="T2",
            dest=dest_a,
            files=files,
            states=["existing"] * RUNS,
            do_hash=True,
            method="smm_loop",
        )
    )

    print("--- T3 seed + mutate ---")
    rmtree_timed(dest_b)
    seed3 = copy_payload(files, dest_b, do_hash=False)
    mutated = mutate_existing(dest_b, files)
    print(f"T3 seed copyfile_ms={seed3['copyfile_ms']} mutated_files={mutated}")
    print("--- T3 existing different-content smm_loop ---")
    all_rows.extend(
        run_series(
            test="T3",
            dest=dest_b,
            files=files,
            states=["existing"] * RUNS,
            do_hash=True,
            method="smm_loop",
        )
    )

    print("--- T4 seed identical ---")
    rmtree_timed(dest_c)
    seed4 = copy_payload(files, dest_c, do_hash=False)
    print(f"T4 seed copyfile_ms={seed4['copyfile_ms']}")
    print("--- T4 existing same-content smm_loop ---")
    all_rows.extend(
        run_series(
            test="T4",
            dest=dest_c,
            files=files,
            states=["existing"] * RUNS,
            do_hash=True,
            method="smm_loop",
        )
    )

    print("--- T1-min absent independent ---")
    all_rows.extend(
        run_series(
            test="T1-min",
            dest=dest_d,
            files=files,
            states=["absent"] * RUNS,
            do_hash=False,
            method="minimal",
        )
    )
    print("--- T2-min existing independent ---")
    all_rows.extend(
        run_series(
            test="T2-min",
            dest=dest_d,
            files=files,
            states=["existing"] * RUNS,
            do_hash=False,
            method="minimal",
        )
    )

    print("--- T5 real SMM deploy ---")
    from core.mod_platform import PLATFORM_STEAM
    from services.deploy_identity import is_frozen_internal_uuid
    from services.identity_service import resolve_internal_id_from_workspace_id

    frozen = resolve_internal_id_from_workspace_id(
        WS, platform=PLATFORM_STEAM, app_id=APP_ID, db=db
    )
    if not is_frozen_internal_uuid(frozen):
        raise SystemExit(f"no Frozen internal_id for workspace_id={WS}")
    print(f"T5 identity workspace_id={WS} internal_id={frozen} (mod_pk is DAL only)")
    all_rows.extend(run_t5(frozen, Path(default_mod_library())))

    totals = [float(r.get("total_ms") or 0.0) for r in all_rows]
    wall_t5 = [float(r.get("wall_ms") or 0.0) for r in all_rows if r.get("test") == "T5"]
    minute = any(t >= DEEP_MS for t in totals) or any(t >= DEEP_MS for t in wall_t5)

    report = {
        "header": header,
        "rows": all_rows,
        "minute_scale_reproduced": minute,
        "max_total_ms": round(max(totals) if totals else 0.0, 3),
        "max_t5_wall_ms": round(max(wall_t5) if wall_t5 else 0.0, 3),
        "t3_seed": {"copyfile_ms": seed3["copyfile_ms"], "mutated_files": mutated},
        "t4_seed": {"copyfile_ms": seed4["copyfile_ms"]},
    }
    out_path = OUT_DIR / "report.json"
    slim_rows = []
    for row in all_rows:
        slim = dict(row)
        slim.pop("all_files", None)
        slim.pop("top20_slowest_files", None)
        slim_rows.append(slim)
    out_path.write_text(
        json.dumps({**report, "rows": slim_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    full_path = OUT_DIR / "report_full.json"
    full_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"wrote {out_path}")
    print(f"minute_scale_reproduced={minute} max_total_ms={report['max_total_ms']}")

    for leftover in (dest_a, dest_b, dest_c, dest_d):
        try:
            if leftover.exists():
                shutil.rmtree(leftover)
                print(f"removed {leftover}")
        except OSError as exc:
            print(f"cleanup failed {leftover}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
