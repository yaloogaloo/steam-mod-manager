"""Total War: WARHAMMER III (AppID 1142710) pack-only deploy."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_FAILED, DatabaseManager
from core.mod_platform import (
    PLATFORM_STEAM,
    WARHAMMER3_APP_IDS,
    ModFileEntry,
    ModFilesBundle,
    is_warhammer3_game,
)
from services.backup_manager import transaction_path_for
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_WARHAMMER3,
    WARHAMMER3_APP_ID,
    Warhammer3Strategy,
    load_manifest,
    resolve_deploy_type,
    resolve_strategy,
)
from services.deploy_rules.base import DeployContext
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity, identity_create_scope

WH3 = WARHAMMER3_APP_ID
assert WH3 == 1142710
assert WH3 in WARHAMMER3_APP_IDS


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "wh3_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_wh3(db: DatabaseManager, tmp_path: Path) -> Path:
    mod_path = tmp_path / "Warhammer3Data"
    workshop = tmp_path / "workshop" / "content" / str(WH3)
    mod_path.mkdir(parents=True)
    workshop.mkdir(parents=True)
    db.update_game_deploy_config(
        WH3,
        name="Total War: WARHAMMER III",
        install_path=str(tmp_path / "WH3Install"),
        mod_path=str(mod_path),
        workshop_path=str(workshop),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    folder: str,
    external_id: str,
    files: dict[str, bytes | str],
    app_id: int = WH3,
    game_name: str = "Total War: WARHAMMER III",
    game_folder: str = "Warhammer3",
) -> tuple[Path, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(external_id),
            workshop_id=str(external_id),
            title=folder,
            app_id=app_id,
            game_name=game_name,
            operation="import",
        )
    pk = str(created.mod_id)
    mod_dir = library / game_folder / folder
    mod_dir.mkdir(parents=True)
    info = mod_dir / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": pk,
                "published_file_id": str(external_id),
                "title": folder,
                "app_id": app_id,
                "game_name": game_name,
                "platform": PLATFORM_STEAM,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for rel, data in files.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(mod_dir),
        folder_present=True,
    )
    if app_id == WH3:
        cfg = db.get_game_deploy_config(WH3)
        workshop_root = str(cfg.workshop_path or "").strip() if cfg is not None else ""
        if workshop_root:
            wdir = Path(workshop_root) / str(external_id)
            wdir.mkdir(parents=True, exist_ok=True)
            for rel, data in files.items():
                if not str(rel).lower().endswith(".pack"):
                    continue
                dest = wdir / Path(rel).name
                if isinstance(data, bytes):
                    dest.write_bytes(data)
                else:
                    dest.write_bytes(str(data).encode("utf-8"))
    return mod_dir, pk


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def _ctx(
    tmp_path: Path,
    db: DatabaseManager,
    source: Path,
    *,
    app_id: int = WH3,
    deploy_type: str = "folder_copy",
) -> DeployContext:
    cfg = db.get_game_deploy_config(app_id)
    assert cfg is not None
    return DeployContext(
        internal_id="1",
        source=source,
        app_id=app_id,
        config=cfg,
        deploy_type=deploy_type,
        managed_path=source,
    )


def test_app_id_identifies_warhammer3() -> None:
    assert is_warhammer3_game(game_id=1142710) is True
    assert is_warhammer3_game("Total War: WARHAMMER III") is True
    assert is_warhammer3_game("全面战争：战锤 III") is True
    assert is_warhammer3_game("Palworld", 1623730) is False
    assert is_warhammer3_game("Total War: WARHAMMER II") is False
    assert is_warhammer3_game(game_id=289070) is False


def test_resolve_wh3_deploy_type_and_strategy(tmp_path: Path, db: DatabaseManager) -> None:
    assert resolve_deploy_type(WH3, "folder_copy") == DEPLOY_TYPE_WARHAMMER3
    assert resolve_deploy_type(WH3, "palworld_pak") == DEPLOY_TYPE_WARHAMMER3
    _configure_wh3(db, tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pack").write_bytes(b"P")
    strategy = resolve_strategy(_ctx(tmp_path, db, src))
    assert isinstance(strategy, Warhammer3Strategy)
    assert strategy.deploy_type == DEPLOY_TYPE_WARHAMMER3


def _data_pack_names(mod_path: Path) -> set[str]:
    if not mod_path.exists():
        return set()
    return {
        p.name
        for p in mod_path.iterdir()
        if p.is_file() and p.suffix.lower() == ".pack"
    }


def test_plan_stays_in_library_not_game_data(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod_path = _configure_wh3(db, tmp_path)
    src = tmp_path / "library" / "Warhammer3" / "One"
    src.mkdir(parents=True)
    pack = src / "xxx.pack"
    pack.write_bytes(b"PACK")
    planned = Warhammer3Strategy().plan(_ctx(tmp_path, db, src))
    assert planned.success is True
    assert Path(planned.target).resolve() == src.resolve()
    assert Path(planned.files[0].source).resolve() == pack.resolve()
    assert Path(planned.files[0].target).resolve() == pack.resolve()
    assert str(mod_path) not in planned.files[0].target
    assert planned.copied_files == 0


def test_single_pack_deploys_without_copying_to_data(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="OnePack",
        external_id="11401",
        files={
            "xxx.pack": b"PACK",
            "preview.png": b"PNG",
            "readme.txt": "docs",
            "xxx.mod": "meta",
        },
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_WARHAMMER3
    assert int(result.get("copied_files") or 0) == 0
    assert (folder / "xxx.pack").read_bytes() == b"PACK"
    assert _data_pack_names(mod_path) == set()
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == "deployed"
    workshop_dir = tmp_path / "workshop" / "content" / str(WH3) / "11401"
    assert Path(info.deploy_path).resolve() == workshop_dir.resolve()
    used = tmp_path / "WH3Install" / "used_mods.txt"
    text = used.read_text(encoding="utf-8")
    assert str(workshop_dir) in text
    assert str(folder) not in text
    assert 'mod "xxx.pack";' in text
    assert pk not in text or f'mod "{pk}";' not in text
    assert f'mod "11401";' not in text


def test_multiple_packs_stay_in_library(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="Multi",
        external_id="11402",
        files={"a.pack": b"A", "b.pack": b"B", "readme.txt": "no"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (folder / "a.pack").read_bytes() == b"A"
    assert (folder / "b.pack").read_bytes() == b"B"
    assert _data_pack_names(mod_path) == set()
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    workshop_dir = tmp_path / "workshop" / "content" / str(WH3) / "11402"
    assert 'mod "a.pack";' in text
    assert 'mod "b.pack";' in text
    assert text.count("add_working_directory") == 1
    assert str(workshop_dir) in text
    assert str(folder) not in text
    assert not any("\\" in line and line.startswith("mod ") for line in text.splitlines())


def test_nested_folder_packs_activate_from_library(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="NestedDir",
        external_id="11412",
        files={
            "foo.pack": b"FOO",
            "bar.txt": "docs",
            "sub/baz.pack": b"BAZ",
            "sub/image.png": b"PNG",
            "sub/deep/more.pack": b"DEEP",
        },
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (folder / "foo.pack").read_bytes() == b"FOO"
    assert (folder / "sub" / "baz.pack").read_bytes() == b"BAZ"
    assert (folder / "sub" / "deep" / "more.pack").read_bytes() == b"DEEP"
    assert _data_pack_names(mod_path) == set()
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    workshop_dir = tmp_path / "workshop" / "content" / str(WH3) / "11412"
    assert 'mod "foo.pack";' in text
    assert 'mod "baz.pack";' in text
    assert 'mod "more.pack";' in text
    assert str(workshop_dir) in text
    assert str(folder) not in text
    assert not any(line.startswith("mod ") and "\\" in line for line in text.splitlines())


def test_non_pack_and_info_sidecar_not_activated(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="Sidecar",
        external_id="11403",
        files={"real.pack": b"OK", "notes.md": "# n"},
    )
    (folder / INFO_DIR_NAME / "hidden.pack").write_bytes(b"NO")
    (folder / "info").mkdir()
    (folder / "info" / "legacy.pack").write_bytes(b"NO")
    hist = folder / "历史版本"
    hist.mkdir()
    (hist / "old.pack").write_bytes(b"NO")

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    assert 'mod "real.pack";' in text
    assert "hidden.pack" not in text
    assert "legacy.pack" not in text
    assert "old.pack" not in text
    assert "notes.md" not in text
    assert _data_pack_names(mod_path) == set()


def test_chinese_folder_name_pack_only(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="中文模组包",
        external_id="11404",
        files={"单位.pack": b"CN", "说明.txt": "txt"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (folder / "单位.pack").read_bytes() == b"CN"
    assert _data_pack_names(mod_path) == set()
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    assert 'mod "单位.pack";' in text
    workshop_dir = tmp_path / "workshop" / "content" / str(WH3) / "11404"
    assert str(workshop_dir) in text
    assert str(folder) not in text


def test_pack_extension_case_insensitive(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="Case",
        external_id="11405",
        files={"Upper.PACK": b"U", "Mixed.Pack": b"M"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (folder / "Upper.PACK").read_bytes() == b"U"
    assert (folder / "Mixed.Pack").read_bytes() == b"M"
    assert _data_pack_names(mod_path) == set()
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    assert 'mod "Upper.PACK";' in text
    assert 'mod "Mixed.Pack";' in text


def test_archive_deploys_to_workshop_unzip(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder = "ZipNested"
    source, pk = _seed_mod(library, db, folder=folder, external_id="11406", files={})
    _make_zip(
        source / "mod.zip",
        {
            "package/abc.pack": b"ABC",
            "package/def.pack": b"DEF",
            "package/readme.txt": b"txt",
        },
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert _data_pack_names(mod_path) == set()
    assert (source / "mod.zip").is_file()
    unzip = tmp_path / "workshop" / "content" / str(WH3) / "ZipNested_unzip"
    packs = {p.name.lower() for p in unzip.rglob("*.pack")}
    assert "abc.pack" in packs
    assert "def.pack" in packs
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    assert str(source) not in text
    assert "ZipNested_unzip" in text
    assert 'mod "abc.pack";' in text
    assert 'mod "def.pack";' in text
    assert f'mod "{pk}";' not in text


# Nexus Mod 112: two deployable archives; checkbox must pick the exact file.
_WH3_112_QUEEN = "Khalida the Queen Uncensored-112-1-0-1688086374_2.zip"
_WH3_112_MORATHI = "Khalida Morathi Animation-112-1-0-1688086431.zip"
_WH3_112_QUEEN_PACK = "queen_uncensored.pack"
_WH3_112_MORATHI_PACK = "morathi_animation.pack"


def _seed_wh3_112_multi_archive(
    library: Path, db: DatabaseManager
) -> tuple[Path, str]:
    source, pk = _seed_mod(
        library,
        db,
        folder="Khalida the Queen Uncensored",
        external_id="112",
        files={},
    )
    _make_zip(source / _WH3_112_QUEEN, {_WH3_112_QUEEN_PACK: b"QUEEN"})
    _make_zip(source / _WH3_112_MORATHI, {_WH3_112_MORATHI_PACK: b"MORATHI"})
    return source, pk


def _set_wh3_112_selection(db: DatabaseManager, pk: str, selected_name: str) -> None:
    # Morathi first in JSON so files[0] is the *wrong* file if selection is ignored.
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="morathi",
                    filename=_WH3_112_MORATHI,
                    path=_WH3_112_MORATHI,
                    selected_for_deploy=selected_name == _WH3_112_MORATHI,
                ),
                ModFileEntry(
                    id="queen",
                    filename=_WH3_112_QUEEN,
                    path=_WH3_112_QUEEN,
                    selected_for_deploy=selected_name == _WH3_112_QUEEN,
                ),
            ]
        ),
    )


def _spy_wh3_extracts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from services import deploy_apply

    extracted: list[str] = []
    original = deploy_apply.extract_archive_via_core

    def _wrap(archive: Path, dest: Path):
        extracted.append(Path(archive).name)
        return original(archive, dest)

    monkeypatch.setattr(deploy_apply, "extract_archive_via_core", _wrap)
    return extracted


def _wh3_112_used_text(tmp_path: Path) -> str:
    used = tmp_path / "WH3Install" / "used_mods.txt"
    return used.read_text(encoding="utf-8") if used.is_file() else ""


def _wh3_112_unzip_packs(tmp_path: Path) -> set[str]:
    unzip = (
        tmp_path
        / "workshop"
        / "content"
        / str(WH3)
        / "Khalida the Queen Uncensored_unzip"
    )
    if not unzip.is_dir():
        return set()
    return {p.name.lower() for p in unzip.rglob("*.pack")}


def test_wh3_mod112_select_queen_deploys_queen_not_morathi(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    _source, pk = _seed_wh3_112_multi_archive(library, db)
    _set_wh3_112_selection(db, pk, _WH3_112_QUEEN)
    extracted = _spy_wh3_extracts(monkeypatch)

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert extracted == [_WH3_112_QUEEN]
    assert _WH3_112_MORATHI not in extracted
    packs = _wh3_112_unzip_packs(tmp_path)
    assert _WH3_112_QUEEN_PACK in packs
    assert _WH3_112_MORATHI_PACK not in packs
    text = _wh3_112_used_text(tmp_path)
    assert f'mod "{_WH3_112_QUEEN_PACK}";' in text
    assert _WH3_112_MORATHI_PACK not in text


def test_wh3_mod112_select_morathi_deploys_morathi_not_queen(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    _source, pk = _seed_wh3_112_multi_archive(library, db)
    _set_wh3_112_selection(db, pk, _WH3_112_MORATHI)
    extracted = _spy_wh3_extracts(monkeypatch)

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert extracted == [_WH3_112_MORATHI]
    assert _WH3_112_QUEEN not in extracted
    packs = _wh3_112_unzip_packs(tmp_path)
    assert _WH3_112_MORATHI_PACK in packs
    assert _WH3_112_QUEEN_PACK not in packs
    text = _wh3_112_used_text(tmp_path)
    assert f'mod "{_WH3_112_MORATHI_PACK}";' in text
    assert _WH3_112_QUEEN_PACK not in text


def test_wh3_mod112_redeploy_follows_current_checkbox(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    _source, pk = _seed_wh3_112_multi_archive(library, db)
    extracted = _spy_wh3_extracts(monkeypatch)
    deployer = ModDeployer(library_root=library, db=db)

    _set_wh3_112_selection(db, pk, _WH3_112_QUEEN)
    first = deployer.deploy_mod(pk)
    assert first["success"] is True, first
    assert extracted == [_WH3_112_QUEEN]
    text = _wh3_112_used_text(tmp_path)
    assert f'mod "{_WH3_112_QUEEN_PACK}";' in text
    assert _WH3_112_MORATHI_PACK not in text

    extracted.clear()
    _set_wh3_112_selection(db, pk, _WH3_112_MORATHI)
    second = deployer.deploy_mod(pk)
    assert second["success"] is True, second
    assert extracted == [_WH3_112_MORATHI]
    assert _WH3_112_QUEEN not in extracted
    packs = _wh3_112_unzip_packs(tmp_path)
    assert _WH3_112_MORATHI_PACK in packs
    assert _WH3_112_QUEEN_PACK not in packs
    text = _wh3_112_used_text(tmp_path)
    assert f'mod "{_WH3_112_MORATHI_PACK}";' in text
    assert _WH3_112_QUEEN_PACK not in text

    extracted.clear()
    _set_wh3_112_selection(db, pk, _WH3_112_QUEEN)
    third = deployer.deploy_mod(pk)
    assert third["success"] is True, third
    assert extracted == [_WH3_112_QUEEN]
    text = _wh3_112_used_text(tmp_path)
    assert f'mod "{_WH3_112_QUEEN_PACK}";' in text
    assert _WH3_112_MORATHI_PACK not in text


def test_wh3_single_archive_selection_unchanged(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    source, pk = _seed_mod(
        library, db, folder="OneZip", external_id="11430", files={}
    )
    zip_name = "only-mod.zip"
    _make_zip(source / zip_name, {"only.pack": b"ONLY"})
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="only",
                    filename=zip_name,
                    path=zip_name,
                    selected_for_deploy=True,
                )
            ]
        ),
    )
    extracted = _spy_wh3_extracts(monkeypatch)
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert extracted == [zip_name]
    unzip = tmp_path / "workshop" / "content" / str(WH3) / "OneZip_unzip"
    packs = {p.name.lower() for p in unzip.rglob("*.pack")}
    assert packs == {"only.pack"}
    text = (tmp_path / "WH3Install" / "used_mods.txt").read_text(encoding="utf-8")
    assert 'mod "only.pack";' in text
    assert str(source) not in text


def test_workshop_path_missing_fails_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.wh3_activation import WH3_WORKSHOP_MISSING

    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    _folder, pk = _seed_mod(
        library, db, folder="Gone", external_id="11420", files={"a.pack": b"A"}
    )
    db.update_game_deploy_config(
        WH3,
        workshop_path=str(tmp_path / "missing_workshop"),
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is False
    assert WH3_WORKSHOP_MISSING in str(result.get("error") or "")
    assert _data_pack_names(mod_path) == set()


def test_missing_pack_fails_without_half_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    sentinel = mod_path / "keep.pack"
    sentinel.write_bytes(b"KEEP")
    source, pk = _seed_mod(
        library,
        db,
        folder="NoPack",
        external_id="11407",
        files={"readme.txt": "only docs", "preview.png": b"PNG"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is False
    assert "未找到 .pack 文件" in str(result.get("error") or "")
    assert sentinel.read_bytes() == b"KEEP"
    assert {p.name for p in mod_path.iterdir()} == {"keep.pack"}
    assert load_manifest(source) is None
    assert not transaction_path_for(source).is_file()
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED


def test_archive_without_pack_fails(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    source, pk = _seed_mod(
        library,
        db,
        folder="ZipEmpty",
        external_id="11408",
        files={"notes.txt": "docs only"},
    )
    _make_zip(
        source / "docs.zip",
        {"readme.txt": b"hi", "preview.jpg": b"img"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is False
    assert "未找到 .pack 文件" in str(result.get("error") or "")
    assert list(mod_path.iterdir()) == []
    assert load_manifest(source) is None
    assert not transaction_path_for(source).is_file()


def test_activation_does_not_write_copy_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    source, pk = _seed_mod(
        library,
        db,
        folder="Mani",
        external_id="11409",
        files={"a.pack": b"A", "b.pack": b"B", "readme.txt": "no"},
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert load_manifest(source) is None
    assert int(result.get("copied_files") or 0) == 0


def test_same_name_packs_do_not_copy_into_data(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod_path = _configure_wh3(db, tmp_path)
    folder_a, pk_a = _seed_mod(
        library, db, folder="First", external_id="11410", files={"shared.pack": b"ONE"}
    )
    folder_b, pk_b = _seed_mod(
        library, db, folder="Second", external_id="11411", files={"shared.pack": b"TWO"}
    )
    deployer = ModDeployer(library_root=library, db=db)

    first = deployer.deploy_mod(pk_a)
    assert first["success"] is True, first
    assert (folder_a / "shared.pack").read_bytes() == b"ONE"
    assert _data_pack_names(mod_path) == set()

    second = deployer.deploy_mod(pk_b)
    assert second["success"] is True, second
    assert (folder_b / "shared.pack").read_bytes() == b"TWO"
    assert (folder_a / "shared.pack").read_bytes() == b"ONE"
    assert _data_pack_names(mod_path) == set()


def test_folder_copy_game_still_deploys_pack_and_readme(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Regression: pack-only must not leak into generic folder_copy."""
    library = tmp_path / "library"
    mods_root = tmp_path / "OtherMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        100,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    _folder, pk = _seed_mod(
        library,
        db,
        folder="PlainMod",
        external_id="81099",
        files={"abc.pack": b"PACK", "readme.txt": "docs"},
        app_id=100,
        game_name="SomeGame",
        game_folder="SomeGame",
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY
    dest = mods_root / "PlainMod"
    assert (dest / "abc.pack").read_bytes() == b"PACK"
    assert (dest / "readme.txt").read_text(encoding="utf-8") == "docs"
    assert resolve_deploy_type(100, "folder_copy") == DEPLOY_TYPE_FOLDER_COPY


