"""Total War: WARHAMMER III activation / load order / used_mods.txt.

WH3-only. Enable, disable, and load-order changes never copy ``.pack`` files.

``used_mods.txt`` format is taken from Shazbot/WH3-Mod-Manager
(``src/ipcMainListeners.ts`` startGame writer and ``src/usedMods.ts`` parser):

    add_working_directory "C:\\path\\to\\mod\\folder";
    mod "filename.pack";

Working-directory lines are unique and listed first for directories that are
not the game data folder. Then ``mod "name.pack";`` lines follow load order.
WH3 encoding is UTF-8. Launch argument is ``used_mods.txt;``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager, get_db
from core.mod_platform import WARHAMMER3_APP_IDS, is_warhammer3_game
from services.deploy_rules.pak_mod_path import iter_suffix_payload_files

logger = logging.getLogger(__name__)

WH3_APP_ID = next(iter(WARHAMMER3_APP_IDS))
USED_MODS_FILENAME = "used_mods.txt"
WH3_EXE_NAME = "Warhammer3.exe"
WH3_LAUNCH_ARG = f"{USED_MODS_FILENAME};"
WH3_CANONICAL_ORDER_FILENAME = "wh3.json"
WH3_STATE_DIRNAME = "wh3"
WH3_ORDER_FILENAME = "load_order.json"
WH3_LEGACY_ORDER_FILENAME = "wh3_load_order.json"


def is_wh3_activation_app(app_id: int | str = 0, game_name: str = "") -> bool:
    """True when this game uses WH3 activation (App ID 1142710)."""
    try:
        aid = int(str(app_id or 0).strip() or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid in WARHAMMER3_APP_IDS:
        return True
    return bool(game_name) and is_warhammer3_game(game_name, aid)


def canon_internal_id(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


def _order_path() -> Path:
    from core.paths import load_order_dir

    return load_order_dir() / WH3_CANONICAL_ORDER_FILENAME


def _legacy_order_paths() -> list[Path]:
    from core.paths import data_dir

    return [
        data_dir() / WH3_STATE_DIRNAME / WH3_ORDER_FILENAME,
        data_dir() / WH3_LEGACY_ORDER_FILENAME,
    ]


def _unlink_quietly(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.debug("could not remove leftover WH3 load-order file %s", path, exc_info=True)


def _remove_legacy_order_files() -> None:
    for legacy in _legacy_order_paths():
        _unlink_quietly(legacy)


def _parse_order_payload(raw: object) -> list[str]:
    if isinstance(raw, dict):
        items = raw.get("order") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        mid = canon_internal_id(item)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _read_order_file(path: Path) -> list[str] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return _parse_order_payload(raw)


def load_saved_order() -> list[str]:
    """Persisted load-order of ``internal_id`` values (may include stale ids)."""
    path = _order_path()
    if path.is_file():
        parsed = _read_order_file(path)
        _remove_legacy_order_files()
        return parsed if parsed is not None else []
    for legacy in _legacy_order_paths():
        if not legacy.is_file():
            continue
        parsed = _read_order_file(legacy)
        if parsed is None:
            continue
        save_saved_order(parsed)
        return parsed
    return []


def save_saved_order(internal_ids: list[str]) -> None:
    path = _order_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in internal_ids:
        mid = canon_internal_id(raw)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ordered.append(mid)
    path.write_text(
        json.dumps({"order": ordered}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _remove_legacy_order_files()


def merge_deployed_order(saved: list[str], deployed_ids: list[str]) -> list[str]:
    """Keep saved order for still-deployed mods; append newly deployed; drop missing."""
    deployed: list[str] = []
    seen: set[str] = set()
    for raw in deployed_ids:
        mid = canon_internal_id(raw)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        deployed.append(mid)
    live = set(deployed)
    merged: list[str] = []
    seen.clear()
    for mid in saved:
        canon = canon_internal_id(mid)
        if canon in live and canon not in seen:
            seen.add(canon)
            merged.append(canon)
    for mid in deployed:
        if mid not in seen:
            seen.add(mid)
            merged.append(mid)
    return merged


def insertion_index(source_index: int, target_index: int, remaining: int) -> int:
    """Shazbot ``getLoadOrderInsertionIndex`` for card-to-card drops."""
    adjusted = (
        target_index - 1
        if source_index >= 0 and target_index > source_index
        else target_index
    )
    return max(0, min(adjusted, remaining))


def move_in_order(
    order: list[str],
    source_id: str,
    target_id: str,
) -> list[str]:
    """Move *source_id* onto *target_id*'s slot. Display numbers are list positions."""
    src = canon_internal_id(source_id)
    dst = canon_internal_id(target_id)
    ids = [canon_internal_id(i) for i in order if canon_internal_id(i)]
    if not src or src not in ids:
        return ids
    if not dst or dst not in ids or src == dst:
        return ids
    source_index = ids.index(src)
    target_index = ids.index(dst)
    remaining = [i for i in ids if i != src]
    idx = insertion_index(source_index, target_index, len(remaining))
    remaining.insert(idx, src)
    return remaining


