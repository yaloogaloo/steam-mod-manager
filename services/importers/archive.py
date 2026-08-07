"""Archive (zip/7z/rar) extract → mod-root resolve → existing importers."""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from core.paths import data_dir
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportResult, ModImporter
from services.importers.local_scanner import scan_mod_directory
from services.importers.nexus import NexusImporter, parse_nexus_id
from services.importers.steam import SteamImporter

logger = logging.getLogger(__name__)

IMPORT_CACHE_DIR_NAME = "import_cache"
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
_MOD_FILE_SUFFIXES = {".pak", ".dll", ".json", ".ini", ".cfg"}
_MOD_DIR_NAMES = {"logicmods", "paks", "content", "binaries"}
UNSUPPORTED_FMT_MSG = "当前系统不支持该压缩格式，请安装7-Zip"
NO_MOD_FILES_MSG = "压缩包中未找到 Mod 文件（.pak / .dll / .json / .ini 等）"


ProgressCallback = Callable[[str], None]


def import_cache_root() -> Path:
    path = data_dir() / IMPORT_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_import_cache(path: str | Path | None = None) -> bool:
    """
    Delete a temporary extract directory under ``data/import_cache/``.

    Only removes paths that resolve inside the import cache root.
    Returns True when something was removed.
    """
    if path is None:
        return False
    target = Path(path).expanduser().resolve()
    try:
        cache_root = import_cache_root().resolve()
    except OSError:
        return False
    if target == cache_root:
        return False
    try:
        target.relative_to(cache_root)
    except ValueError:
        return False
    if not target.exists():
        return False
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=False)
        else:
            target.unlink()
    except OSError as exc:
        logger.warning("Failed to cleanup import cache %s: %s", target, exc)
        return False
    return True


def find_7z_executable() -> str | None:
    """Return path to ``7z`` / ``7zz`` if available on PATH or common install dirs."""
    candidates = (
        "7z",
        "7z.exe",
        "7zz",
        "7za",
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        "/usr/bin/7z",
        "/usr/local/bin/7z",
        "/opt/homebrew/bin/7z",
    )
    for name in candidates:
        found = shutil.which(name) if "\\" not in name and "/" not in name else (
            name if Path(name).is_file() else None
        )
        if found:
            return found
    return None


def is_archive_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in ARCHIVE_SUFFIXES


def _dir_score(folder: Path) -> int:
    """Higher = more likely a Mod root."""
    score = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return -1
    for entry in entries:
        name_l = entry.name.lower()
        if entry.is_file():
            if entry.suffix.lower() in _MOD_FILE_SUFFIXES:
                score += 10
            if entry.suffix.lower() == ".pak":
                score += 5
        elif entry.is_dir():
            if name_l in _MOD_DIR_NAMES:
                score += 8
            if name_l == "logicmods":
                score += 12
    return score


def find_mod_root(extract_root: str | Path) -> Path | None:
    """
    Resolve the Mod content root under an extracted archive tree.

    Prefers directories that contain ``.pak`` / ``.dll`` / ``.json`` / ``.ini`` /
    ``LogicMods/``. Returns ``None`` when no Mod-like files exist.
    """
    root = Path(extract_root).expanduser().resolve()
    if not root.is_dir():
        return None

    candidates: list[tuple[int, int, Path]] = []  # (-score, depth, path)

    for path in root.rglob("*"):
        if path.is_dir() and path.name.lower() == "logicmods":
            parent = path.parent
            depth = len(parent.relative_to(root).parts)
            candidates.append((-(_dir_score(parent) + 30), depth, parent))
            continue
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in _MOD_FILE_SUFFIXES:
            continue
        parent = path.parent
        depth = len(parent.relative_to(root).parts)
        candidates.append((-_dir_score(parent), depth, parent))

    if not candidates:
        return None

    candidates.sort()
    best = candidates[0][2]

    # Single top-level wrapper folder (ModName/…) — prefer it when best is inside
    try:
        children = [p for p in root.iterdir() if not p.name.startswith(".")]
    except OSError:
        children = []
    if len(children) == 1 and children[0].is_dir():
        only = children[0].resolve()
        try:
            if best == root or best == only or only in best.parents or best == only:
                if best == root:
                    return only
        except Exception:  # noqa: BLE001
            pass

    return best


def extract_archive(
    archive_path: str | Path,
    *,
    dest_dir: str | Path | None = None,
) -> Path:
    """
    Extract *archive_path* into ``data/import_cache/{uuid}/`` (or *dest_dir*).

    ``.zip`` → stdlib ``zipfile``.
    ``.7z`` / ``.rar`` → external ``7z`` CLI.
    """
    src = Path(archive_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"压缩包不存在：{src}")
    suffix = src.suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        raise ValueError(f"不支持的压缩格式：{suffix}")

    out = Path(dest_dir) if dest_dir else (import_cache_root() / str(uuid.uuid4()))
    out.mkdir(parents=True, exist_ok=True)

    if suffix == ".zip":
        _extract_zip(src, out)
        return out

    seven = find_7z_executable()
    if not seven:
        raise RuntimeError(UNSUPPORTED_FMT_MSG)
    _extract_with_7z(seven, src, out)
    return out


