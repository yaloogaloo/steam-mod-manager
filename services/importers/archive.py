"""Archive (zip/7z/rar) import — Steam extracts; Nexus/GitHub keep archives as sources."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Sequence

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_MODIO,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_OTHER,
    normalize_platform,
)
from services.importers.local_scanner import is_history_version_path
from core.paths import data_dir, project_root
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportResult, ModImporter
from services.importers.modio import ModioImporter
from services.importers.nexus import NexusImporter, parse_nexus_id
from services.importers.other import OtherImporter
from services.importers.source_files import build_archive_source_entries
from services.importers.steam import SteamImporter

logger = logging.getLogger(__name__)

IMPORT_CACHE_DIR_NAME = "import_cache"
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
# Used only as a *hint* when choosing a nested Mod root — never as an import gate.
_MOD_FILE_SUFFIXES = {".pak", ".dll", ".json", ".ini", ".cfg"}
_MOD_DIR_NAMES = {"logicmods", "paks", "content", "binaries"}
UNSUPPORTED_FMT_MSG = "部署失败：不支持的压缩格式"
RAR_ERROR_PYTHON_SUPPORT_MISSING = "RAR_PYTHON_SUPPORT_MISSING"
RAR_ERROR_TOOL_UNAVAILABLE = "RAR_TOOL_UNAVAILABLE"
RAR_ERROR_EXECUTION_FAILED = "RAR_EXECUTION_FAILED"
RAR_ERROR_EXECUTABLE_INVALID = "RAR_EXECUTABLE_INVALID"
RAR_PYTHON_SUPPORT_MISSING_MSG = (
    "部署失败: 缺少 Python RAR 支持组件(rarfile)，请安装项目依赖"
)
RAR_TOOL_UNAVAILABLE_MSG = "部署失败: 未找到 RAR 解压执行组件(UnRAR)"
# Backward-compatible alias for older tests / persisted deploy_error strings.
TOOL_UNAVAILABLE_MSG = RAR_TOOL_UNAVAILABLE_MSG
EMPTY_ARCHIVE_MSG = "压缩包为空"
# Kept for older imports / tests that still reference the message string.
NO_MOD_FILES_MSG = "压缩包中未找到 Mod 文件（.pak / .dll / .json / .ini 等）"


ProgressCallback = Callable[[str], None]


class RarExtractError(RuntimeError):
    """RAR extract failure with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _raise_rar_python_support_missing(exc: ImportError) -> None:
    raise RarExtractError(
        RAR_ERROR_PYTHON_SUPPORT_MISSING,
        RAR_PYTHON_SUPPORT_MISSING_MSG,
    ) from exc


def _raise_rar_tool_unavailable(exc: BaseException | None = None) -> None:
    if exc is not None:
        raise RarExtractError(
            RAR_ERROR_TOOL_UNAVAILABLE,
            RAR_TOOL_UNAVAILABLE_MSG,
        ) from exc
    raise RarExtractError(RAR_ERROR_TOOL_UNAVAILABLE, RAR_TOOL_UNAVAILABLE_MSG)


def _raise_rar_executable_invalid(exc: BaseException | None = None) -> None:
    msg = "部署失败: RAR 解压执行组件无效或架构不兼容"
    if exc is not None:
        raise RarExtractError(RAR_ERROR_EXECUTABLE_INVALID, msg) from exc
    raise RarExtractError(RAR_ERROR_EXECUTABLE_INVALID, msg)


def _unrar_executable_usable(tool: Path) -> bool:
    """
    Reject placeholder / wrong-architecture UnRAR binaries before subprocess.

    Real bundled UnRAR is hundreds of KB; tiny ``MZ`` stubs fail with WinError 216.
    """
    try:
        if not tool.is_file():
            return False
        size = int(tool.stat().st_size)
        if size < 50_000:
            return False
        if platform.system() == "Windows":
            with tool.open("rb") as fh:
                if fh.read(2) != b"MZ":
                    return False
        return True
    except OSError:
        return False


def _raise_rar_execution_failed(
    detail: str,
    *,
    exc: BaseException | None = None,
) -> None:
    text = (detail or "").strip() or "未知错误"
    if exc is not None:
        raise RarExtractError(
            RAR_ERROR_EXECUTION_FAILED,
            f"RAR 部署失败: {text}",
        ) from exc
    raise RarExtractError(RAR_ERROR_EXECUTION_FAILED, f"RAR 部署失败: {text}")


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


