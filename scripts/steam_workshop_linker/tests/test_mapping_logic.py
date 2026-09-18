from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from helpers import game_pair, make_db, steam_mod
from junction import create_junction, is_junction, lexists
from mapping import (
    _status_for_action,
    build_plan,
    classify_item,
    decide_workshop_action,
    print_summary,
    sync_game,
)

WINDOWS = os.name == "nt"


def _db_for(game, tmp_path: Path, mods: list[tuple[str, Path, str]]) -> Path:
    rows = []
    for workspace_id, path, iid in mods:
        rows.append(
            {
                "app_id": 262060,
                "platform": "steam",
                "workspace_id": workspace_id,
                "internal_id": iid,
                "last_known_path": str(path),
            }
        )
    return make_db(tmp_path / "mod_manager.db", rows)


def test_decide_workshop_action_table() -> None:
    assert decide_workshop_action(exists=False, is_link=False, correct_target=False) == "create"
    assert decide_workshop_action(exists=True, is_link=True, correct_target=True) == "keep"
    assert decide_workshop_action(exists=True, is_link=True, correct_target=False) == "repair"
    assert decide_workshop_action(exists=True, is_link=False, correct_target=False) == "replace"


def test_status_for_action_does_not_fold_replace_into_repair() -> None:
    assert _status_for_action("create") == "REGISTERED + CREATE"
    assert _status_for_action("keep") == "REGISTERED + KEEP"
    assert _status_for_action("repair") == "REGISTERED + REPAIR"
    assert _status_for_action("replace") == "REGISTERED + REPLACE"


def test_replace_ordinary_directory_status_and_counts(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "MyMod", "2853239091", internal_id="iid-x")
    db = _db_for(game, tmp_path, [("2853239091", target, "iid-x")])
    workshop = game.workshop_root / "2853239091"
    workshop.mkdir()
    (workshop / "dup.bin").write_bytes(b"dup")
    action, _old = classify_item(workshop, target)
    assert action == "replace"
    result = sync_game(game, dry_run=True, db_path=db)
    item = next(x for x in result.items if x.workspace_id == "2853239091")
    assert item.action == "replace"
    assert item.status == "REGISTERED + REPLACE"
    assert result.repaired == 0
    assert result.replaced == 1
    assert result.created == 0
    assert result.kept == 0
    assert any("REGISTERED + REPLACE" in line for line in result.logs)
    assert not any("REGISTERED + REPAIR" in line for line in result.logs)


def test_create_missing_workshop_status_and_counts(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "CreateMe", "111", internal_id="iid-111")
    db = _db_for(game, tmp_path, [("111", target, "iid-111")])
    workshop = game.workshop_root / "111"
    action, _old = classify_item(workshop, target)
    assert action == "create"
    result = sync_game(game, dry_run=True, db_path=db)
    item = next(x for x in result.items if x.workspace_id == "111")
    assert item.action == "create"
    assert item.status == "REGISTERED + CREATE"
    assert result.created == 1
    assert result.repaired == 0
    assert result.replaced == 0
    assert result.kept == 0


