"""ID architecture contract — Internal / Workspace / external_id must stay distinct."""

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
    is_internal_mod_id,
    resolve_workspace_id,
    steam_workshop_url,
)
from core.models import ModMetadata
from services.identity_invariants import (
    AMBIGUOUS_MOD_ID_USAGE,
    INTERNAL_ID_EXPOSED_IN_USER_UI,
    INTERNAL_ID_USED_AS_EXTERNAL_ID,
    INTERNAL_ID_USED_AS_WORKSPACE_ID,
    scan_id_architecture_source,
    scan_invalid_entities,
)
from services.identity_service import (
    IdentityCreateBypassError,
    bind_platform_identity,
    create_mod_identity,
    lifecycle_scope,
    persist_workspace_id,
    sidecar_published_file_id,
)
from services.mod_identity_authority import (
    safe_workspace_id_for_deploy,
    sanitize_platform_external_id,
)
from ui.library_query import ModFilterIndex, matches_search
from ui.platform_labels import format_mod_info_clipboard, platform_id_label


STEAM_WORKSHOP = "3571849225"
NEXUS_MOD = "7329"
INTERNAL = "9000000000003410"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "id_arch.db")
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    yield manager
    DatabaseManager.reset_instance()


def test_internal_id_is_database_only() -> None:
    assert is_internal_mod_id(INTERNAL)
    assert not is_internal_mod_id(STEAM_WORKSHOP)
    assert persist_workspace_id(
        platform=PLATFORM_NEXUS, mod_id=INTERNAL, workspace_id=INTERNAL
    ) == ""


def test_workspace_id_is_user_facing_identifier() -> None:
    assert platform_id_label(PLATFORM_STEAM) == "Workspace ID"
    assert platform_id_label(PLATFORM_NEXUS) == "Workspace ID"
    text = format_mod_info_clipboard(
        name="X", platform=PLATFORM_STEAM, workspace_id=STEAM_WORKSHOP
    )
    assert "Workspace ID:" in text
    assert "Internal ID" not in text
    assert "Steam Workshop ID" not in text


def test_steam_workspace_id_equals_external_id(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_STEAM,
        workshop_id=STEAM_WORKSHOP,
        external_id=STEAM_WORKSHOP,
        source_url=steam_workshop_url(STEAM_WORKSHOP),
        title="Errata Thermal Katana",
        app_id=100,
        game_name="SomeGame",
        operation="import",
    )
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.external_id == STEAM_WORKSHOP
    assert info.workspace_id == STEAM_WORKSHOP
    assert info.workspace_id == info.external_id


def test_nexus_workspace_id_equals_external_id(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=NEXUS_MOD,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{NEXUS_MOD}",
        title="Nexus Mod",
        app_id=100,
        game_name="SomeGame",
        operation="import",
    )
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert is_internal_mod_id(info.mod_id)
    assert info.external_id == NEXUS_MOD
    assert info.workspace_id == NEXUS_MOD
    assert info.workspace_id != info.mod_id


def test_other_platform_workspace_id_is_generated(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_OTHER,
        external_id="local/folder",
        source_url="",
        title="Local Mod",
        app_id=100,
        game_name="SomeGame",
        operation="import",
    )
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert is_internal_mod_id(info.mod_id)
    assert info.workspace_id
    assert info.workspace_id != info.mod_id
    assert not is_internal_mod_id(info.workspace_id)


def test_internal_id_never_generates_workspace_id() -> None:
    assert resolve_workspace_id(PLATFORM_STEAM, mod_id=STEAM_WORKSHOP) == ""
    assert resolve_workspace_id(PLATFORM_STEAM, external_id=STEAM_WORKSHOP) == STEAM_WORKSHOP
    assert resolve_workspace_id(PLATFORM_NEXUS, mod_id=INTERNAL) == ""
    assert resolve_workspace_id(PLATFORM_OTHER, mod_id=INTERNAL) == ""
    assert persist_workspace_id(
        platform=PLATFORM_STEAM, mod_id=INTERNAL, workspace_id=""
    ) == ""


def test_internal_id_never_generates_external_id() -> None:
    assert sanitize_platform_external_id(PLATFORM_STEAM, INTERNAL, mod_id=INTERNAL) == ""
    assert sanitize_platform_external_id(PLATFORM_NEXUS, INTERNAL, mod_id=INTERNAL) == ""


def test_internal_id_never_generates_platform_url() -> None:
    assert steam_workshop_url(INTERNAL) == ""
    assert steam_workshop_url(STEAM_WORKSHOP).endswith(f"id={STEAM_WORKSHOP}")


def test_normal_ui_exposes_workspace_id_only() -> None:
    findings = scan_id_architecture_source()
    ui_hits = [
        f
        for f in findings
        if f.violation_code == INTERNAL_ID_EXPOSED_IN_USER_UI
    ]
    assert ui_hits == [], [f.evidence for f in ui_hits]


