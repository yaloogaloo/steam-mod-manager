#!/usr/bin/env python3
"""Independent Steam Workshop → SMM Junction mapper.

SMM Mod directories are the only real store. Workshop folders become
Windows Junctions (mklink /J) pointing at those directories.

Registration is a read-only SQLite query of data/mod_manager.db
(platform='steam', app_id, workspace_id). This tool never writes the
database, never imports Steam Mod Manager, and does not run as a daemon.

If Steam later deletes a Workshop Junction, that removes the link only.
The SMM target remains. Steam may then recreate a real Workshop folder;
re-run this tool (default dry-run, then --execute) to delete that copy
and restore the Junction. There is no background monitor.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import ConfigError, GameConfig, load_games, load_mapped_games, save_mapped_game
from database import default_database_path
from mapping import SyncResult, print_summary, sync_game

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GAMES = SCRIPT_DIR / "games.json"
DEFAULT_MAPPED = SCRIPT_DIR / "mapped_games.json"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map Steam Workshop folders to SMM Mod directories with Junctions."
    )
    parser.add_argument(
        "--games-json",
        type=Path,
        default=DEFAULT_GAMES,
        help="Path to games.json",
    )
    parser.add_argument(
        "--mapped-json",
        type=Path,
        default=DEFAULT_MAPPED,
        help="Path to mapped_games.json history file",
    )
    parser.add_argument(
        "--app-id",
        default="",
        help="Select a game by Steam app_id instead of the interactive menu",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Read-only path to data/mod_manager.db (never written)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only (default if --execute is not passed)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Apply deletes and create Junctions. Required for real changes.",
    )
    return parser.parse_args(argv)


def print_menu(games: list[GameConfig], mapped: dict[str, dict]) -> None:
    print("Steam Workshop → SMM Mod Junction 映射工具")
    print()
    print("可同步游戏：")
    print()
    for index, game in enumerate(games, start=1):
        status = "已处理 / 待检查" if game.app_id in mapped else "未处理"
        print(f"[{index}] {game.name}")
        print(f"    App ID: {game.app_id}")
        print(f"    Workshop: {game.workshop_root}")
        print(f"    SMM:      {game.smm_mod_root}")
        print(f"    状态: {status}")
        print()


def select_game(
    games: list[GameConfig],
    mapped: dict[str, dict],
    *,
    app_id: str,
    input_fn=input,
) -> GameConfig | None:
    if app_id.strip():
        wanted = app_id.strip()
        for game in games:
            if game.app_id == wanted:
                return game
        print(f"未知 app_id: {wanted}。未执行任何文件操作。")
        return None

    print_menu(games, mapped)
    upper = len(games)
    while True:
        try:
            raw = str(input_fn(f"请选择游戏 [1-{upper}]: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("已取消。未执行任何文件操作。")
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            print("已取消。未执行任何文件操作。")
            return None
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= upper:
                return games[choice - 1]
        print("无效序号，未执行任何文件操作。请重新选择。")


def main(argv: list[str] | None = None, input_fn=input) -> int:
    configure_stdio()
    args = parse_args(argv)
    dry_run = not args.execute
    print(f"CLI execute = {bool(args.execute)}", flush=True)
    print(f"CLI dry_run = {dry_run}", flush=True)
    try:
        games = load_games(args.games_json)
        mapped = load_mapped_games(args.mapped_json)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 2

    db_path = args.db if args.db is not None else default_database_path()
    game = select_game(games, mapped, app_id=args.app_id, input_fn=input_fn)
    if game is None:
        return 1

    try:
        result: SyncResult = sync_game(game, dry_run=dry_run, db_path=db_path)
    except KeyboardInterrupt:
        print()
        print("[DONE] interrupted")
        return 130

    print_summary(result)
    if result.aborted:
        return 2
    if dry_run:
        return 1 if result.errors else 0
    try:
        save_mapped_game(args.mapped_json, game, mapped)
    except OSError as exc:
        print(f"[ERROR] failed to write mapped_games.json: {exc}")
        return 1
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
