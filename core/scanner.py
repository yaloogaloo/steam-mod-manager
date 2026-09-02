"""Scan Steam Workshop download directories for numeric Mod IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Steam workshop item folders are pure decimal IDs
_MOD_ID_PATTERN = re.compile(r"^\d+$")


@dataclass(frozen=True)
class ScannedMod:
    """A Mod folder discovered under the Steam workshop content tree."""

    published_file_id: str
    path: Path
    # Single stat() on the workshop mod root at scan time. Fallback only when the
    # Steam API returned no row for this mod; never overrides explicit API timestamps.
    source_dir_mtime: int | None = None


class WorkshopScanner:
    """
    Scan a Steam workshop content directory for Mod folders.

    Typical Steam layout::

        <Steam>/steamapps/workshop/content/<appid>/<publishedfileid>/

    Users may point either at ``content`` (all games), a single ``<appid>``
    folder, or a flat folder of numeric IDs. This scanner accepts all three.
    """

    def __init__(self, workshop_root: str | Path) -> None:
        self.workshop_root = Path(workshop_root).expanduser().resolve()

    def validate_root(self) -> None:
        if not self.workshop_root.exists():
            raise FileNotFoundError(f"Workshop directory not found: {self.workshop_root}")
        if not self.workshop_root.is_dir():
            raise NotADirectoryError(f"Not a directory: {self.workshop_root}")

    def scan(self, *, recursive: bool = True) -> list[ScannedMod]:
        """
        Return all Mod folders under the configured root.

        When *recursive* is True (default), also look one level deeper so a
        ``content`` directory containing ``<appid>/<id>`` trees is supported.
        Duplicate IDs keep the first path encountered.
        """
        self.validate_root()

        found: dict[str, ScannedMod] = {}

        # Direct children that look like Mod IDs
        for child in self._iter_mod_dirs(self.workshop_root):
            found.setdefault(
                child.published_file_id,
                child,
            )

        if recursive:
            # e.g. content/<appid>/<modid>
            for app_dir in sorted(self.workshop_root.iterdir()):
                if not app_dir.is_dir():
                    continue
                if _MOD_ID_PATTERN.match(app_dir.name):
                    # Already counted as a mod folder; still scan inside
                    # only if it itself looks like an app-id parent with
                    # nested numeric children (rare but harmless).
                    pass
                for child in self._iter_mod_dirs(app_dir):
                    found.setdefault(child.published_file_id, child)

        return sorted(found.values(), key=lambda m: int(m.published_file_id))

    def list_ids(self, *, recursive: bool = True) -> list[str]:
        """Convenience: return only published file IDs."""
        return [m.published_file_id for m in self.scan(recursive=recursive)]

    @staticmethod
    def _iter_mod_dirs(directory: Path) -> list[ScannedMod]:
        results: list[ScannedMod] = []
        try:
            entries = list(directory.iterdir())
        except PermissionError:
            return results

        for entry in entries:
            if entry.is_dir() and _MOD_ID_PATTERN.match(entry.name):
                mtime: int | None = None
                try:
                    raw = int(entry.stat().st_mtime)
                    mtime = raw if raw > 0 else None
                except OSError:
                    mtime = None
                results.append(
                    ScannedMod(
                        published_file_id=entry.name,
                        path=entry,
                        source_dir_mtime=mtime,
                    )
                )
        return results

    @staticmethod
    def is_mod_id(name: str) -> bool:
        return bool(_MOD_ID_PATTERN.match(name))
