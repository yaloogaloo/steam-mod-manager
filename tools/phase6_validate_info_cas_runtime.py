"""Phase 6 production validation — 10 Mods, no bulk .info/assets delete."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from core.cas_runtime import cas_only_info_asset_runtime, reset_cas_runtime_cache
from core.paths import asset_store_dir, database_path, offline_view_cache_dir, project_root
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, verify_manifest_against_store
from services.asset_store import AssetStore, sha256_file
from services.backup_asset_migration import repair_info_assets_from_backup_store
from services.info_asset_migration import discover_info_asset_trees, iter_asset_files
from services.info_asset_runtime import ensure_live_offline_openable
from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root
from services.offline.backup_closure import (
    ensure_backup_offline_openable,
    snapshot_offline_closure,
)
from services.offline.paths import resolve_offline_page

OUT = project_root() / "_tmp" / "phase6_validation.json"


def _count_info_assets(folder: Path) -> tuple[int, int]:
    files = 0
    nbytes = 0
    for tree in discover_info_asset_trees(folder):
        for p in iter_asset_files(tree.assets_dir):
            try:
                nbytes += int(p.stat().st_size)
                files += 1
            except OSError:
                pass
    return files, nbytes


def _pick_mods(limit: int = 10) -> list[dict]:
    """Pick offline Mods; ensure LIVE manifest+CAS via migrate (assets kept)."""
    from services.info_asset_migration import migrate_info_assets

    conn = sqlite3.connect(f"file:{database_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT mod_id, title, last_known_path
        FROM mods
        WHERE last_known_path IS NOT NULL AND TRIM(last_known_path) != ''
        ORDER BY mod_id
        """
    ).fetchall()
    conn.close()

    store = AssetStore(root=asset_store_dir())
    candidates: list[dict] = []

    for row in rows:
        if len(candidates) >= max(40, limit * 4):
            break
        folder = Path(str(row["last_known_path"]))
        if not folder.is_dir():
            continue
        index = resolve_offline_page(folder)
        if index is None:
            continue
        man_path = index.parent / MANIFEST_FILENAME
        # Ensure manifest+CAS without clearing legacy assets (Phase 6 debt).
        need_mig = not man_path.is_file()
        if man_path.is_file():
            try:
                need_mig = bool(
                    verify_manifest_against_store(
                        AssetManifest.from_path(man_path), store
                    )
                )
            except Exception:
                need_mig = True
        if need_mig:
            mig = migrate_info_assets(
                folder, store=store, dry_run=False, mod_id=str(row["mod_id"])
            )
            if not mig.ok and not man_path.is_file():
                continue
        if not man_path.is_file():
            continue
        try:
            man = AssetManifest.from_path(man_path)
        except Exception:
            continue
        if verify_manifest_against_store(man, store):
            continue
        n_files, n_bytes = _count_info_assets(folder)
        bak = backup_root(str(row["mod_id"])) / BACKUP_OFFLINE_DIR
        has_bak = (bak / "index.html").is_file() and (bak / MANIFEST_FILENAME).is_file()
        candidates.append(
            {
                "mod_id": str(row["mod_id"]),
                "title": str(row["title"] or "")[:80],
                "path": str(folder),
                "manifest_assets": len(man.assets),
                "info_assets_files": n_files,
                "info_assets_bytes": n_bytes,
                "has_backup": has_bak,
            }
        )

    picked: list[dict] = []
    seen: set[str] = set()

    def take(pred, n: int) -> None:
        for item in candidates:
            if sum(1 for p in picked if pred(p)) >= n:
                return
            if item["mod_id"] in seen or not pred(item):
                continue
            picked.append(item)
            seen.add(item["mod_id"])
            if len(picked) >= limit:
                return

    take(lambda i: i["info_assets_files"] == 0, 2)
    take(lambda i: i["manifest_assets"] >= 200, 3)
    take(lambda i: 0 < i["manifest_assets"] < 100, 3)
    take(lambda i: True, limit)
    return picked[:limit]


