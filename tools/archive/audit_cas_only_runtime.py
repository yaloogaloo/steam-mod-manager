#!/usr/bin/env python3
"""
CAS_ONLY offline runtime audit.

Scans LIVE Mods for:
  - leftover .info/assets / .info/offline/assets (should be 0 after finalize)
  - missing LIVE manifest when offline index exists
  - Asset Store orphans (objects not referenced by any LIVE/Backup manifest)
  - OPEN readiness (ensure_live → offline_view)

Usage:
  python tools/audit_cas_only_runtime.py
  python tools/audit_cas_only_runtime.py --limit 50 --json-out _tmp/cas_only_audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.cas_runtime import cas_only_info_asset_runtime  # noqa: E402
from core.paths import asset_store_dir  # noqa: E402
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, ManifestError  # noqa: E402
from services.asset_store import AssetStore  # noqa: E402
from services.info_asset_migration import (  # noqa: E402
    discover_info_asset_trees,
    iter_asset_files,
    list_mod_ids_with_paths,
)
from services.info_asset_runtime import (  # noqa: E402
    ensure_live_offline_openable,
    load_usable_live_manifest,
)
from services.offline.paths import resolve_offline_page  # noqa: E402


@dataclass
class ModCasAudit:
    mod_id: str
    managed_path: str = ""
    has_index: bool = False
    index_path: str = ""
    info_asset_files: int = 0
    offline_asset_files: int = 0
    has_manifest: bool = False
    manifest_assets: int = 0
    usable_cas: bool = False
    open_ok: bool = False
    open_path: str = ""
    open_via_offline_view: bool = False
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CasOnlyAuditReport:
    gate_on: bool = True
    mods_scanned: int = 0
    mods_with_index: int = 0
    mods_with_info_assets: int = 0
    mods_with_offline_assets: int = 0
    mods_missing_manifest: int = 0
    mods_open_fail: int = 0
    info_asset_files_total: int = 0
    offline_asset_files_total: int = 0
    store_objects: int = 0
    live_referenced_objects: int = 0
    orphan_objects: int = 0
    orphan_sample: list[str] = field(default_factory=list)
    mods: list[ModCasAudit] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_on": self.gate_on,
            "mods_scanned": self.mods_scanned,
            "mods_with_index": self.mods_with_index,
            "mods_with_info_assets": self.mods_with_info_assets,
            "mods_with_offline_assets": self.mods_with_offline_assets,
            "mods_missing_manifest": self.mods_missing_manifest,
            "mods_open_fail": self.mods_open_fail,
            "info_asset_files_total": self.info_asset_files_total,
            "offline_asset_files_total": self.offline_asset_files_total,
            "store_objects": self.store_objects,
            "live_referenced_objects": self.live_referenced_objects,
            "orphan_objects": self.orphan_objects,
            "orphan_sample": self.orphan_sample,
            "elapsed_s": self.elapsed_s,
            "mods": [m.to_dict() for m in self.mods],
        }


def _count_tree_files(folder: Path) -> tuple[int, int]:
    info_n = 0
    offline_n = 0
    for tree in discover_info_asset_trees(folder):
        n = sum(1 for _ in iter_asset_files(tree.assets_dir))
        if tree.offline_root.name == "offline":
            offline_n += n
        else:
            info_n += n
    return info_n, offline_n


def audit_cas_only_runtime(
    *,
    store: AssetStore | None = None,
    limit: int = 0,
    open_check: bool = True,
) -> CasOnlyAuditReport:
    store = store or AssetStore(root=asset_store_dir())
    report = CasOnlyAuditReport(gate_on=cas_only_info_asset_runtime())
    t0 = time.monotonic()
    pairs = list_mod_ids_with_paths()
    if limit > 0:
        pairs = pairs[: int(limit)]

    referenced: set[str] = set()

    for mid, folder in pairs:
        report.mods_scanned += 1
        row = ModCasAudit(mod_id=mid, managed_path=str(folder))
        index = resolve_offline_page(folder)
        if index is None:
            report.mods.append(row)
            continue
        row.has_index = True
        row.index_path = str(index)
        report.mods_with_index += 1

        info_n, offline_n = _count_tree_files(folder)
        row.info_asset_files = info_n
        row.offline_asset_files = offline_n
        report.info_asset_files_total += info_n
        report.offline_asset_files_total += offline_n
        if info_n:
            report.mods_with_info_assets += 1
            row.issues.append(f"leftover .info/assets files={info_n}")
        if offline_n:
            report.mods_with_offline_assets += 1
            row.issues.append(f"leftover .info/offline/assets files={offline_n}")

        man_path = index.parent / MANIFEST_FILENAME
        row.has_manifest = man_path.is_file()
        if not row.has_manifest:
            report.mods_missing_manifest += 1
            row.issues.append("manifest missing beside index")
        else:
            try:
                man = AssetManifest.from_path(man_path)
                row.manifest_assets = len(man.assets)
                for ref in man.assets:
                    referenced.add(ref.sha256)
            except (OSError, ManifestError) as exc:
                row.issues.append(f"manifest unreadable: {exc}")

        usable = load_usable_live_manifest(index, store=store)
        row.usable_cas = usable is not None
        if usable is not None:
            for ref in usable.assets:
                referenced.add(ref.sha256)

        if open_check:
            opened = ensure_live_offline_openable(folder, store=store)
            row.open_ok = opened is not None
            if opened is not None:
                row.open_path = str(opened)
                row.open_via_offline_view = "offline_view" in str(opened)
                if report.gate_on and not row.open_via_offline_view and (
                    info_n or offline_n or row.usable_cas
                ):
                    # Gate ON with CAS should prefer offline_view; legacy disk-only OK.
                    if row.usable_cas:
                        row.issues.append("OPEN not via offline_view despite usable CAS")
            else:
                report.mods_open_fail += 1
                row.issues.append("OPEN failed")

        report.mods.append(row)

    # Orphan scan: store objects not referenced by any LIVE manifest we saw.
    try:
        all_objs = list(store.iter_objects())
        report.store_objects = len(all_objs)
        report.live_referenced_objects = len(referenced)
        orphans = [o.sha256 for o in all_objs if o.sha256 not in referenced]
        report.orphan_objects = len(orphans)
        report.orphan_sample = orphans[:20]
    except Exception as exc:  # noqa: BLE001
        report.orphan_sample = [f"store scan failed: {exc}"]

    report.elapsed_s = round(time.monotonic() - t0, 3)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CAS_ONLY offline runtime audit")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument("--md-out", type=str, default="")
    args = parser.parse_args(argv)

    report = audit_cas_only_runtime(
        limit=int(args.limit or 0),
        open_check=not bool(args.no_open),
    )
    json_out = (
        Path(args.json_out)
        if args.json_out
        else _REPO / "_tmp" / "cas_only_runtime_audit.json"
    )
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    md_out = (
        Path(args.md_out)
        if args.md_out
        else _REPO / "_tmp" / "cas_only_runtime_audit.md"
    )
    lines = [
        "# CAS_ONLY Runtime Audit Report",
        "",
        f"- gate_on: {report.gate_on}",
        f"- mods_scanned: {report.mods_scanned}",
        f"- mods_with_index: {report.mods_with_index}",
        f"- mods_with_info_assets (leftover): {report.mods_with_info_assets} "
        f"({report.info_asset_files_total} files)",
        f"- mods_with_offline_assets (leftover): {report.mods_with_offline_assets} "
        f"({report.offline_asset_files_total} files)",
        f"- mods_missing_manifest: {report.mods_missing_manifest}",
        f"- mods_open_fail: {report.mods_open_fail}",
        f"- store_objects: {report.store_objects}",
        f"- live_referenced_objects: {report.live_referenced_objects}",
        f"- orphan_objects (vs LIVE manifests): {report.orphan_objects}",
        f"- elapsed_s: {report.elapsed_s}",
        "",
        "## Notes",
        "",
        "- Leftover `.info/assets` after CAS_ONLY finalize should trend toward 0.",
        "- Orphans may include Backup-only objects (expected; not LIVE GC).",
        "- OPEN under gate ON should use `cache/offline_view/` when CAS is usable.",
        "",
    ]
    md_out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {json_out}")
    print(f"wrote {md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
