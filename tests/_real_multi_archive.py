"""Real multi-mod offline archive smoke (network)."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from services.archive import (
    GLOBAL_ASSET_WORKERS,
    STEAM_ARCHIVE_LIMITER,
)
from services.file_ops import ModFileManager
from services.sync import OFFLINE_ARCHIVE_MOD_WORKERS, ModSyncService, SyncOptions

# Known public Workshop items (diverse games / sizes).
WORKSHOP_IDS = [
    "3761838546",
    "2878328869",
    "2397079060",
    "3081938189",
    "2882834331",
    "2890017294",
]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    STEAM_ARCHIVE_LIMITER.reset()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i, wid in enumerate(WORKSHOP_IDS):
            folder = root / "Game" / f"Mod_{wid}"
            info = folder / ".info"
            info.mkdir(parents=True)
            (info / "mod.json").write_text(
                json.dumps(
                    {
                        "published_file_id": wid,
                        "title": f"Mod_{wid}",
                        "game_name": "Game",
                    }
                ),
                encoding="utf-8",
            )
            (folder / "dummy.pak").write_bytes(b"x")

        svc = ModSyncService(root, root, file_manager=ModFileManager(root))
        print(
            "config",
            {
                "mod_workers": OFFLINE_ARCHIVE_MOD_WORKERS,
                "global_asset_workers": GLOBAL_ASSET_WORKERS,
            },
        )
        t0 = time.perf_counter()
        result = svc.sync_offline_pages_only(
            options=SyncOptions(
                archive_pages=True,
                download_covers=False,
                skip_existing=True,
                overwrite_files=False,
            ),
        )
        elapsed = time.perf_counter() - t0
        print(
            "RESULT",
            {
                "mods": len(WORKSHOP_IDS),
                "success": len(result.success),
                "failed": len(result.failed),
                "rate_limited": len(result.rate_limited),
                "skipped": len(result.skipped),
                "elapsed_sec": round(elapsed, 1),
                "avg_sec": round(elapsed / max(len(WORKSHOP_IDS), 1), 1),
            },
        )
        for meta, err in result.failed[:5]:
            print("fail", getattr(meta, "published_file_id", None), err[:120])
        for meta, err in result.rate_limited[:5]:
            print("429", getattr(meta, "published_file_id", None), err[:80])


if __name__ == "__main__":
    main()