def main() -> None:
    reset_cas_runtime_cache()
    assert cas_only_info_asset_runtime() is True

    store = AssetStore(root=asset_store_dir())
    mods = _pick_mods(10)
    results = []
    new_info_assets_generated = 0
    open_ok = 0
    repair_ok = 0
    repair_fail_cas = 0
    miss_ok = 0
    snapshot_assets_created = 0

    for item in mods:
        folder = Path(item["path"])
        mid = item["mod_id"]
        before_f, before_b = _count_info_assets(folder)
        index = resolve_offline_page(folder)
        assert index is not None

        opened = ensure_live_offline_openable(folder, store=store)
        open_pass = opened is not None and opened.is_file()
        open_is_view = open_pass and "offline_view" in str(opened)
        after_open_f, after_open_b = _count_info_assets(folder)
        if after_open_f > before_f:
            new_info_assets_generated += after_open_f - before_f

        # Backup snapshot must not recreate Backup offline/assets; LIVE assets count unchanged
        bak = backup_root(mid) / BACKUP_OFFLINE_DIR
        snap_ok = False
        bak_assets_after = 0
        if index.is_file():
            snap_ok = bool(snapshot_offline_closure(index, bak))
            if (bak / "assets").exists():
                bak_assets_after = sum(1 for _ in (bak / "assets").rglob("*") if _.is_file())
                snapshot_assets_created += bak_assets_after

        # Repair: must not grow .info/assets
        repair = repair_info_assets_from_backup_store(folder, mod_id=mid, store=store)
        after_repair_f, after_repair_b = _count_info_assets(folder)
        if after_repair_f > before_f:
            new_info_assets_generated += after_repair_f - before_f
        if repair.ok:
            repair_ok += 1
        elif "CAS" in (repair.reason or "") or any(
            "missing" in i.lower() for i in repair.issues
        ):
            repair_fail_cas += 1

        if open_pass:
            open_ok += 1

        row = {
            **item,
            "before_info_assets": before_f,
            "before_info_bytes": before_b,
            "open_ok": open_pass,
            "open_path": str(opened) if opened else None,
            "open_is_offline_view": open_is_view,
            "after_open_info_assets": after_open_f,
            "snapshot_ok": snap_ok,
            "backup_assets_after_snapshot": bak_assets_after,
            "repair_ok": repair.ok,
            "repair_reason": repair.reason,
            "after_repair_info_assets": after_repair_f,
            "info_assets_grew": after_repair_f > before_f or after_open_f > before_f,
        }
        results.append(row)

    # Dedicated MISS case: temporarily stash .info/assets on first mod that has them
    miss = {"attempted": False, "ok": False, "detail": ""}
    for row in results:
        folder = Path(row["path"])
        trees = discover_info_asset_trees(folder)
        if not trees:
            continue
        assets_dir = trees[0].assets_dir
        if not assets_dir.is_dir() or not any(iter_asset_files(assets_dir)):
            continue
        miss["attempted"] = True
        stash = Path(tempfile.mkdtemp(prefix="phase6_miss_")) / "assets"
        before = _count_info_assets(folder)
        shutil.move(str(assets_dir), str(stash))
        try:
            repair = repair_info_assets_from_backup_store(
                folder, mod_id=row["mod_id"], store=store
            )
            after = _count_info_assets(folder)
            opened = ensure_live_offline_openable(folder, store=store)
            bak_open = ensure_backup_offline_openable(
                backup_root(row["mod_id"]) / BACKUP_OFFLINE_DIR
            )
            miss["ok"] = bool(
                repair.ok
                and after[0] == 0
                and opened is not None
                and (bak_open is not None or opened is not None)
            )
            miss["detail"] = {
                "mod_id": row["mod_id"],
                "before": before,
                "after_repair_info_assets": after,
                "repair_ok": repair.ok,
                "repair_reason": repair.reason,
                "open_ok": opened is not None,
                "open_is_view": opened is not None and "offline_view" in str(opened),
            }
            if miss["ok"]:
                miss_ok += 1
        finally:
            # Restore legacy assets (Phase 6 does not bulk-delete)
            if stash.exists() and not assets_dir.exists():
                assets_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stash), str(assets_dir))
            elif stash.exists():
                shutil.rmtree(stash, ignore_errors=True)
        break

    # Sample legacy remaining debt across library (count only, no delete)
    sample_files = 0
    sample_bytes = 0
    sample_mods = 0
    conn = sqlite3.connect(f"file:{database_path()}?mode=ro", uri=True)
    paths = [
        Path(r[0])
        for r in conn.execute(
            "SELECT last_known_path FROM mods WHERE last_known_path IS NOT NULL"
        ).fetchall()
        if r[0]
    ]
    conn.close()
    for folder in paths:
        if not folder.is_dir():
            continue
        f, b = _count_info_assets(folder)
        if f:
            sample_mods += 1
            sample_files += f
            sample_bytes += b

    payload = {
        "gate": "CAS_ONLY_INFO_ASSET_RUNTIME",
        "gate_enabled": True,
        "mods_tested": len(results),
        "open_ok": open_ok,
        "repair_ok": repair_ok,
        "repair_fail_cas_reported": repair_fail_cas,
        "miss": miss,
        "miss_ok": miss_ok,
        "new_info_assets_generated": new_info_assets_generated,
        "backup_asset_files_created_by_snapshot": snapshot_assets_created,
        "legacy_info_assets_remaining": {
            "mods_with_assets": sample_mods,
            "files": sample_files,
            "bytes": sample_bytes,
        },
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in payload if k != "results"}, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