def display_numbers(order: list[str]) -> dict[str, int]:
    """1-based consecutive numbers for the full ordered list."""
    return {mid: i + 1 for i, mid in enumerate(order)}


@dataclass(frozen=True)
class Wh3ModRef:
    internal_id: str
    workspace_id: str
    enabled: bool
    last_known_path: str
    deploy_status: str
    platform: str = ""
    external_id: str = ""
    title: str = ""


@dataclass(frozen=True)
class Wh3PackLine:
    pack_name: str
    directory: str
    in_data: bool
    internal_id: str


@dataclass(frozen=True)
class Wh3GamePaths:
    install_path: str
    data_folder: str
    workshop_path: str


def _row_to_ref(row: dict[str, Any]) -> Wh3ModRef | None:
    mid = canon_internal_id(row.get("internal_id") or row.get("mod_id"))
    if not mid:
        return None
    enabled = True
    raw_enabled = row.get("enabled", True)
    try:
        enabled = bool(int(raw_enabled)) if raw_enabled is not None else True
    except (TypeError, ValueError):
        enabled = bool(raw_enabled)
    return Wh3ModRef(
        internal_id=mid,
        workspace_id=str(row.get("workspace_id") or "").strip(),
        enabled=enabled,
        last_known_path=str(
            row.get("managed_path") or row.get("last_known_path") or ""
        ).strip(),
        deploy_status=str(row.get("deploy_status") or "").strip(),
        platform=str(row.get("platform") or "").strip(),
        external_id=str(row.get("external_id") or "").strip(),
        title=str(row.get("steam_name") or row.get("name") or "").strip(),
    )


