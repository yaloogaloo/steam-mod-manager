"""
Steam Workshop Mod Manager — application entry point.

Default: launch the PySide6 GUI.
CLI subcommands (scan / fetch / sync) remain available for scripting.
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


def launch_gui() -> int:
    from PySide6.QtWidgets import QApplication

    from core.db_manager import get_db
    from ui.main_window import MainWindow
    from ui.styles import APP_STYLE, apply_dark_palette
    from PySide6.QtCore import QTimer

    from ui.startup_lifecycle import (
        dump_startup_surface_audit,
        log_startup,
        mark_qapplication_created,
        reset_startup_timeline,
    )
    from ui.window_chrome import (
        DarkTitleBarFilter,
        TITLE_BAR_STYLE,
        apply_application_icon,
        diagnose_and_bind_win32_taskbar_icon,
        install_syscommand_probe,
    )

    # Ensure SQLite schema exists before any sync / UI lookup
    get_db()

    reset_startup_timeline()
    app = QApplication(sys.argv)
    mark_qapplication_created()
    from core.debug_config import ui_trace_enabled, performance_log_enabled

    def _boot(msg: str) -> None:
        if ui_trace_enabled() or performance_log_enabled():
            print(f"[BOOT] {msg}", flush=True)

    _boot("QApplication ready")
    app.setOrganizationName("SteamModManager")
    app.setApplicationName("WorkshopLibrary")
    app.setStyle("Fusion")
    apply_dark_palette(app)
    log_startup("apply_dark_palette done")
    apply_application_icon(app)
    log_startup("apply_application_icon done")

    if ui_trace_enabled():
        from ui.widget_show_trace import install_widget_show_trace

        install_widget_show_trace(app)
    # App-level sheet so top-level popups (QMenu / QToolTip / combo lists) inherit dark tokens.
    app.setStyleSheet(APP_STYLE + "\n" + TITLE_BAR_STYLE)
    log_startup("app.setStyleSheet done")

    # Dark native titlebars + icon for dialogs / message boxes.
    chrome_filter = DarkTitleBarFilter(app)
    app.installEventFilter(chrome_filter)
    # Keep filter alive for the app lifetime.
    app.setProperty("_dark_titlebar_filter", chrome_filter)
    log_startup("DarkTitleBarFilter installed")

    log_startup("MainWindow construct begin")
    _boot("before MainWindow")
    window = MainWindow()
    _boot("MainWindow created")
    log_startup("MainWindow construct end")
    if ui_trace_enabled():
        install_syscommand_probe(app, window)
    log_startup("about to show()")
    _boot("before show")
    window.show()
    _boot("after show")
    app.processEvents()
    log_startup(
        f"show() returned visible={window.isVisible()} "
        f"size={window.width()}x{window.height()} state={window.windowState()!r}"
    )
    dump_startup_surface_audit(window)
    # Icon diagnose only — must not resize / change flags after show.
    QTimer.singleShot(0, lambda: diagnose_and_bind_win32_taskbar_icon(window))
    return app.exec()


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
    _configure_logging(verbose=verbose)
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main())