def test_unregistered_workshop_is_not_modified(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    a = steam_mod(game.smm_mod_root, "A", "111", internal_id="iid-111")
    b = steam_mod(game.smm_mod_root, "B", "222", internal_id="iid-222")
    db = _db_for(game, tmp_path, [("111", a, "iid-111"), ("222", b, "iid-222")])
    for name in ("111", "222", "333"):
        folder = game.workshop_root / name
        folder.mkdir()
        (folder / "payload.bin").write_bytes(name.encode("ascii"))
    before = (game.workshop_root / "333" / "payload.bin").read_bytes()
    result = sync_game(game, dry_run=True, db_path=db)
    assert result.aborted is False
    assert result.unregistered == 1
    assert (game.workshop_root / "333" / "payload.bin").read_bytes() == before
    result = sync_game(game, dry_run=False, db_path=db)
    assert result.errors == 0
    assert (game.workshop_root / "333" / "payload.bin").read_bytes() == before
    assert (game.workshop_root / "333").is_dir()
    assert not is_junction(game.workshop_root / "333")


def test_dry_run_does_not_change_filesystem(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "MyMod", "2853239091", internal_id="iid-x")
    db = _db_for(game, tmp_path, [("2853239091", target, "iid-x")])
    old = game.workshop_root / "2853239091"
    old.mkdir()
    leftover = old / "dup.bin"
    leftover.write_bytes(b"dup")
    orphan = game.workshop_root / "333"
    orphan.mkdir()
    (orphan / "x.bin").write_bytes(b"x")
    result = sync_game(game, dry_run=True, db_path=db)
    assert result.dry_run is True
    assert leftover.is_file()
    assert (target / "content.txt").read_text(encoding="utf-8") == "smm-2853239091"
    assert (orphan / "x.bin").is_file()
    assert not is_junction(old)
    assert any("WOULD DELETE + LINK" in line for line in result.logs)
    assert any("REGISTERED + REPLACE" in line for line in result.logs)
    assert not any("REGISTERED + REPAIR" in line for line in result.logs)
    assert result.replaced == 1
    assert result.repaired == 0


def test_ambiguous_registration_is_not_modified(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "ModA", "111", internal_id="iid-a")
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {"workspace_id": "111", "internal_id": "iid-a", "last_known_path": str(target)},
            {"workspace_id": "111", "internal_id": "iid-b", "last_known_path": str(target)},
        ],
    )
    folder = game.workshop_root / "111"
    folder.mkdir()
    (folder / "payload.bin").write_bytes(b"stay")
    result = sync_game(game, dry_run=False, db_path=db)
    assert any(item.status == "AMBIGUOUS_REGISTRATION" for item in result.items)
    assert (folder / "payload.bin").read_bytes() == b"stay"
    assert not is_junction(folder)


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_create_keep_repair_and_replace(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    created = steam_mod(game.smm_mod_root, "CreateMe", "111", internal_id="iid-111")
    kept = steam_mod(game.smm_mod_root, "KeepMe", "222", internal_id="iid-222")
    repaired = steam_mod(game.smm_mod_root, "RepairMe", "333", internal_id="iid-333")
    replaced = steam_mod(game.smm_mod_root, "ReplaceMe", "444", internal_id="iid-444")
    (replaced / "keep-me.txt").write_text("safe", encoding="utf-8")
    db = _db_for(
        game,
        tmp_path,
        [
            ("111", created, "iid-111"),
            ("222", kept, "iid-222"),
            ("333", repaired, "iid-333"),
            ("444", replaced, "iid-444"),
        ],
    )

    create_junction(game.workshop_root / "222", kept)
    other = game.smm_mod_root / "Other"
    other.mkdir()
    (other / "other.txt").write_text("leave", encoding="utf-8")
    create_junction(game.workshop_root / "333", other)
    real = game.workshop_root / "444"
    real.mkdir()
    (real / "dup.bin").write_bytes(b"workshop-copy")

    plan = build_plan(game, db)
    actions = {item.workspace_id: item.action for item in plan.items if item.action != "skip"}
    statuses = {item.workspace_id: item.status for item in plan.items if item.action != "skip"}
    assert actions["111"] == "create"
    assert actions["222"] == "keep"
    assert actions["333"] == "repair"
    assert actions["444"] == "replace"
    assert statuses["111"] == "REGISTERED + CREATE"
    assert statuses["222"] == "REGISTERED + KEEP"
    assert statuses["333"] == "REGISTERED + REPAIR"
    assert statuses["444"] == "REGISTERED + REPLACE"

    dry = sync_game(game, dry_run=True, db_path=db)
    assert dry.errors == 0
    assert dry.kept == 1
    assert dry.created == 1
    assert dry.repaired == 1
    assert dry.replaced == 1
    keep_item = next(x for x in dry.items if x.workspace_id == "222")
    repair_item = next(x for x in dry.items if x.workspace_id == "333")
    replace_item = next(x for x in dry.items if x.workspace_id == "444")
    create_item = next(x for x in dry.items if x.workspace_id == "111")
    assert keep_item.status == "REGISTERED + KEEP"
    assert repair_item.status == "REGISTERED + REPAIR"
    assert replace_item.status == "REGISTERED + REPLACE"
    assert create_item.status == "REGISTERED + CREATE"
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_summary(dry)
    summary = buf.getvalue()
    assert "KEEP:\n  1" in summary.replace("\r\n", "\n")
    assert "CREATE:\n  1" in summary.replace("\r\n", "\n")
    assert "REPAIR:\n  1" in summary.replace("\r\n", "\n")
    assert "REPLACE:\n  1" in summary.replace("\r\n", "\n")

    result = sync_game(game, dry_run=False, db_path=db)
    assert result.errors == 0
    assert result.created == 1
    assert result.repaired == 1
    assert result.replaced == 1
    assert result.kept == 1
    assert result.dry_run is False
    assert any("[DELETE + LINK] 444" in line for line in result.logs)
    assert any("execute=True" in line for line in result.logs)
    assert any("dry_run=False" in line for line in result.logs)
    assert is_junction(game.workshop_root / "111")
    assert is_junction(game.workshop_root / "222")
    assert is_junction(game.workshop_root / "333")
    assert is_junction(game.workshop_root / "444")
    assert (game.workshop_root / "111" / "content.txt").read_text(encoding="utf-8") == "smm-111"
    assert (created / "content.txt").is_file()
    assert (kept / "content.txt").is_file()
    assert (repaired / "content.txt").is_file()
    assert (other / "other.txt").read_text(encoding="utf-8") == "leave"
    assert (replaced / "keep-me.txt").read_text(encoding="utf-8") == "safe"
    assert not (game.workshop_root / "444" / "dup.bin").exists()
    assert (game.workshop_root / "444" / "keep-me.txt").is_file()


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_remove_junction_does_not_delete_smm_target(tmp_path: Path) -> None:
    from safety import delete_workshop_path

    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "MyMod", "2853239091", internal_id="iid-x")
    link = game.workshop_root / "2853239091"
    create_junction(link, target)
    kind = delete_workshop_path(
        link,
        workshop_root=game.workshop_root,
        smm_target=target,
        smm_mod_root=game.smm_mod_root,
    )
    assert kind == "junction"
    assert not lexists(link)
    assert (target / "content.txt").read_text(encoding="utf-8") == "smm-2853239091"


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_internal_reparse_does_not_delete_external_target(tmp_path: Path) -> None:
    from safety import delete_workshop_path

    game = game_pair(tmp_path)
    smm = steam_mod(game.smm_mod_root, "ModA", "111", internal_id="iid-111")
    external = tmp_path / "external_keep"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("outside", encoding="utf-8")
    workshop_mod = game.workshop_root / "111"
    workshop_mod.mkdir()
    (workshop_mod / "payload.bin").write_bytes(b"x")
    create_junction(workshop_mod / "link", external)
    kind = delete_workshop_path(
        workshop_mod,
        workshop_root=game.workshop_root,
        smm_target=smm,
        smm_mod_root=game.smm_mod_root,
    )
    assert kind == "directory"
    assert not workshop_mod.exists()
    assert marker.read_text(encoding="utf-8") == "outside"
    assert (smm / "content.txt").is_file()