def list_deployed_wh3_mods(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[Wh3ModRef]:
    database = db if db is not None else get_db()
    deployed_ids: list[str] = []
    seen: set[str] = set()
    for raw in list(database.list_deployed_mod_ids_for_app(WH3_APP_ID)) + list(
        database.list_deployed_mod_ids_for_library_game(
            WH3_APP_ID, library_root=library_root
        )
    ):
        mid = canon_internal_id(raw)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        deployed_ids.append(mid)
    if not deployed_ids:
        return []
    by_id: dict[str, Wh3ModRef] = {}
    for row in database.list_mod_list_items(app_id=WH3_APP_ID):
        ref = _row_to_ref(row)
        if ref is not None:
            by_id[ref.internal_id] = ref
    missing = [mid for mid in deployed_ids if mid not in by_id]
    for mid in missing:
        rows = database.list_mod_list_items(mod_id=mid)
        if not rows:
            continue
        ref = _row_to_ref(rows[0])
        if ref is not None:
            by_id[mid] = ref
    out: list[Wh3ModRef] = []
    for mid in deployed_ids:
        ref = by_id.get(mid)
        if ref is None:
            continue
        if ref.deploy_status and ref.deploy_status != DEPLOY_STATUS_DEPLOYED:
            continue
        out.append(ref)
    return out


def resolved_load_order(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    deployed = [
        m.internal_id
        for m in list_deployed_wh3_mods(db, library_root=library_root)
    ]
    return merge_deployed_order(load_saved_order(), deployed)


def persist_load_order(
    internal_ids: list[str],
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    deployed = [
        m.internal_id
        for m in list_deployed_wh3_mods(db, library_root=library_root)
    ]
    merged = merge_deployed_order(internal_ids, deployed)
    save_saved_order(merged)
    return merged


def apply_card_drop(
    source_id: str,
    target_id: str,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    current = resolved_load_order(db, library_root=library_root)
    next_order = move_in_order(current, source_id, target_id)
    save_saved_order(next_order)
    return next_order


def set_wh3_enabled(
    internal_id: str,
    enabled: bool,
    db: DatabaseManager | None = None,
) -> bool:
    """Toggle ``mods.enabled`` only — never deploys or copies files."""
    database = db if db is not None else get_db()
    mid = canon_internal_id(internal_id)
    if not mid or not mid.isdigit():
        return False
    if enabled:
        database.enable_mod(mid)
    else:
        database.disable_mod(mid)
    return bool(database.is_mod_enabled(mid)) is bool(enabled)


def load_wh3_game_paths(db: DatabaseManager | None = None) -> Wh3GamePaths:
    database = db if db is not None else get_db()
    cfg = database.get_game_deploy_config(WH3_APP_ID)
    if cfg is None:
        return Wh3GamePaths(install_path="", data_folder="", workshop_path="")
    return Wh3GamePaths(
        install_path=str(cfg.install_path or "").strip(),
        data_folder=str(cfg.mod_path or "").strip(),
        workshop_path=str(cfg.workshop_path or "").strip(),
    )


WH3_WORKSHOP_MISSING = "战锤 III 部署失败：Steam Workshop 目录不存在"
WH3_WORKSHOP_MOD_MISSING = "战锤 III 部署失败：Workshop Mod 目录不存在"
WH3_MISSING_PACK = "战锤 III Mod 部署失败：未找到 .pack 文件"


def workshop_content_root(workshop_path: str) -> Path | None:
    """WH3 Workshop content root: ``.../workshop/content/1142710``."""
    raw = str(workshop_path or "").strip()
    if not raw:
        return None
    base = Path(raw).expanduser()
    app = str(WH3_APP_ID)
    try:
        name = base.name
    except OSError:
        return None
    if name == app:
        return base
    if name.lower() == "content":
        return base / app
    return base / "content" / app


def local_mod_directory(ref: Wh3ModRef) -> Path | None:
    """Managed library folder (import location). Not used in used_mods.txt."""
    raw = str(ref.last_known_path or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        if path.is_file() and path.suffix.lower() == ".pack":
            return path.parent
        if path.is_dir():
            return path
    except OSError:
        return None
    return None


def _list_packs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return list(iter_suffix_payload_files(folder, suffix=".pack"))


def _has_cjk(text: str) -> bool:
    for char in text:
        code = ord(char)
        if 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            return True
        if 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF:
            return True
    return False


def english_unzip_stem(title: str, archive_stem: str = "") -> str:
    """English Mod title, else archive stem. Never a CJK library folder name."""
    from core.sanitize import sanitize_folder_name

    raw = str(title or "").strip()
    if raw and not raw.isdigit() and not _has_cjk(raw):
        cleaned = sanitize_folder_name(raw, fallback="")
        if cleaned:
            return cleaned
    stem = str(archive_stem or "").strip()
    if stem:
        return sanitize_folder_name(Path(stem).stem, fallback="mod")
    return ""


def _list_library_archives(folder: Path) -> list[Path]:
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
    from services.importers.archive import is_archive_path

    if not folder.is_dir():
        return []
    skip = {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"}
    archives: list[Path] = []
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return []
    for path in children:
        if path.name in skip or path.name.startswith("."):
            continue
        try:
            if path.is_file() and is_archive_path(path):
                archives.append(path)
        except OSError:
            continue
    return archives


def _match_selected_library_archive(
    library: Path,
    entry: Any,
    discovered_by_name: dict[str, Path],
) -> Path | None:
    """Resolve one ``mod_files`` entry onto a discovered library archive."""
    names: list[str] = []
    for raw in (
        getattr(entry, "path", None),
        getattr(entry, "filename", None),
        getattr(entry, "name", None),
    ):
        text = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not text:
            continue
        if text not in names:
            names.append(text)
        base = Path(text).name
        if base and base not in names:
            names.append(base)
    for name in names:
        try:
            candidate = library / name
            if candidate.is_file():
                hit = discovered_by_name.get(candidate.name.lower())
                if hit is not None:
                    return hit
        except OSError:
            pass
        hit = discovered_by_name.get(Path(name).name.lower())
        if hit is not None:
            return hit
    return None


def _filter_wh3_selected_archives(
    ref: Wh3ModRef,
    library: Path | None,
    discovered: list[Path],
    *,
    db: DatabaseManager | None = None,
) -> list[Path]:
    """
    Multi-archive Mods: deploy only ``selected_for_deploy`` files.

    Single-archive / empty ``mod_files`` keep the previous scan (no regression).
    Never falls back to filename order when the user has a checkbox selection.
    """
    if library is None or len(discovered) <= 1:
        return list(discovered)
    database = db if db is not None else get_db()
    try:
        from services.mod_library_cache import dal_mod_pk

        pk = dal_mod_pk(ref.internal_id)
        if not pk:
            return list(discovered)
        bundle = database.get_mod_files(pk)
    except Exception:  # noqa: BLE001
        return list(discovered)
    if not bundle.files:
        return list(discovered)

    from core.mod_platform import (
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        is_entry_selected_for_deploy,
        normalize_file_role,
    )

    discovered_by_name = {path.name.lower(): path for path in discovered}
    selected: list[Path] = []
    seen: set[str] = set()
    for entry in bundle.files:
        if normalize_file_role(getattr(entry, "file_role", None)) == (
            FILE_ROLE_GITHUB_SOURCE_ARCHIVE
        ):
            continue
        if not is_entry_selected_for_deploy(entry):
            continue
        matched = _match_selected_library_archive(
            library, entry, discovered_by_name
        )
        if matched is None:
            continue
        key = matched.name.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(matched)
    return selected


def _reset_extract_dir(path: Path) -> None:
    """Drop stale unzip contents so a new checkbox selection cannot mix packs."""
    import shutil

    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError:
        logger.warning("WH3 unzip reset failed path=%s", path, exc_info=True)
    path.mkdir(parents=True, exist_ok=True)


def _unzip_dir(root: Path, ref: Wh3ModRef, archives: list[Path]) -> Path | None:
    stem = archives[0].stem if archives else ""
    name = english_unzip_stem(ref.title, stem)
    if not name:
        return None
    return root / f"{name}_unzip"


def _packs_to_lines(
    ref: Wh3ModRef,
    packs: list[Path],
    data_folder: str,
) -> list[Wh3PackLine]:
    data_resolved: Path | None = None
    if str(data_folder or "").strip():
        try:
            data_resolved = Path(data_folder).expanduser().resolve()
        except OSError:
            data_resolved = Path(data_folder)
    lines: list[Wh3PackLine] = []
    seen: set[str] = set()
    for pack in packs:
        key = pack.name.lower()
        if key in seen:
            continue
        seen.add(key)
        in_data = False
        if data_resolved is not None:
            try:
                in_data = pack.parent.resolve() == data_resolved
            except OSError:
                in_data = False
        lines.append(
            Wh3PackLine(
                pack_name=pack.name,
                directory=str(pack.parent),
                in_data=in_data,
                internal_id=ref.internal_id,
            )
        )
    return lines


def _extract_archives_to(archives: list[Path], dest: Path) -> str:
    """Extract into *dest* using the existing ArchiveExtractor. Empty = success."""
    from services.deploy_apply import extract_archive_via_core

    dest.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        result, status = extract_archive_via_core(archive, dest)
        if not result.success:
            if getattr(result, "status", None) == status.TIMEOUT:
                return result.error or "压缩包解压超时"
            return result.error or f"压缩包解压失败：{archive.name}"
    return ""


def prepare_wh3_workshop_packs(
    ref: Wh3ModRef,
    *,
    workshop_path: str = "",
    data_folder: str = "",
    extract: bool = False,
    db: DatabaseManager | None = None,
) -> tuple[list[Wh3PackLine], str]:
    """
    Locate ``.pack`` files under the Steam Workshop tree.

    Case A: ``<workshop>/<workspace_id>/*.pack``
    Case B: extract library archives into ``<workshop>/<EnglishName>_unzip``

    Never uses the Mod Manager Chinese library path as a load directory.
    ``extract=True`` only during deploy; enable/sort reuse an existing unzip.
    Multi-archive Mods extract only ``selected_for_deploy`` files.
    """
    root = workshop_content_root(workshop_path)
    try:
        root_ok = root is not None and root.is_dir()
    except OSError:
        root_ok = False
    if not root_ok:
        if extract:
            return [], WH3_WORKSHOP_MISSING
        return [], ""

    assert root is not None
    native = None
    wid = str(ref.workspace_id or "").strip()
    if wid:
        candidate = root / wid
        try:
            if candidate.is_dir():
                native = candidate
        except OSError:
            native = None
    native_packs = _list_packs(native) if native is not None else []
    if native_packs:
        return _packs_to_lines(ref, native_packs, data_folder), ""

    library = local_mod_directory(ref)
    discovered = _list_library_archives(library) if library is not None else []
    archives = _filter_wh3_selected_archives(
        ref, library, discovered, db=db
    )
    unzip = _unzip_dir(root, ref, archives)
    unzip_packs = _list_packs(unzip) if unzip is not None else []
    # Single-archive / enable-sort: reuse unzip. Multi-archive deploy must
    # extract the current checkbox selection instead of leftover packs.
    reuse_unzip = (not extract) or len(discovered) <= 1
    if reuse_unzip and unzip_packs:
        return _packs_to_lines(ref, unzip_packs, data_folder), ""

    if extract and archives and unzip is not None:
        if len(discovered) > 1:
            _reset_extract_dir(unzip)
        err = _extract_archives_to(archives, unzip)
        if err:
            return [], err
        unzip_packs = _list_packs(unzip)
        if unzip_packs:
            return _packs_to_lines(ref, unzip_packs, data_folder), ""
        return [], WH3_MISSING_PACK

    if extract:
        if not wid or native is None:
            return [], WH3_WORKSHOP_MOD_MISSING
        return [], WH3_MISSING_PACK
    return [], ""


def resolve_pack_lines(
    ref: Wh3ModRef,
    *,
    workshop_path: str = "",
    data_folder: str = "",
    db: DatabaseManager | None = None,
) -> list[Wh3PackLine]:
    """used_mods lookup: Workshop packs only, never extract, never library path."""
    lines, _error = prepare_wh3_workshop_packs(
        ref,
        workshop_path=workshop_path,
        data_folder=data_folder,
        extract=False,
        db=db,
    )
    return lines


def _native_dir(path: str) -> str:
    return os.path.normpath(str(path or "").strip())


def render_used_mods_text(lines: list[Wh3PackLine]) -> str:
    """Build ``used_mods.txt`` body. Pack names only — never identity tokens."""
    working: list[str] = []
    seen_dirs: set[str] = set()
    for line in lines:
        if line.in_data:
            continue
        directory = _native_dir(line.directory)
        if not directory or directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        working.append(f'add_working_directory "{directory}";')
    seen_names: set[str] = set()
    mods: list[str] = []
    for line in lines:
        name = str(line.pack_name or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        mods.append(f'mod "{name}";')
    return "\n".join(working + mods)


def collect_enabled_pack_lines(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
    paths: Wh3GamePaths | None = None,
) -> list[Wh3PackLine]:
    database = db if db is not None else get_db()
    game_paths = paths if paths is not None else load_wh3_game_paths(database)
    by_id = {
        ref.internal_id: ref
        for ref in list_deployed_wh3_mods(database, library_root=library_root)
    }
    order = merge_deployed_order(load_saved_order(), list(by_id.keys()))
    lines: list[Wh3PackLine] = []
    for mid in order:
        ref = by_id.get(mid)
        if ref is None or not ref.enabled:
            continue
        lines.extend(
            resolve_pack_lines(
                ref,
                workshop_path=game_paths.workshop_path,
                data_folder=game_paths.data_folder,
                db=database,
            )
        )
    return lines


def complete_wh3_deploy_activation(
    internal_id: str,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> None:
    """After deploy_status=deployed: enable, append load order, rewrite used_mods.txt."""
    database = db if db is not None else get_db()
    mid = canon_internal_id(internal_id)
    if mid.isdigit():
        database.enable_mod(mid)
    persist_load_order(load_saved_order(), db=database, library_root=library_root)
    sync_used_mods_txt(database, library_root=library_root)


def complete_wh3_undeploy_activation(
    internal_id: str,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> None:
    """After deploy_status is cleared: drop load-order id and rewrite used_mods.txt."""
    del internal_id
    database = db if db is not None else get_db()
    persist_load_order(load_saved_order(), db=database, library_root=library_root)
    sync_used_mods_txt(database, library_root=library_root)


def used_mods_path(install_path: str | Path) -> Path:
    return Path(install_path).expanduser() / USED_MODS_FILENAME


def sync_used_mods_txt(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
    install_path: str | Path | None = None,
) -> tuple[Path | None, bool]:
    """Write ``used_mods.txt`` when content changed. Returns (path, wrote)."""
    database = db if db is not None else get_db()
    game_paths = load_wh3_game_paths(database)
    install = str(install_path or game_paths.install_path or "").strip()
    if not install:
        logger.warning("WH3 used_mods.txt skipped: install_path is empty")
        return None, False
    target = used_mods_path(install)
    text = render_used_mods_text(
        collect_enabled_pack_lines(
            database, library_root=library_root, paths=game_paths
        )
    )
    existing = None
    try:
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing == text:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, True


def launch_wh3(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
    spawn: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """Sync ``used_mods.txt`` then start ``Warhammer3.exe used_mods.txt;``."""
    database = db if db is not None else get_db()
    game_paths = load_wh3_game_paths(database)
    install = str(game_paths.install_path or "").strip()
    if not install:
        return False, "WH3 install_path is not configured."
    exe = Path(install) / WH3_EXE_NAME
    if not exe.is_file():
        return False, f"Game executable not found: {exe}"
    sync_used_mods_txt(database, library_root=library_root, install_path=install)
    runner = spawn if spawn is not None else subprocess.Popen
    kwargs: dict[str, Any] = {
        "cwd": str(Path(install)),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    try:
        runner([str(exe), WH3_LAUNCH_ARG], **kwargs)
    except TypeError:
        runner([str(exe), WH3_LAUNCH_ARG], cwd=str(Path(install)))
    except OSError as exc:
        return False, str(exc)
    return True, ""
