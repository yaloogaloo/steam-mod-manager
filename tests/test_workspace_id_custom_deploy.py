"""Workspace ID assignment and custom deploy path."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    resolve_workspace_id,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "ws.db")
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    yield manager
    DatabaseManager.reset_instance()


def test_resolve_workspace_id_rules() -> None:
    assert (
        resolve_workspace_id(PLATFORM_STEAM, external_id="3761838546") == "3761838546"
    )
    assert resolve_workspace_id(PLATFORM_STEAM, mod_id="3761838546") == ""
    assert (
        resolve_workspace_id(
            PLATFORM_NEXUS,
            source_url="https://www.nexusmods.com/palworld/mods/336",
        )
        == "336"
    )
    assert resolve_workspace_id(PLATFORM_GITHUB, mod_id="1") == ""
    assert resolve_workspace_id(PLATFORM_OTHER, mod_id="1") == ""
    assert (
        resolve_workspace_id(
            PLATFORM_STEAM, mod_id="1", existing="keep-me"
        )
        == "keep-me"
    )


def test_steam_upsert_sets_workspace_id(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="4242", title="Steam Mod", app_id=100))
    info = db.get_mod_display_info(4242)
    assert info is not None
    assert info.workspace_id == "4242"


def test_nexus_register_workspace_from_url(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="999",
        source_url="https://www.nexusmods.com/game/mods/999",
        title="Nexus Mod",
        app_id=100,
        game_name="SomeGame",
    )
    assert info.workspace_id == "999"


def test_correct_nexus_workspace_id_from_url(db: DatabaseManager) -> None:
    from core.mod_platform import corrected_nexus_workspace_id

    assert (
        corrected_nexus_workspace_id(
            platform=PLATFORM_NEXUS,
            source_url="https://www.nexusmods.com/stardewvalley/mods/2400",
            workspace_id="17863499569189047",
        )
        == "2400"
    )
    assert (
        corrected_nexus_workspace_id(
            platform=PLATFORM_NEXUS,
            source_url="https://www.nexusmods.com/stardewvalley/mods/2400",
            workspace_id="2400",
        )
        is None
    )
    assert (
        corrected_nexus_workspace_id(
            platform=PLATFORM_GITHUB,
            source_url="https://www.nexusmods.com/stardewvalley/mods/2400",
            workspace_id="1",
        )
        is None
    )

    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="rand-ext",
        source_url="https://www.nexusmods.com/stardewvalley/mods/2400",
        title="Calcifer",
        app_id=100,
        game_name="SomeGame",
    )
    # Force a wrong/random workspace_id as batch import may have done.
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "UPDATE mods SET workspace_id = ? WHERE mod_id = ?",
            ("17863499569189047", int(info.mod_id)),
        )
        db._conn.commit()  # noqa: SLF001

    corrected = db.correct_nexus_workspace_id_from_url(info.mod_id)
    assert corrected == "2400"
    again = db.get_mod_display_info(info.mod_id)
    assert again is not None
    assert again.workspace_id == "2400"
    assert db.correct_nexus_workspace_id_from_url(info.mod_id) is None


def test_batch_refresh_nexus_silent_workspace_wash(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nexus-only multi-select refresh must not block; wash ids inside the loop."""
    from services.metadata_refresh import refresh_selected_mods_metadata

    lib = tmp_path / "library"
    entries: list[tuple[str, Path, str]] = []
    for nid, bad_wid in (("2400", "1111111111111"), ("34535", "2222222222222")):
        info = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id=f"ext-{nid}",
            source_url=f"https://www.nexusmods.com/stardewvalley/mods/{nid}",
            title=f"Mod {nid}",
            app_id=100,
            game_name="SomeGame",
        )
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                "UPDATE mods SET workspace_id = ? WHERE mod_id = ?",
                (bad_wid, int(info.mod_id)),
            )
            db._conn.commit()  # noqa: SLF001
        folder = lib / "SomeGame" / f"Mod{nid}"
        folder.mkdir(parents=True)
        (folder / "mod.txt").write_text("x", encoding="utf-8")
        entries.append((str(info.mod_id), folder, PLATFORM_NEXUS))

    monkeypatch.setattr(
        "services.info_sidecar.rescan_mod_folder",
        lambda *a, **k: None,
    )

    progress: list[tuple[int, int]] = []
    results = refresh_selected_mods_metadata(
        entries,
        library_root=lib,
        on_progress=lambda d, t, _m: progress.append((d, t)),
    )
    assert len(results) == 2
    assert all(r.success for r in results)
    assert progress[-1] == (2, 2)

    for mid, _path, _plat in entries:
        info = db.get_mod_display_info(mid)
        assert info is not None
        # URL trailing digits must replace the random workspace_id.
        assert info.workspace_id in {"2400", "34535"}


def test_github_gets_generated_numeric_workspace(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="GH",
        app_id=100,
        game_name="SomeGame",
    )
    assert info.workspace_id
    assert info.workspace_id.isdigit()
    assert info.workspace_id != info.mod_id


def test_custom_deploy_path_copies_contents_not_shell(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    managed = library / "SomeGame" / "SpecialMod"
    managed.mkdir(parents=True)
    (managed / "payload.txt").write_text("hello", encoding="utf-8")
    nested = managed / "sub"
    nested.mkdir()
    (nested / "inner.bin").write_text("bin", encoding="utf-8")
    (managed / INFO_DIR_NAME).mkdir()
    (managed / INFO_DIR_NAME / "mod.json").write_text(
        '{"published_file_id":"88001","title":"SpecialMod","app_id":100}',
        encoding="utf-8",
    )

    db.upsert_mod(
        ModMetadata(published_file_id="88001", title="SpecialMod", app_id=100)
    )
    custom = tmp_path / "game_root" / "custom_target"
    custom.mkdir(parents=True)
    db.update_mod_user_metadata(
        88001,
        {
            "display_name": "SpecialMod",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": str(custom),
        },
    )

    # Intentionally leave game mod_path empty — custom path must still deploy.
    db.update_game_deploy_config(100, name="SomeGame", mod_path="")

    result = ModDeployer(library, db=db).deploy_mod(88001)
    assert result.get("success") is True, result
    assert (custom / "payload.txt").is_file()
    assert (custom / "sub" / "inner.bin").is_file()
    # Must NOT nest the managed shell folder name.
    assert not (custom / "SpecialMod").exists()
    assert not (custom / INFO_DIR_NAME).exists()