def find_system_unrar_executable() -> str | None:
    """Return path to a system ``unrar`` / ``UnRAR`` binary on PATH, if any."""
    is_windows = platform.system() == "Windows"
    names = (
        "UnRAR.exe",
        "unrar.exe",
        "UnRAR",
        "unrar",
    ) if is_windows else ("unrar",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


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


def _runtime_base_paths() -> list[Path]:
    """Candidate app roots for bundled tools (dev + PyInstaller)."""
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
        try:
            bases.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
    bases.append(project_root())
    # services/importers/archive.py → parents[2] == repo root (dev fallback)
    bases.append(Path(__file__).resolve().parents[2])

    unique: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        try:
            key = str(base.resolve())
        except OSError:
            key = str(base)
        if key in seen:
            continue
        seen.add(key)
        unique.append(base)
    return unique


def resolve_bundled_unrar_tool() -> Path | None:
    """
    Absolute path to a bundled UnRAR binary, if present.

    Looks under ``bin/tools`` and ``bin`` for each runtime base path.
    Windows prefers ``UnRAR.exe`` / ``unrar.exe``; Unix uses ``unrar``.
    """
    is_windows = platform.system() == "Windows"
    names = (
        ("UnRAR.exe", "unrar.exe")
        if is_windows
        else ("unrar",)
    )
    for base in _runtime_base_paths():
        for folder in ("bin/tools", "bin"):
            for name in names:
                candidate = base / folder / name
                try:
                    if candidate.is_file() and _unrar_executable_usable(candidate):
                        return candidate.resolve()
                except OSError:
                    continue
    return None


def configure_rarfile_unrar_tool(rarfile_mod: Any) -> Path | None:
    """Set ``rarfile.UNRAR_TOOL`` to the bundled UnRAR path when available."""
    tool = resolve_bundled_unrar_tool()
    if tool is None:
        return None
    rarfile_mod.UNRAR_TOOL = str(tool)
    return tool


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
    Resolve the content root under an extracted archive tree.

    Prefers directories that contain ``.pak`` / ``.dll`` / ``.json`` / ``.ini`` /
    ``LogicMods/`` when present. When no known Mod extensions exist, falls
    back to a single wrapper folder or the extract root itself — import never
    requires known Mod file types.
    """
    from services.deploy_fs import safe_iter_dirs, safe_iter_files

    root = Path(extract_root).expanduser().resolve()
    if not root.is_dir():
        return None

    candidates: list[tuple[int, int, Path]] = []  # (-score, depth, path)

    for path in safe_iter_dirs(root, name="LogicMods"):
        if is_history_version_path(path):
            continue
        parent = path.parent
        if is_history_version_path(parent):
            continue
        depth = len(parent.relative_to(root).parts)
        candidates.append((-(_dir_score(parent) + 30), depth, parent))

    for path in safe_iter_files(root):
        if is_history_version_path(path):
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in _MOD_FILE_SUFFIXES:
            continue
        parent = path.parent
        if is_history_version_path(parent):
            continue
        depth = len(parent.relative_to(root).parts)
        candidates.append((-_dir_score(parent), depth, parent))

    if not candidates:
        return _fallback_content_root(root)

    candidates.sort()
    best = candidates[0][2]

    # Extension hits are only hints. If using *best* would drop sibling files
    # elsewhere under the natural content root, keep the broader root instead.
    natural = _fallback_content_root(root) or root
    if _has_files_outside(natural, best):
        return natural

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


def _has_files_outside(root: Path, inner: Path) -> bool:
    """True when *root* contains files that are not under *inner*."""
    from services.deploy_fs import safe_iter_files

    try:
        inner_r = inner.resolve()
        root_r = root.resolve()
    except OSError:
        return False
    if inner_r == root_r:
        return False
    try:
        for path in safe_iter_files(root_r):
            if is_history_version_path(path):
                continue
            if path.name.startswith("."):
                continue
            try:
                path.relative_to(inner_r)
            except ValueError:
                return True
    except OSError:
        return False
    return False


def _fallback_content_root(root: Path) -> Path | None:
    """
    Pick a content root when no known Mod extensions are present.

    Returns ``None`` only when the extract tree has no files at all.
    """
    from services.deploy_fs import safe_has_any_file

    try:
        has_files = safe_has_any_file(root)
    except OSError:
        return None
    if not has_files:
        return None
    try:
        children = [p for p in root.iterdir() if not p.name.startswith(".")]
    except OSError:
        return root
    if len(children) == 1 and children[0].is_dir():
        return children[0].resolve()
    return root


def extract_archive(
    archive_path: str | Path,
    *,
    dest_dir: str | Path | None = None,
) -> Path:
    """
    Extract *archive_path* into ``data/import_cache/{uuid}/`` (or *dest_dir*).

    ``.zip`` → stdlib ``zipfile``.
    ``.7z`` → system ``7z`` CLI when present, else ``py7zr``.
    ``.rar`` → bundled UnRAR, then system ``unrar``, then ``7z`` CLI fallback.
    """
    src = Path(archive_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"压缩包不存在：{src}")
    suffix = src.suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        raise ValueError(f"{UNSUPPORTED_FMT_MSG} {suffix}")

    out = Path(dest_dir) if dest_dir else (import_cache_root() / str(uuid.uuid4()))
    out.mkdir(parents=True, exist_ok=True)

    if suffix == ".zip":
        _extract_zip(src, out)
        return out

    if suffix == ".rar":
        _extract_rar(src, out)
        return out

    seven = find_7z_executable()
    if seven:
        _extract_with_7z(seven, src, out)
        return out

    if suffix == ".7z":
        _extract_7z_with_py7zr(src, out)
        return out

    raise RuntimeError(f"{UNSUPPORTED_FMT_MSG} {suffix}")


def _extract_zip(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            # 压缩包内「历史版本」路径一律不落盘
            if is_history_version_path(name):
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
        if src.suffix.lower() == ".rar":
            raise RuntimeError(f"RAR 部署失败: {exc}") from exc
        raise RuntimeError(f"解压失败：{exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        if src.suffix.lower() == ".rar":
            raise RuntimeError(_format_rar_failure(err)) from None
        raise RuntimeError(f"解压失败：{err}")


def _extract_7z_with_py7zr(src: Path, dest: Path) -> None:
    """Fallback 7z extract via py7zr — no user install / PATH setup."""
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - dependency declared in requirements
        raise RuntimeError(f"部署失败：缺少 7z 解压组件（py7zr）") from exc

    try:
        with py7zr.SevenZipFile(src, mode="r") as archive:
            archive.extractall(path=dest)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"解压失败：{exc}") from exc


def _format_rar_failure(detail: str) -> str:
    """Map raw 7-Zip RAR stderr to stable Chinese UI messages."""
    text = (detail or "").strip()
    low = text.lower()
    if (
        "need first volume" in low
        or "first volume" in low
        or ("volume" in low and ("missing" in low or "open" in low))
        or "分卷" in text
    ):
        return "解压失败：这是一个分卷压缩包，请导入第一卷"
    if not text:
        return "RAR 部署失败: 未知错误"
    return f"RAR 部署失败: {text}"


def _extract_rar(src: Path, dest: Path) -> None:
    """
    Extract ``.rar`` with backend priority:

    1. Project-bundled UnRAR (``bin/tools``) — subprocess with timeout
    2. System ``unrar`` on PATH — subprocess with timeout
    3. System 7-Zip CLI fallback
    4. rarfile + explicit UnRAR binary (last resort)
    """
    bundled = resolve_bundled_unrar_tool()
    if bundled is not None:
        try:
            _extract_rar_with_unrar_subprocess(bundled, src, dest)
            return
        except RarExtractError as exc:
            if exc.code != RAR_ERROR_EXECUTABLE_INVALID:
                raise
            logger.warning("bundled UnRAR unusable: %s", bundled)

    system = find_system_unrar_executable()
    if system is not None:
        system_path = Path(system)
        if _unrar_executable_usable(system_path):
            try:
                _extract_rar_with_unrar_subprocess(system_path, src, dest)
                return
            except RarExtractError as exc:
                if exc.code != RAR_ERROR_EXECUTABLE_INVALID:
                    raise
                logger.warning("system UnRAR unusable: %s", system_path)

    seven = find_7z_executable()
    if seven is not None:
        _extract_with_7z(seven, src, dest)
        return

    try:
        import rarfile  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        _raise_rar_python_support_missing(exc)
    _raise_rar_tool_unavailable()


def _extract_rar_with_unrar_subprocess(
    tool: Path | str,
    src: Path,
    dest: Path,
    *,
    timeout: int = 600,
) -> None:
    """Run UnRAR with a hard timeout — avoids rarfile blocking indefinitely."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [str(tool), "x", "-o+", "-inul", str(src), str(dest) + os.sep]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"RAR 部署失败: 解压超时（>{timeout}s）") from exc
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if winerr == 216 or "216" in str(exc):
            _raise_rar_executable_invalid(exc)
        _raise_rar_tool_unavailable(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(_format_rar_failure(err)) from None


def _extract_rar_with_rarfile(
    src: Path,
    dest: Path,
    *,
    unrar_tool: Path | str | None = None,
) -> None:
    """Extract ``.rar`` via rarfile + an explicit UnRAR binary."""
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover
        _raise_rar_python_support_missing(exc)

    tool = unrar_tool
    if tool is None:
        bundled = resolve_bundled_unrar_tool()
        if bundled is not None:
            tool = bundled
        else:
            system = find_system_unrar_executable()
            if system is not None:
                tool = system

    if tool is not None:
        rarfile.UNRAR_TOOL = str(tool)

    try:
        with rarfile.RarFile(src) as archive:
            archive.extractall(path=str(dest))
    except rarfile.NeedFirstVolume as exc:
        raise RuntimeError("解压失败：这是一个分卷压缩包，请导入第一卷") from exc
    except rarfile.RarCannotExec as exc:
        _raise_rar_tool_unavailable(exc)
    except FileNotFoundError as exc:
        _raise_rar_tool_unavailable(exc)
    except rarfile.BadRarFile as exc:
        _raise_rar_execution_failed(str(exc), exc=exc)
    except rarfile.PasswordRequired as exc:
        _raise_rar_execution_failed("压缩包已加密，需要密码", exc=exc)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or exc.__class__.__name__
        low = msg.lower()
        if "password" in low or "encrypted" in low:
            _raise_rar_execution_failed(msg, exc=exc)
        if "volume" in low or "first volume" in low:
            raise RuntimeError("解压失败：这是一个分卷压缩包，请导入第一卷") from exc
        _raise_rar_execution_failed(msg, exc=exc)


def normalize_archive_paths(
    archive_path: str | Path = "",
    archive_paths: Sequence[str | Path] | None = None,
) -> list[Path]:
    """Merge ``archive_paths`` / single ``archive_path`` into an ordered unique list."""
    ordered: list[Path] = []
    seen: set[str] = set()

    def _add(raw: str | Path) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        path = Path(text).expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            return
        seen.add(key)
        ordered.append(path)

    if archive_paths:
        for item in archive_paths:
            _add(item)
    if not ordered:
        _add(archive_path)
    return ordered


def _unique_stored_name(dest_dir: Path, original_name: str) -> str:
    candidate = original_name
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    n = 2
    while (dest_dir / candidate).exists():
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def stage_archive_sources(
    archives: Sequence[Path],
    *,
    on_progress: ProgressCallback | None = None,
) -> tuple[Path, list[dict[str, str]]]:
    """
    Copy archives into a staging folder (no extract).

    Returns ``(stage_dir, specs)`` where each spec has ``archive_name``,
    ``path`` (stored filename), and ``internal_path`` (empty for whole archive).
    """
    if not archives:
        raise ValueError("缺少压缩包路径")
    if on_progress is not None:
        on_progress("正在准备压缩包...")
    stage = import_cache_root() / str(uuid.uuid4())
    stage.mkdir(parents=True, exist_ok=False)
    specs: list[dict[str, str]] = []
    try:
        for src in archives:
            if not src.is_file():
                raise FileNotFoundError(f"压缩包不存在：{src}")
            if not is_archive_path(src):
                raise ValueError(f"不是支持的压缩包：{src.name}")
            original = src.name
            stored = _unique_stored_name(stage, original)
            shutil.copy2(src, stage / stored)
            specs.append(
                {
                    "archive_name": original,
                    "filename": stored,
                    "path": stored,
                    "internal_path": "",
                }
            )
    except Exception:
        cleanup_import_cache(stage)
        raise
    return stage, specs


class ArchiveImporter(ModImporter):
    """
    Archive import bridge.

    - Steam Workshop: extract → resolve Mod root → SteamImporter (unchanged).
    - Nexus / GitHub: keep each archive as a FileEntry source unit (no flatten).
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
        _prog("正在扫描文件...")
        root = find_mod_root(extract_dir)
        if root is None:
            # Empty archive → still import as an empty Mod folder (missing content).
            return extract_dir, extract_dir
        # Import does not validate "is this a Mod" — any files become FileEntry later.
        return root, extract_dir

    def import_mod(
        self,
        *,
        archive_path: str | Path = "",
        archive_paths: Sequence[str | Path] | None = None,
        platform: str = PLATFORM_NEXUS,
        library_root: str | Path = "",
        title: str = "",
        source_folder: str | Path = "",  # ignored when archives set
        on_progress: ProgressCallback | None = None,
        # platform-specific
        workshop_id: str = "",
        nexus_url: str = "",
        nexus_id: str = "",
        github_url: str = "",
        modio_url: str = "",
        modio_id: str = "",
        source_url: str = "",
        game_name: str = "",
        app_id: int = 0,
        **_kwargs: Any,
    ) -> ImportResult:
        del source_folder
        archives = normalize_archive_paths(archive_path, archive_paths)
        if not archives:
            return ImportResult(success=False, error="缺少压缩包路径")

        plat = normalize_platform(platform)
        if plat == PLATFORM_STEAM:
            return self._import_steam_extracted(
                archives=archives,
                workshop_id=workshop_id,
                title=title,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                on_progress=on_progress,
                **_kwargs,
            )
        return self._import_nexus_github_archives(
            archives=archives,
            platform=plat,
            title=title,
            library_root=library_root,
            nexus_url=nexus_url,
            nexus_id=nexus_id,
            github_url=github_url,
            modio_url=modio_url,
            modio_id=modio_id,
            source_url=source_url,
            game_name=game_name,
            app_id=app_id,
            on_progress=on_progress,
            **_kwargs,
        )

    def _import_steam_extracted(
        self,
        *,
        archives: list[Path],
        workshop_id: str,
        title: str,
        library_root: str | Path,
        game_name: str,
        app_id: int,
        on_progress: ProgressCallback | None,
        **_kwargs: Any,
    ) -> ImportResult:
        # Steam Workshop archive path unchanged: single extract → folder import.
        # Duplicate check MUST run before extract / materialize.
        from core.mod_platform import steam_workshop_url
        from services.importers.duplicate_check import check_import_duplicate

        mid = str(workshop_id or archives[0].stem or "").strip()
        if mid.isdigit() or "id=" in mid:
            probe = mid
            if not probe.isdigit() and "id=" in probe:
                probe = probe.split("id=", 1)[-1].split("&", 1)[0].strip()
            if probe.isdigit():
                dup = check_import_duplicate(
                    self._database(),
                    platform=PLATFORM_STEAM,
                    workshop_id=probe,
                    external_id=probe,
                    source_url=steam_workshop_url(probe),
                    app_id=int(app_id or 0),
                )
                if dup is not None:
                    return dup

        archive = archives[0]
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

        db = self._database()
        name = (title or "").strip() or archive.stem
        cover_source = _kwargs.get("cover_source") or _kwargs.get("cover_path")
        result = SteamImporter(db=db).import_mod(
            workshop_id=workshop_id or archive.stem,
            title=name,
            source_folder=resolved,
            library_root=library_root,
            game_name=game_name,
            app_id=app_id,
            cover_source=cover_source,
            context=_kwargs.get("context"),
        )
        if result.success:
            self._finalize_successful_import(
                result=result,
                library_root=library_root,
                staging_dir=extract_dir,
                cover_source=cover_source,
            )
        return result

    def _import_nexus_github_archives(
        self,
        *,
        archives: list[Path],
        platform: str,
        title: str,
        library_root: str | Path,
        nexus_url: str,
        nexus_id: str,
        github_url: str,
        modio_url: str,
        modio_id: str,
        game_name: str,
        app_id: int,
        on_progress: ProgressCallback | None,
        source_url: str = "",
        **_kwargs: Any,
    ) -> ImportResult:
        # Duplicate check MUST run before archive staging / extract.
        from services.importers.duplicate_check import check_import_duplicate

        db = self._database()
        if platform == PLATFORM_GITHUB:
            from services.importers.github import GithubImporter as _Gh

            url = str(github_url or "").strip()
            if url:
                repo = _Gh.parse_repo(url)
                canonical = url if url.startswith("http") else f"https://github.com/{repo}"
                dup = check_import_duplicate(
                    db,
                    platform=PLATFORM_GITHUB,
                    external_id=repo,
                    source_url=canonical,
                    app_id=int(app_id or 0),
                )
                if dup is not None:
                    return dup
        elif platform == PLATFORM_MODIO:
            from services.importers.modio import parse_modio_id

            ext = parse_modio_id(modio_url, modio_id) or str(modio_id or "").strip()
            dup = check_import_duplicate(
                db,
                platform=PLATFORM_MODIO,
                external_id=ext,
                source_url=str(modio_url or "").strip(),
                app_id=int(app_id or 0),
            )
            if dup is not None:
                return dup
        elif platform == PLATFORM_OTHER:
            url = str(source_url or "").strip()
            if url:
                dup = check_import_duplicate(
                    db,
                    platform=PLATFORM_OTHER,
                    source_url=url,
                    app_id=int(app_id or 0),
                )
                if dup is not None:
                    return dup
        else:
            raw_url = str(nexus_url or "").strip()
            nid = str(nexus_id or parse_nexus_id(raw_url, "") or "").strip()
            dup = check_import_duplicate(
                db,
                platform=PLATFORM_NEXUS,
                external_id=nid,
                source_url=raw_url,
                app_id=int(app_id or 0),
            )
            if dup is not None:
                return dup

        staging_dir: Path | None = None
        try:
            staging_dir, specs = stage_archive_sources(
                archives, on_progress=on_progress
            )
        except FileNotFoundError as exc:
            return ImportResult(success=False, error=str(exc))
        except ValueError as exc:
            return ImportResult(success=False, error=str(exc))
        except OSError as exc:
            return ImportResult(success=False, error=str(exc))

        if on_progress is not None:
            on_progress("正在导入...")

        if platform == PLATFORM_GITHUB:
            source_type = SOURCE_TYPE_GITHUB
        elif platform == PLATFORM_MODIO:
            source_type = SOURCE_TYPE_MODIO
        elif platform == PLATFORM_OTHER:
            source_type = SOURCE_TYPE_OTHER
        else:
            source_type = SOURCE_TYPE_NEXUS
        file_entries = build_archive_source_entries(specs, source_type=source_type)
        db = self._database()
        name = (title or "").strip() or archives[0].stem
        cover_source = _kwargs.get("cover_source") or _kwargs.get("cover_path")
        cover_kwargs = {
            "cover_source": cover_source,
            "context": _kwargs.get("context"),
            "file_entries": file_entries,
        }

        if platform == PLATFORM_GITHUB:
            result = GithubImporter(db=db).import_mod(
                github_url=github_url,
                source_folder=staging_dir,
                title=name,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                **cover_kwargs,
            )
        elif platform == PLATFORM_MODIO:
            result = ModioImporter(db=db).import_mod(
                source_folder=staging_dir,
                title=name,
                modio_url=modio_url,
                modio_id=modio_id,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                **cover_kwargs,
            )
        elif platform == PLATFORM_OTHER:
            result = OtherImporter(db=db).import_mod(
                source_folder=staging_dir,
                title=name,
                source_url=source_url
                or str(_kwargs.get("other_url") or "").strip(),
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                external_id_suffix=archives[0].stem,
                **cover_kwargs,
            )
        else:
            raw_url = nexus_url
            nid = nexus_id or parse_nexus_id(raw_url, "")
            local_archive = not str(nid or "").strip()
            if local_archive:
                nid = archives[0].stem
            result = NexusImporter(db=db).import_mod(
                source_folder=staging_dir,
                title=name,
                nexus_url=raw_url,
                nexus_id=nid,
                library_root=library_root,
                game_name=game_name,
                app_id=app_id,
                is_batch_mode=local_archive,
                **cover_kwargs,
            )

        if result.success:
            self._finalize_successful_import(
                result=result,
                library_root=library_root,
                staging_dir=staging_dir,
                cover_source=cover_source,
            )
        # Failure: keep staging for debug
        return result

    def _finalize_successful_import(
        self,
        *,
        result: ImportResult,
        library_root: str | Path,
        staging_dir: Path | None,
        cover_source: str | Path | None = None,
    ) -> None:
        from services.importers.image_picker import apply_cover_to_mod
        from services.importers.materialize import find_managed_mod_path

        dest: Path | None = None
        if result.managed_path:
            dest = Path(result.managed_path)
        elif library_root and result.mod_id:
            dest = find_managed_mod_path(library_root, result.mod_id)
        if dest is not None and dest.is_dir() and cover_source:
            apply_cover_to_mod(
                dest, cover_source, mod_id=result.mod_id, update_db=True,
                mark_user_override=False,
            )
            result.managed_path = str(dest)
        elif dest is not None:
            result.managed_path = str(dest)
        if dest is not None and dest.is_dir():
            try:
                from services.file_ops import apply_missing_content_marker

                apply_missing_content_marker(dest)
            except Exception:  # noqa: BLE001
                pass
        if staging_dir is not None:
            cleanup_import_cache(staging_dir)
