from __future__ import annotations

import json
from pathlib import Path

from metadata import is_steam_source, normalize_workspace_id, scan_smm_mods


def _write_meta(mod_dir: Path, payload: dict) -> None:
    info = mod_dir / ".info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_steam_source_aliases() -> None:
    assert is_steam_source({"source": "steam"})
    assert is_steam_source({"source_type": "Steam"})
    assert is_steam_source({"platform": "steam"})
    assert not is_steam_source({"source": "nexus"})
    assert not is_steam_source({"source_type": "modio"})
    assert not is_steam_source({"source_type": "other"})
    assert not is_steam_source({})
    assert not is_steam_source({"source_path": r"F:\SteamLibrary\workshop\content\262060\1"})


def test_process_steam_with_workspace_id(tmp_path: Path) -> None:
    smm = tmp_path / "smm"
    _write_meta(
        smm / "MyMod",
        {"source_type": "steam", "platform": "steam", "workspace_id": "2853239091", "title": "MyMod"},
    )
    scanned = scan_smm_mods(smm)
    assert len(scanned) == 1
    assert scanned[0].steam is True
    assert scanned[0].workspace_id == "2853239091"
    assert scanned[0].skip_reason == ""


def test_non_steam_skipped(tmp_path: Path) -> None:
    smm = tmp_path / "smm"
    _write_meta(smm / "NexusMod", {"source": "nexus", "workspace_id": "111", "title": "Nexus"})
    _write_meta(smm / "LocalMod", {"source_type": "other", "title": "Local"})
    scanned = {item.path.name: item for item in scan_smm_mods(smm)}
    assert scanned["NexusMod"].steam is False
    assert "non-steam" in scanned["NexusMod"].skip_reason
    assert scanned["LocalMod"].steam is False


def test_missing_workspace_id(tmp_path: Path) -> None:
    smm = tmp_path / "smm"
    _write_meta(smm / "Broken", {"source_type": "steam", "title": "Broken"})
    scanned = scan_smm_mods(smm)
    assert scanned[0].steam is True
    assert scanned[0].workspace_id is None
    assert "workspace_id" in scanned[0].skip_reason


def test_illegal_workspace_id_rejected() -> None:
    assert normalize_workspace_id("2853239091") == "2853239091"
    assert normalize_workspace_id("3308841144") == "3308841144"
    assert normalize_workspace_id("") is None
    assert normalize_workspace_id("..") is None
    assert normalize_workspace_id("../2853239091") is None
    assert normalize_workspace_id("..\\2853239091") is None
    assert normalize_workspace_id(r"F:\xxx") is None
    assert normalize_workspace_id(r"2853239091\abc") is None
    assert normalize_workspace_id("2853239091/abc") is None
    assert normalize_workspace_id("abc") is None
    assert normalize_workspace_id("12 34") is None
