#!/usr/bin/env python3
"""Read-only Library diagnostics → JSON (Phase 7 Task 5).

Never deletes data. Example::

    python scripts/library_diagnostics.py
    python scripts/library_diagnostics.py --out diagnostics.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.paths import data_dir, default_mod_library  # noqa: E402
from services.library_diagnostics import build_library_diagnostics  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Write JSON to file")
    args = parser.parse_args(argv)

    library_root = Path(args.library) if args.library else Path(default_mod_library())
    data_root = Path(args.data) if args.data else Path(data_dir())
    payload = build_library_diagnostics(library_root, data_root=data_root)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