def test_debug_ui_may_expose_internal_id_explicitly() -> None:
    from ui import mod_detail_panel as panel_mod

    src = Path(panel_mod.__file__).read_text(encoding="utf-8")
    assert "Internal Database ID" in src
    assert "内部 ID" not in src
    assert "Steam Workshop ID" not in src


def test_same_numeric_value_does_not_merge_id_semantics() -> None:
    """Steam PK may equal Workshop ID numerically without merging field meaning."""
    same = STEAM_WORKSHOP
    assert resolve_workspace_id(PLATFORM_STEAM, mod_id=same) == ""
    assert resolve_workspace_id(PLATFORM_STEAM, external_id=same) == same
    pub = sidecar_published_file_id(
        mod_id=same, platform=PLATFORM_STEAM, external_id=same
    )
    assert pub == same
    pub_from_internal_only = sidecar_published_file_id(
        mod_id=same, platform=PLATFORM_STEAM, external_id=""
    )
    assert pub_from_internal_only == ""


def test_reconcile_does_not_mint_workspace_id_from_internal_id(db: DatabaseManager) -> None:
    ws = persist_workspace_id(
        platform=PLATFORM_STEAM,
        mod_id=INTERNAL,
        workspace_id="",
        external_id="",
    )
    assert ws == ""
    assert safe_workspace_id_for_deploy(
        platform=PLATFORM_OTHER, workspace_id="", mod_id=INTERNAL
    ) == ""


def test_no_unknown_mod_can_be_created_from_internal_id(db: DatabaseManager) -> None:
    with lifecycle_scope("import"):
        with pytest.raises(IdentityCreateBypassError):
            create_mod_identity(
                db,
                platform=PLATFORM_STEAM,
                external_id=INTERNAL,
                workshop_id=INTERNAL,
                title="Unknown Mod",
                app_id=100,
                game_name="SomeGame",
                operation="import",
            )


def test_bind_platform_identity_does_not_copy_internal_to_workspace(
    db: DatabaseManager,
) -> None:
    mid = str(db.allocate_mod_id())
    bind_platform_identity(
        db,
        mid,
        platform=PLATFORM_NEXUS,
        external_id=NEXUS_MOD,
        source_url=f"https://www.nexusmods.com/game/mods/{NEXUS_MOD}",
        app_id=100,
        title="Bound",
    )
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.workspace_id == NEXUS_MOD
    assert info.workspace_id != mid


def test_search_uses_workspace_id_not_internal_id() -> None:
    item = ModFilterIndex(
        mod_id=INTERNAL,
        display_name="Katana",
        steam_name="",
        notes="",
        game_name="Cyberpunk 2077",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="Katana",
        workspace_id=STEAM_WORKSHOP,
    )
    assert matches_search(item, STEAM_WORKSHOP)
    assert not matches_search(item, INTERNAL)


def test_static_id_semantic_violations_are_detectable(db: DatabaseManager, tmp_path: Path) -> None:
    mid = str(db.allocate_mod_id())
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET platform=?, external_id=?, workspace_id=?, source_url=? "
            "WHERE mod_id=?",
            (
                PLATFORM_NEXUS,
                mid,
                mid,
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}",
                int(mid),
            ),
        )
        db._conn.commit()
    report = scan_invalid_entities(tmp_path / "mod", db=db)
    codes = {f.violation_code for f in report.findings if f.entity_id == mid}
    assert INTERNAL_ID_USED_AS_WORKSPACE_ID in codes
    assert INTERNAL_ID_USED_AS_EXTERNAL_ID in codes
    src = scan_id_architecture_source()
    assert all(f.violation_code != AMBIGUOUS_MOD_ID_USAGE for f in src)


def test_steam_upsert_workspace_from_workshop_not_internal_fallback(
    db: DatabaseManager,
) -> None:
    db.upsert_mod(ModMetadata(published_file_id=STEAM_WORKSHOP, title="Katana", app_id=100))
    info = db.get_mod_display_info(STEAM_WORKSHOP)
    assert info is not None
    assert info.external_id == STEAM_WORKSHOP
    assert info.workspace_id == STEAM_WORKSHOP
    # Semantics remain distinct even when numbers match.
    assert info.mod_id == STEAM_WORKSHOP
    assert resolve_workspace_id(PLATFORM_STEAM, mod_id=info.mod_id) == ""
    assert resolve_workspace_id(PLATFORM_STEAM, external_id=info.external_id) == info.workspace_id


def test_github_workspace_is_not_internal_id(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="GH",
        app_id=100,
        game_name="SomeGame",
        operation="import",
    )
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.workspace_id
    assert info.workspace_id != info.mod_id
    assert not is_internal_mod_id(info.workspace_id)
