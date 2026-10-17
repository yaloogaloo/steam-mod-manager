"""Cover / metadata / backup / deploy writes require an explicit context."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.importers.image_picker import apply_cover_to_mod
from services.mutation_context import (
    MutationBoundaryError,
    activate_production_context,
    reset_mutation_context,
    suspend_mutation_context,
)
from tests.diagnostics.run_transient_popup_forensic import (
    require_temporary_roots,
)


def _png(folder: Path) -> Path:
    path = folder / "incoming.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-cover")
    return path


def test_context_allows_temp_root(tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "TempMod"
    folder.mkdir(parents=True)
    rel = apply_cover_to_mod(
        folder, _png(tmp_path), update_db=False, sync_backup=False
    )
    assert rel.endswith("cover.png")
    assert (folder / ".info" / "cover.png").is_file()


def test_context_requires_explicit_target(tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "NoContext"
    folder.mkdir(parents=True)
    token = suspend_mutation_context()
    try:
        with pytest.raises(MutationBoundaryError):
            apply_cover_to_mod(
                folder, _png(tmp_path), update_db=False, sync_backup=False
            )
    finally:
        reset_mutation_context(token)
    assert not (folder / ".info" / "cover.png").exists()


def test_production_target_is_not_default(tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "NeedsOptIn"
    folder.mkdir(parents=True)
    suspended = suspend_mutation_context()
    try:
        with pytest.raises(MutationBoundaryError):
            apply_cover_to_mod(
                folder, _png(tmp_path), update_db=False, sync_backup=False
            )
    finally:
        reset_mutation_context(suspended)
    with pytest.raises(MutationBoundaryError):
        activate_production_context()
    assert not (folder / ".info" / "cover.png").exists()


def test_outside_temp_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "smm_outside_mutation_probe"
    outside.mkdir(parents=True, exist_ok=True)
    folder = outside / "Game" / "NotAFixture"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with pytest.raises(MutationBoundaryError):
            apply_cover_to_mod(
                folder, _png(tmp_path), update_db=False, sync_backup=False
            )
        assert not (folder / ".info" / "cover.png").exists()
    finally:
        import shutil

        shutil.rmtree(outside, ignore_errors=True)


def test_diagnostic_cannot_fallback_to_real_mod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORENSIC_FIXTURE_ROOT", raising=False)
    monkeypatch.delenv("FORENSIC_DATA_ROOT", raising=False)
    with pytest.raises(MutationBoundaryError):
        require_temporary_roots()
    source = Path("tests/diagnostics/run_transient_popup_forensic.py").read_text(
        encoding="utf-8"
    )
    assert "_pick_steam_card" not in source
    assert "default_mod_library()" not in source
