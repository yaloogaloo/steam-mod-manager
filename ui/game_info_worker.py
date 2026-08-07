"""Background worker that resolves Steam Store GameInfo for an AppID."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.game_info import GameInfo
from core.paths import data_dir
from core.steam_api import SteamWorkshopClient


class GameInfoWorker(QThread):
    """Fetch :class:`GameInfo` (+ optional local header image) off the UI thread."""

    # GameInfo, local cover path (str|None)
    finished_ok = Signal(object, object)
    finished_error = Signal(str)

    def __init__(self, app_id: int, parent=None) -> None:
        super().__init__(parent)
        self.app_id = int(app_id)

    def run(self) -> None:
        try:
            cover_path: str | None = None
            with SteamWorkshopClient() as client:
                info = client.get_game_info(self.app_id)
                if info.header_image:
                    headers_dir = data_dir() / "headers"
                    headers_dir.mkdir(parents=True, exist_ok=True)
                    target = headers_dir / f"{info.app_id}.jpg"
                    saved = client.download_preview(
                        info.header_image,
                        target,
                        overwrite=False if target.is_file() else True,
                    )
                    if saved and Path(saved).is_file():
                        cover_path = str(saved)
            self.finished_ok.emit(info, cover_path)
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))
