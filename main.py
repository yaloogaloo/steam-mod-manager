"""
Steam Workshop Mod Manager — application entry point.

Steps 1–2: project bootstrap + Steam metadata core.
GUI (Step 4) will replace the CLI smoke-test path below.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steam-mod-manager",
        description="Backup, organize, and visualize downloaded Steam Workshop mods.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    sub = parser.add_subparsers(dest="command")

    scan_cmd = sub.add_parser("scan", help="Scan a Steam workshop directory for Mod IDs")
    scan_cmd.add_argument("workshop_dir", help="Path to Steam workshop content directory")
    scan_cmd.add_argument(
        "--flat",
        action="store_true",
        help="Only scan the given directory (do not recurse into appid folders)",
    )

    fetch_cmd = sub.add_parser("fetch", help="Fetch Steam metadata for one or more Mod IDs")
    fetch_cmd.add_argument("ids", nargs="+", help="Published file ID(s)")

    return parser


def cmd_scan(workshop_dir: str, *, recursive: bool) -> int:
    from core.scanner import WorkshopScanner

    scanner = WorkshopScanner(workshop_dir)
    mods = scanner.scan(recursive=recursive)
    print(f"Found {len(mods)} mod folder(s) under: {scanner.workshop_root}")
    for mod in mods:
        print(f"  {mod.published_file_id}  ->  {mod.path}")
    return 0


def cmd_fetch(ids: list[str]) -> int:
    from core.steam_api import SteamWorkshopClient

    with SteamWorkshopClient() as client:
        metas = client.get_details_batch(ids)

    for meta in metas:
        if meta.fetch_error:
            print(f"[{meta.published_file_id}] ERROR: {meta.fetch_error}")
            continue
        print(f"[{meta.published_file_id}] {meta.title}")
        print(f"  preview : {meta.preview_url or '(none)'}")
        print(f"  app_id  : {meta.app_id}")
        print(f"  size    : {meta.file_size} bytes")
        print(f"  url     : {meta.workshop_url}")
        if meta.tags:
            print(f"  tags    : {', '.join(meta.tags)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(verbose=getattr(args, "verbose", False))

    if args.command == "scan":
        return cmd_scan(args.workshop_dir, recursive=not args.flat)
    if args.command == "fetch":
        return cmd_fetch(args.ids)

    # No subcommand: later this launches the GUI.
    # For now, print a short status so the entry point is runnable.
    print("Steam Workshop Mod Manager")
    print("Core modules ready. GUI will be wired in Step 4.")
    print()
    print("Try:")
    print("  python main.py scan <steam_workshop_content_dir>")
    print("  python main.py fetch <mod_id> [<mod_id> ...]")
    print()
    print("Install deps:  pip install -r requirements.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
