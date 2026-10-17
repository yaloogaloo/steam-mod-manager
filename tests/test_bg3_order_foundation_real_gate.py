"""Read-only real BG3 pak / modsettings gate. Never writes the live profile."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from services.bg3_modsettings import (
    GUSTAVX_UUID,
    parse_bg3_modsettings,
    project_bg3_modsettings,
)
from services.bg3_pak import resolve_bg3_mod_metadata

LIVE_MODS = Path(r"F:\SteamLibrary\steamapps\common\Baldurs Gate 3\Mods")
LIVE_LSX = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Larian Studios"
    / "Baldur's Gate 3"
    / "PlayerProfiles"
    / "Public"
    / "modsettings.lsx"
)

REAL_PAKS = (
    "NoIntro_7fa55404-7280-11ed-4c26-63116a161862.pak",
    "AppearanceEditEnhanced.pak",
    "5eSpells.pak",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.skipif(not LIVE_LSX.is_file(), reason="live BG3 modsettings.lsx is not present")
@pytest.mark.skipif(not LIVE_MODS.is_dir(), reason="live BG3 Mods directory is not present")
def test_real_readonly_resolver_and_projector_gate(tmp_path: Path) -> None:
    before_stat = LIVE_LSX.stat()
    before_sha = _sha256(LIVE_LSX)

    resolved = []
    for name in REAL_PAKS:
        pak = LIVE_MODS / name
        if not pak.is_file():
            pytest.skip(f"live pak missing: {name}")
        meta = resolve_bg3_mod_metadata(pak)
        assert meta.uuid, name
        assert "-" in meta.uuid
        resolved.append(meta)

    live_text = LIVE_LSX.read_text(encoding="utf-8")
    _, live_nodes = parse_bg3_modsettings(live_text)
    live_uuids = [node.uuid for node in live_nodes]
    assert live_nodes[0].uuid == GUSTAVX_UUID
    for meta in resolved:
        assert meta.uuid in live_uuids, meta.pak_path

    copy = tmp_path / "modsettings.lsx.copy"
    copy.write_text(live_text, encoding="utf-8")
    # Reverse current managed subsequence so the projector must move slots.
    current = [uuid for uuid in live_uuids if uuid in {m.uuid for m in resolved}]
    order = list(reversed(current))
    assert order != current

    result = project_bg3_modsettings(
        copy,
        tmp_path / "projected.lsx",
        managed_uuid_order=order,
        dry_run=False,
    )
    _, out_nodes = parse_bg3_modsettings(result.output_text)
    assert out_nodes[0].uuid == GUSTAVX_UUID
    assert "ModOrder" not in result.output_text
    got_managed = [n.uuid for n in out_nodes if n.uuid in set(order)]
    assert got_managed == order
    live_unmanaged = [n.uuid for n in live_nodes[1:] if n.uuid not in set(order)]
    out_unmanaged = [n.uuid for n in out_nodes[1:] if n.uuid not in set(order)]
    assert out_unmanaged == live_unmanaged
    assert live_unmanaged.count("396c5966-09b0-40a1-af3f-93a5e9ce71c0") == out_unmanaged.count(
        "396c5966-09b0-40a1-af3f-93a5e9ce71c0"
    )

    after_stat = LIVE_LSX.stat()
    after_sha = _sha256(LIVE_LSX)
    assert after_sha == before_sha
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert LIVE_LSX.read_text(encoding="utf-8") == live_text
