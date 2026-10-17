"""Backup retention contract (warning-level) — no auto-purge; size signals.

Does not delete backups. Oversized entries emit warnings only.
Size-warning behaviour is asserted on a synthetic tree (fast, isolated).
Production ``data/mod_backup`` gets a shallow, time-budgeted smoke sample.
"""

from __future__ import annotations

import ast
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = ROOT / "data" / "mod_backup"

# Warning threshold per ``mod_backup/<id>/`` tree (offline HTML trees can grow).
OVERSIZE_BYTES = 25 * 1024 * 1024
# Warning threshold for the whole backup tree.
OVERSIZE_TOTAL_BYTES = 3 * 1024 * 1024 * 1024
_SIZE_SCAN_BUDGET_S = 2.0
_PROD_MAX_BUCKETS = 40

# Modules that may touch backup trees (snapshot sync only).
_BACKUP_MODULES = (
    ROOT / "services" / "metadata_backup.py",
    ROOT / "services" / "metadata_backup_sync.py",
)


def _dir_size_bytes(path: Path, *, deadline: float) -> int:
    total = 0
    for file_path in path.rglob("*"):
        if time.monotonic() >= deadline:
            break
        if not file_path.is_file():
            continue
        try:
            total += int(file_path.stat().st_size)
        except OSError:
            continue
    return total


def _emit_oversize_warnings(
    root: Path,
    *,
    oversize_bytes: int,
    oversize_total_bytes: int,
    deadline: float,
    max_buckets: int | None = None,
) -> list[warnings.WarningMessage]:
    """Shared warning emitter used by synthetic + production smoke scans."""
    oversized: list[tuple[str, int]] = []
    total = 0
    buckets = 0
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        for child in root.iterdir():
            if time.monotonic() >= deadline:
                break
            if max_buckets is not None and buckets >= max_buckets:
                break
            if not child.is_dir():
                continue
            buckets += 1
            size = _dir_size_bytes(child, deadline=deadline)
            total += size
            if size >= oversize_bytes:
                oversized.append((child.name, size))

        oversized.sort(key=lambda item: item[1], reverse=True)
        for mid, size in oversized[:20]:
            warnings.warn(
                f"oversized mod_backup entry {mid}: {size / (1024 * 1024):.1f} MiB "
                f"(threshold {oversize_bytes // (1024 * 1024)} MiB); "
                "no automatic deletion — operator review only",
                UserWarning,
                stacklevel=1,
            )

        if total >= oversize_total_bytes:
            warnings.warn(
                f"mod_backup total size {total / (1024 ** 3):.2f} GiB exceeds "
                f"{oversize_total_bytes // (1024 ** 3)} GiB warning threshold; "
                "no automatic deletion — operator review only",
                UserWarning,
                stacklevel=1,
            )
    return list(caught)


def test_no_automatic_backup_bucket_deletion_in_production_source() -> None:
    """
    Retention policy: never auto-delete ``data/mod_backup/<id>/`` buckets.

    Snapshot mirroring may clear cover/offline *contents* when the live
    ``.info`` side loses them; wholesale bucket purge is forbidden.
    """
    forbidden_snippets = (
        "rmtree(backup_root",
        "rmtree(BACKUP_ROOT",
        "shutil.rmtree(backup_root",
        "purge_mod_backup",
        "delete_mod_backup",
        "retention_delete",
        "cleanup_old_backups",
    )
    offenders: list[str] = []
    for module in _BACKUP_MODULES:
        if not module.is_file():
            continue
        text = module.read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{module.name}:{snippet}")

    for module in _BACKUP_MODULES:
        if not module.is_file():
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name != "rmtree":
                continue
            if not node.args:
                continue
            arg_src = ast.unparse(node.args[0])
            if "backup_root" in arg_src or "BACKUP_ROOT" in arg_src:
                offenders.append(f"{module.name}:rmtree({arg_src})")

    assert not offenders, (
        "automatic deletion of mod_backup buckets is forbidden; "
        f"offenders: {offenders}"
    )


def test_oversized_backup_entries_emit_warnings(tmp_path: Path) -> None:
    """Synthetic oversize detection — asserts warnings without production IO."""
    root = tmp_path / "mod_backup"
    small = root / "1"
    small.mkdir(parents=True)
    (small / "metadata.json").write_text("{}", encoding="utf-8")
    big = root / "2"
    big.mkdir(parents=True)
    (big / "blob.bin").write_bytes(b"x" * 64)

    # Tiny thresholds so the synthetic tree exercises the warning path quickly.
    caught = _emit_oversize_warnings(
        root,
        oversize_bytes=32,
        oversize_total_bytes=48,
        deadline=time.monotonic() + 2.0,
    )
    texts = [str(w.message) for w in caught]
    assert any("oversized mod_backup entry 2" in t for t in texts)
    assert any("mod_backup total size" in t for t in texts)
    assert all("no automatic deletion" in t for t in texts)


def test_production_backup_size_smoke_sample() -> None:
    """Bounded production smoke — never walk the full multi-GiB tree."""
    if not BACKUP_ROOT.is_dir():
        return
    deadline = time.monotonic() + _SIZE_SCAN_BUDGET_S
    # Smoke only: exercise the walker; do not fail the suite on operator warnings.
    _emit_oversize_warnings(
        BACKUP_ROOT,
        oversize_bytes=OVERSIZE_BYTES,
        oversize_total_bytes=OVERSIZE_TOTAL_BYTES,
        deadline=deadline,
        max_buckets=_PROD_MAX_BUCKETS,
    )
    assert True
