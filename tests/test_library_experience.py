"""Phase 7: Library experience — filters, relocate, backup display, game summary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.game_status import (
    OVERALL_HEALTHY,
    OVERALL_WARNING,
    aggregate_game_status,
    header_status_line,
)
from services.library_reconcile import reconcile_library
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    CONTENT_IDENTITY_CONFLICT,
)
from services.mod_relocate import relocate_mod_folder
from ui.library_query import (
    FILTER_CONTENT_MISSING,
    FILTER_FOLDER_MISSING,
    FILTER_IDENTITY_CONFLICT,
    FILTER_PLATFORM_EXTERNAL,
    ModFilterIndex,
    filter_and_sort,
    matches_platform_filter,
    matches_status_filter,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "experience.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.pak").write_bytes(b"pak")


def _index(
    *,
    mod_id: str = "1",
    content_status: str = "",
    source_type: str = "steam",
    platform: str = "steam",
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name="X",
        steam_name="X",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="X",
        content_status=content_status,
        source_type=source_type,
        platform=platform,
    )


def test_case1_folder_missing_filterable() -> None:
    entries = [
        (_index(mod_id="1", content_status=CONTENT_HEALTHY), "ok"),
        (_index(mod_id="2", content_status=CONTENT_FOLDER_MISSING), "miss"),
        (_index(mod_id="3", content_status=CONTENT_CONTENT_MISSING), "empty"),
    ]
    found = filter_and_sort(entries, filter_key=FILTER_FOLDER_MISSING)
    assert found == ["miss"]
    assert matches_status_filter(
        _index(content_status=CONTENT_FOLDER_MISSING), FILTER_FOLDER_MISSING
    )
    assert matches_status_filter(
        _index(content_status=CONTENT_CONTENT_MISSING), FILTER_CONTENT_MISSING
    )
    assert matches_status_filter(
        _index(content_status=CONTENT_IDENTITY_CONFLICT), FILTER_IDENTITY_CONFLICT
    )


def test_case2_relocate_identity_match(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "Mod1"
    _write_info(
        folder,
        {
            "published_file_id": "982001",
            "title": "Mod1",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-982001",
            "external_id": "982001",
        },
    )
    reconcile_library(library)
    archive = tmp_path / "elsewhere" / "Mod1Restored"
    shutil.copytree(folder, archive)
    shutil.rmtree(folder)
    reconcile_library(library)
    row = db.get_mod_backup_row("982001")
    assert row is not None
    assert int(row["folder_present"]) == 0

    result = relocate_mod_folder("982001", archive)
    assert result.success, result.error
    assert result.matched_by in {
        "published_file_id",
        "identity_match",
        "workspace_id",
        "internal_id",
    }
    row = db.get_mod_backup_row("982001")
    assert int(row["folder_present"]) == 1
    assert Path(str(row["last_known_path"])).resolve() == archive.resolve()
    assert str(row.get("content_status") or "") in (CONTENT_HEALTHY, "healthy", "")


def test_case3_relocate_wrong_folder_rejected(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "982002",
            "title": "ModA",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-982002",
        },
    )
    other = library / "GameA" / "Other"
    _write_info(
        other,
        {
            "published_file_id": "982099",
            "title": "Other",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-982099",
        },
    )
    reconcile_library(library)
    shutil.rmtree(folder)
    reconcile_library(library)

    result = relocate_mod_folder("982002", other)
    assert not result.success
    assert "不匹配" in (result.error or "") or "mismatch" in (result.error or "").lower()


def test_case4_game_summary_aggregation() -> None:
    summary = aggregate_game_status(
        "Anno1800",
        content_statuses=[
            CONTENT_HEALTHY,
            CONTENT_HEALTHY,
            CONTENT_FOLDER_MISSING,
            CONTENT_CONTENT_MISSING,
        ],
    )
    assert summary.overall_status == OVERALL_WARNING
    assert summary.healthy_count == 2
    assert summary.folder_missing_count == 1
    assert summary.content_missing_count == 1
    line = header_status_line(summary)
    assert "正常 2" in line
    assert "目录缺失 1" in line
    assert "内容缺失 1" in line

    healthy = aggregate_game_status(
        "G", content_statuses=[CONTENT_HEALTHY, CONTENT_HEALTHY]
    )
    assert healthy.overall_status == OVERALL_HEALTHY


def test_case5_backup_invalid_filter_and_source(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    # Filter by backup_invalid content_status
    idx = _index(content_status="backup_invalid", source_type="external")
    assert matches_status_filter(idx, "backup_invalid")
    assert matches_platform_filter(idx, FILTER_PLATFORM_EXTERNAL)

    # Persist invalid backup_status and ensure diagnostics / row read works
    library = tmp_path / "mod"
    folder = library / "GameB" / "ModB"
    _write_info(
        folder,
        {
            "published_file_id": "982003",
            "title": "ModB",
            "game_name": "GameB",
            "source_type": "github",
            "workspace_id": "ws-982003",
        },
    )
    reconcile_library(library)
    db.update_mod_identity_fields(
        "982003",
        content_status="backup_invalid",
        library_status="backup_invalid",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET backup_status = ? WHERE mod_id = ?",
            ("invalid", 982003),
        )
        db._conn.commit()
    row = db.get_mod_backup_row("982003")
    assert str(row.get("backup_status") or "") == "invalid"
    assert str(row.get("content_status") or "") == "backup_invalid"

    from services.library_diagnostics import build_library_diagnostics

    payload = build_library_diagnostics(library, data_root=data_root)
    assert "982003" in payload["mods"]["invalid_backup"]