def _extract_zip(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            # Zip-slip guard
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise RuntimeError(f"不安全的压缩包路径：{info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_fh, target.open("wb") as out_fh:
                shutil.copyfileobj(src_fh, out_fh)


def _extract_with_7z(seven: str, src: Path, dest: Path) -> None:
    cmd = [seven, "x", str(src), f"-o{dest}", "-y"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"7-Zip 解压失败：{exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"7-Zip 解压失败：{err}")


class ArchiveImporter(ModImporter):
    """
    Extract an archive, resolve Mod root, then delegate to platform importers.

    Does not reimplement Nexus/GitHub/Steam registration logic.
    """

    platform = "archive"

    def detect(self, value: str) -> bool:
        return is_archive_path(value)

    def prepare_source_folder(
        self,
        archive_path: str | Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[Path, Path]:
        """Extract + resolve root. Returns ``(mod_root, extract_dir)``. Raises on failure."""
        def _prog(msg: str) -> None:
            if on_progress is not None:
                on_progress(msg)

        _prog("正在解压...")
        extract_dir = extract_archive(archive_path)
        _prog("正在扫描Mod文件...")
        root = find_mod_root(extract_dir)
        if root is None:
            raise ValueError(NO_MOD_FILES_MSG)
        # Sanity: scanner should see at least one entry ideally
        bundle = scan_mod_directory(root)
        if not bundle.files and _dir_score(root) <= 0:
            raise ValueError(NO_MOD_FILES_MSG)
        return root, extract_dir

    def import_mod(
        self,
        *,
        archive_path: str | Path = "",
        platform: str = PLATFORM_NEXUS,
        library_root: str | Path = "",
        title: str = "",
        source_folder: str | Path = "",  # ignored when archive_path set
        on_progress: ProgressCallback | None = None,
        # platform-specific
        workshop_id: str = "",
        nexus_url: str = "",
        nexus_id: str = "",
        github_url: str = "",
        game_name: str = "",
        app_id: int = 0,
        **_kwargs: Any,
    ) -> ImportResult:
        archive = Path(str(archive_path or "")).expanduser()
        if not str(archive_path or "").strip():
            return ImportResult(success=False, error="缺少压缩包路径")
        extract_dir: Path | None = None
        try:
            resolved, extract_dir = self.prepare_source_folder(
                archive, on_progress=on_progress
            )
        except FileNotFoundError as exc:
            return ImportResult(success=False, error=str(exc))
        except ValueError as exc:
            return ImportResult(success=False, error=str(exc))
        except RuntimeError as exc:
            return ImportResult(success=False, error=str(exc))

        if on_progress is not None:
            on_progress("正在导入...")

        plat = str(platform or PLATFORM_NEXUS).strip().lower()
        db = self._database()
        name = (title or "").strip() or archive.stem
        context = _kwargs.get("context")
        cover_kwargs = {
            "cover_flat_roots": [archive.parent],
            "cover_search_roots": _kwargs.get("cover_search_roots"),
            "context": context,
        }

        if plat == PLATFORM_STEAM:
            result = SteamImporter(db=db).import_mod(
                workshop_id=workshop_id or archive.stem,
                title=name,
                source_folder=resolved,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                context=context,
            )
        elif plat == PLATFORM_GITHUB:
            result = GithubImporter(db=db).import_mod(
                github_url=github_url,
                source_folder=resolved,
                title=name,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                **cover_kwargs,
            )
        else:
            raw_url = nexus_url
            nid = nexus_id or parse_nexus_id(raw_url, "")
            result = NexusImporter(db=db).import_mod(
                source_folder=resolved,
                title=name,
                nexus_url=raw_url,
                nexus_id=nid,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                **cover_kwargs,
            )

        if result.success:
            self._finalize_successful_import(
                result=result,
                archive=archive,
                resolved=resolved,
                library_root=library_root,
                extract_dir=extract_dir,
            )
        # Failure: keep extract_dir for debug
        return result

    def _finalize_successful_import(
        self,
        *,
        result: ImportResult,
        archive: Path,
        resolved: Path,
        library_root: str | Path,
        extract_dir: Path | None,
    ) -> None:
        from services.importers.image_scanner import install_cover_from_source
        from services.importers.materialize import find_managed_mod_path

        dest: Path | None = None
        if result.managed_path:
            dest = Path(result.managed_path)
        elif library_root and result.mod_id:
            dest = find_managed_mod_path(library_root, result.mod_id)
        if dest is not None and dest.is_dir():
            install_cover_from_source(
                resolved,
                dest,
                extra_flat_roots=[archive.parent],
            )
            result.managed_path = str(dest)
        if extract_dir is not None:
            cleanup_import_cache(extract_dir)
