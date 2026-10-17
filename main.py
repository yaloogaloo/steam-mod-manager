"""
Steam Workshop Mod Manager — application entry point.

Default: launch the PySide6 GUI.
CLI subcommands (scan / fetch / sync) remain available for scripting.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _configure_logging(verbose: bool = False, *, crash_hooks: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not crash_hooks:
        import faulthandler
        faulthandler.enable(all_threads=True)
        return
    from services.crash_trace import install_crash_hooks

    install_crash_hooks()
    from services.runtime_identity import log_archive_runtime_identity

    log_archive_runtime_identity(logging.getLogger(__name__), prefix="[RUNTIME]")


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

    parser.add_argument('--read-only', action='store_true', help='Open the WebEngine UI with native read-only data access')
    parser.add_argument('--validation-root', help='Run the WebEngine release candidate against an existing isolated resource root')
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

    sync_cmd = sub.add_parser(
        "sync",
        help="Scan, rename-copy, download covers, and archive offline pages",
    )
    sync_cmd.add_argument("workshop_dir", help="Steam workshop content directory")
    sync_cmd.add_argument("target_dir", help="Managed library (destination) directory")
    sync_cmd.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Do not skip mods already present in the target library",
    )
    sync_cmd.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip offline Workshop page archiving",
    )
    sync_cmd.add_argument(
        "--no-covers",
        action="store_true",
        help="Skip downloading preview covers",
    )
    sync_cmd.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing managed folders / archived pages",
    )
    sync_cmd.add_argument(
        "--flat",
        action="store_true",
        help="Only scan the given workshop directory (no appid recursion)",
    )

    backfill_cmd = sub.add_parser(
        "backfill",
        help="Generate missing .info/index.html fallback pages in a library",
    )
    backfill_cmd.add_argument(
        "target_dir",
        nargs="?",
        default=None,
        help="Managed library directory (default: <project>/mod)",
    )

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


def cmd_sync(args: argparse.Namespace) -> int:
    from services.sync import ModSyncService, SyncOptions

    options = SyncOptions(
        skip_existing=not args.no_skip_existing,
        archive_pages=not args.no_archive,
        download_covers=not args.no_covers,
        overwrite_files=args.overwrite,
        recursive_scan=not args.flat,
    )

    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        total = max(total, 1)
        pct = int(current / total * 100)
        print(f"[{phase} {pct:3d}%] {message}")

    with ModSyncService(args.workshop_dir, args.target_dir) as service:
        result = service.sync(options, on_progress=on_progress)

    print()
    print(
        f"Done. success={len(result.success)} "
        f"skipped={len(result.skipped)} "
        f"failed={len(result.failed)}"
    )
    for meta, err in result.failed:
        mid = meta.published_file_id if meta else "?"
        print(f"  FAIL [{mid}] {err}")
    return 1 if result.failed and not result.success else 0


def cmd_backfill(target_dir: str | None) -> int:
    from core.paths import default_mod_library
    from services.archive import backfill_offline_pages

    root = target_dir or str(default_mod_library())
    created = backfill_offline_pages(root)
    print(f"Backfilled {created} offline page(s) under: {root}")
    return 0


def _notify_already_running() -> None:
    """Tell the user a GUI is already open — no DB, no MainWindow."""
    logging.getLogger(__name__).error("Steam Mod Manager 已经运行")
    import os

    if os.environ.get("SMM_TEST_DB", "").strip():
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        QMessageBox.information(
            None,
            "Steam Mod Manager",
            "Steam Mod Manager 已经运行",
        )
    except Exception:  # noqa: BLE001
        pass


def launch_gui(*, next_frontend: bool = True, read_only: bool = False, validation_root: str | None = None) -> int:
    if validation_root:
        from frontend.web_application import run_validation
        return run_validation(validation_root)
    from services.app_instance import acquire_instance_lock

    if not acquire_instance_lock():
        _notify_already_running()
        return 1
    if next_frontend:
        from services.app_instance import release_instance_lock
        try:
            return _run_gui(next_frontend=True, read_only=read_only)
        finally:
            release_instance_lock()
    return _run_gui()


def _run_gui(*, next_frontend: bool = True, read_only: bool = False) -> int:
    if next_frontend:
        # Production opt-in stays at the canonical application entry boundary.
        from frontend.admission import require_production_admission
        require_production_admission(read_only=read_only)
        from services.mutation_context import activate_production_context, reset_mutation_context
        from frontend.web_application import run
        token = None if read_only else activate_production_context()
        try:
            return run(read_only=read_only)
        finally:
            if token is not None:
                reset_mutation_context(token)

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    cli_commands = {"scan", "fetch", "sync", "backfill"}
    if argv and argv[0] in cli_commands:
        args = parser.parse_args(argv)
        _configure_logging(verbose=args.verbose)

        if args.command == "scan":
            return cmd_scan(args.workshop_dir, recursive=not args.flat)
        if args.command == "fetch":
            return cmd_fetch(args.ids)
        if args.command == "sync":
            return cmd_sync(args)
        if args.command == "backfill":
            return cmd_backfill(args.target_dir)

    if argv and argv[0] in {"-h", "--help"}:
        parser.print_help()
        print("\nWithout a subcommand, the graphical interface is launched.")
        return 0

    verbose = "-v" in argv or "--verbose" in argv
    gui, _qt_arguments = parser.parse_known_args(argv)
    if gui.read_only or gui.validation_root:sys.dont_write_bytecode = True
    _configure_logging(verbose=verbose, crash_hooks=not (gui.read_only or gui.validation_root))
    if gui.read_only and gui.validation_root:
        parser.error('--read-only and --validation-root cannot be combined')
    return launch_gui(next_frontend=True, read_only=gui.read_only,
                      validation_root=gui.validation_root)


if __name__ == "__main__":
    sys.exit(main())