def test_wh3_deploy_does_not_copy_or_hash_pack(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="Timed",
        external_id="11413",
        files={"big.pack": b"PACK" * 64, "skip.txt": "no"},
    )

    def _fail_copy(*_a, **_k):
        raise AssertionError("WH3 deploy must not copy packs")

    def _fail_hash(*_a, **_k):
        raise AssertionError("WH3 deploy must not hash packs")

    monkeypatch.setattr(shutil, "copy2", _fail_copy)
    monkeypatch.setattr(shutil, "copyfile", _fail_copy)
    monkeypatch.setattr(shutil, "copytree", _fail_copy)
    monkeypatch.setattr("services.mod_source_integrity._sha256_file", _fail_hash)
    monkeypatch.setattr(
        "services.deploy_apply.apply_file_plan",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("WH3 deploy must not run Core Apply")
        ),
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert int(result.get("copied_files") or 0) == 0
    timing = result.get("deploy_timing") or {}
    stages = {row["stage"] for row in timing.get("stages") or []}
    assert "copy" not in stages
    assert "hash" not in stages
    diag = timing.get("diagnostics") or {}
    assert int(diag.get("copied_files") or 0) == 0
    assert (folder / "big.pack").read_bytes() == b"PACK" * 64


def test_undeploy_does_not_delete_library_pack(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _configure_wh3(db, tmp_path)
    folder, pk = _seed_mod(
        library,
        db,
        folder="Keep",
        external_id="11414",
        files={"stay.pack": b"KEEP"},
    )
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    und = deployer.undeploy_mod(pk)
    assert und["success"] is True, und
    assert (folder / "stay.pack").read_bytes() == b"KEEP"
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status != "deployed"
    used = tmp_path / "WH3Install" / "used_mods.txt"
    text = used.read_text(encoding="utf-8") if used.is_file() else ""
    assert "stay.pack" not in text
