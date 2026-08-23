"""In-library Mod detail panel — view / edit modes (non-modal QWidget)."""

from __future__ import annotations

import html as html_module
import os
import re
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    RELATIONSHIP_ADDON,
    RELATIONSHIP_CONFLICT,
    RELATIONSHIP_DEPENDENCY,
    RELATIONSHIP_PATCH,
    TAG_TYPE_CONFLICT,
    TAG_TYPE_INVALID,
    ModDisplayInfo,
    get_db,
)

TAG_TYPE_ABANDONED = "abandoned"

from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_NONE,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    PROVIDER_NEXUS_MANUAL_IMPORT,
    format_offline_provider,
    is_entry_selected_for_deploy,
    normalize_offline_status,
    normalize_platform,
    supports_offline_page_download,
)
from ui.mod_files_ux import (
    count_selected,
    file_badge_kind,
    file_combo_label,
    file_description,
    file_list_badge,
    file_primary_label,
    file_secondary_label,
    files_summary_lines,
    sort_files_for_detail,
    sort_files_for_nexus_flat,
)
from services.importers.local_scanner import (
    filter_out_history_version_entries,
    is_history_version_entry,
)
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
    ModStatus,
)
from core.models import ModMetadata
from services.file_ops import ModFileManager
from services.mod_files import ModFileManager as ModFilesJsonManager
from ui.platform_labels import (
    format_external_id,
    format_mod_info_clipboard,
    format_platform_name,
    get_platform_metadata_labels,
)


from ui.styles import (
    ACCENT_ERROR,
    ACCENT_PRIMARY,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    BACKGROUND_BUTTON_PRESSED,
    BORDER_STRONG,
    PANEL_STYLE,
    PLATFORM_GITHUB_BG,
    PLATFORM_GITHUB_BORDER,
    PLATFORM_GITHUB_FG,
    PLATFORM_MODIO_BG,
    PLATFORM_MODIO_BORDER,
    PLATFORM_MODIO_FG,
    PLATFORM_NEXUS_BG,
    PLATFORM_NEXUS_BORDER,
    PLATFORM_NEXUS_FG,
    PLATFORM_OTHER_BG,
    PLATFORM_OTHER_BORDER,
    PLATFORM_OTHER_FG,
    PLATFORM_STEAM_BG,
    PLATFORM_STEAM_BORDER,
    PLATFORM_STEAM_FG,
    TEXT_SECONDARY,
)
from ui.edit_mod_dialog import EditModDialog


class ElideLabel(QLabel):
    """Single-line label: paint elided text; full string only via native tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        # Ignored: layout may shrink below text sizeHint; Fixed edit stays visible.
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.setMinimumWidth(0)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full_text = str(text or "")
        try:
            from ui.popup_trace import log_popup

            log_popup(
                "ElideLabel.setToolTip",
                detail=repr(self._full_text[:80]),
            )
        except Exception:  # noqa: BLE001
            pass
        self.setToolTip(self._full_text)
        # Keep logical text for readers/tests; paintEvent draws ElideRight.
        super().setText(self._full_text)
        self.update()

    def fullText(self) -> str:
        return self._full_text

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        return QSize(40, metrics.height() + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        return QSize(0, metrics.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.width())
        )
        color = self.palette().color(self.foregroundRole())
        painter.setPen(color)
        align = self.alignment()
        if not int(align):
            align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(self.contentsRect(), int(align), elided)


# Back-compat alias used by older call sites / tests.
_ElidedLabel = ElideLabel


COVER_W = 140
COVER_H = 140

MODE_EMPTY = 0
MODE_VIEW = 1
MODE_EDIT = 2


def humanize_deploy_error(error: str) -> str:
    """Map deployer error strings to concise UI copy (keep concrete reasons)."""
    text = (error or "").strip()
    if not text:
        return "未知错误"
    if text in (
        "内容目录不存在，无法部署",
        "Mod 安装目录不存在，请检查游戏设置",
        "目标目录已存在其他内容，无法部署",
        "无法写入游戏 Mod 目录，请检查权限",
        "部署失败：文件复制错误",
        "部署清单不匹配，无法安全删除部署",
        "Backup 无效，无法部署",
        "身份冲突，无法部署",
        "该 Mod 内容缺失，无法部署",
    ):
        return text
    if text == "Target mod directory does not exist" or text.startswith(
        "Target mod directory does not exist"
    ):
        return "Mod 安装目录不存在，请检查游戏设置"
    if text.startswith("Permission denied"):
        return "无法写入游戏 Mod 目录，请检查权限"
    if "请先配置游戏部署目录" in text or (
        "部署目录" in text and "配置" in text
    ):
        return "请先配置游戏部署目录"
    if "源 Mod 目录不存在" in text or "源文件不存在" in text:
        return "内容目录不存在，无法部署"
    if text.startswith("复制失败") or "复制失败" in text:
        return "部署失败：文件复制错误"
    if text == "Unknown deploy error":
        return "未知错误"
    # Preserve archive / extract details (suffix, tool unavailable, etc.).
    return text


_DESC_HTML_HINT = re.compile(
    r"<(?:p|br\s*/?|em|i|b|strong|u|ul|ol|li|div|span|h[1-6]|blockquote)\b",
    re.IGNORECASE,
)
_DESC_TAG_RE = re.compile(
    r"</?([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>",
    re.IGNORECASE,
)
_DESC_ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "b",
        "i",
        "em",
        "strong",
        "u",
        "ul",
        "ol",
        "li",
        "div",
        "span",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "hr",
        "pre",
        "code",
        "a",
    }
)
_DESC_VOID_TAGS = frozenset({"br", "hr"})
_DESC_BLOCK_DANGER = re.compile(
    r"<(script|style|iframe|object|embed|link|meta|form)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DESC_SELF_DANGER = re.compile(
    r"<(script|style|iframe|object|embed|link|meta|form)\b[^>]*/?>",
    re.IGNORECASE,
)


def _format_description_rich_html(text: str) -> str:
    """
    Render description for QLabel RichText.

    HTML descriptions (mod.io / Steam) keep a safe tag subset; plain text is
    escaped with newlines → ``<br>``.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if _DESC_HTML_HINT.search(raw):
        return _sanitize_description_html(raw)
    return html_module.escape(raw).replace("\n", "<br>")


def _sanitize_description_html(html: str) -> str:
    """Allow lightweight markup; strip scripts and unsafe attributes."""
    cleaned = _DESC_BLOCK_DANGER.sub("", html)
    cleaned = _DESC_SELF_DANGER.sub("", cleaned)

    def _replace(match: re.Match[str]) -> str:
        full = match.group(0)
        name = str(match.group(1) or "").lower()
        attrs = str(match.group(2) or "")
        closing = full.lstrip().startswith("</")
        if name not in _DESC_ALLOWED_TAGS:
            return html_module.escape(full)
        if closing:
            return f"</{name}>"
        if name in _DESC_VOID_TAGS:
            return f"<{name}/>"
        if name == "a":
            href_m = re.search(
                r"""href\s*=\s*(['"])(.*?)\1""", attrs, flags=re.IGNORECASE
            )
            href = (href_m.group(2) if href_m else "").strip()
            if href.lower().startswith(("http://", "https://", "steam://")):
                safe = html_module.escape(href, quote=True)
                return f'<a href="{safe}">'
            return "<a>"
        if name == "p":
            return '<p style="margin:0.55em 0;">'
        if name == "em" or name == "i":
            return f"<{name} style='font-style:italic;'>"
        return f"<{name}>"

    return _DESC_TAG_RE.sub(_replace, cleaned)


def get_directory_size(path: Path) -> int:
    """Sum file sizes under *path* (bytes), skipping offline/assets/.cache."""
    from services.dir_size import directory_size

    return directory_size(path)


def format_size(size_bytes: int) -> str:
    """Human-readable size (B / KB / MB / GB)."""
    n = max(0, int(size_bytes or 0))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


_LEADING_HTML_BLANK = re.compile(
    r"^(?:\s|&nbsp;|<br\s*/?>|<(?:p|div)[^>]*>\s*</(?:p|div)>)+",
    re.IGNORECASE,
)


def _strip_leading_html_blank(html: str) -> str:
    """Remove leading blank lines / empty blocks so content starts at the top."""
    text = str(html or "")
    while True:
        nxt = _LEADING_HTML_BLANK.sub("", text, count=1)
        if nxt == text:
            return text.lstrip()
        text = nxt


def _insert_zwsp_breaks(text: str, every: int = 8) -> str:
    """Insert zero-width spaces so unbroken Latin runs can wrap."""
    raw = str(text or "")
    if not raw:
        return ""
    out: list[str] = []
    run = 0
    for ch in raw:
        out.append(ch)
        if ch.isspace() or ch in "-_/\\.":
            run = 0
            continue
        run += 1
        if run >= every:
            out.append("\u200b")
            run = 0
    return "".join(out)


class ModDetailPanel(QWidget):
    """
    Reusable right-hand workspace panel for Mod Library.

    Created once; call ``show_mod`` / ``clear`` / ``enter_edit`` to update.
    Never opens QDialog; never triggers Steam archive downloads.
    Deploy work is requested via ``deploy_requested`` (library starts QThread).
    """

    metadata_saved = Signal(object)  # Path managed_path after successful save
    tags_saved = Signal(object)  # Path after user tags / conflicts saved
    batch_platform_saved = Signal(object)  # list[str] mod_ids after batch source save
    deploy_requested = Signal(str)  # mod_id — library_view starts DeployWorker
    redeploy_requested = Signal(str)
    undeploy_requested = Signal(str)
    remove_requested = Signal(str)  # mod_id — library confirms then removes
    offline_page_updated = Signal(object)  # Path managed_path after offline download
    relocate_completed = Signal(str)  # mod_id after successful path relocate

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modDetailPanel")
        self.setMinimumWidth(350)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._managed_path: Path | None = None
        self._library_root: Path | None = None
        self._folder_absent = False
        self._resolved = None
        self._metadata: ModMetadata | None = None
        self._display_info: ModDisplayInfo | None = None
        self._mode = MODE_EMPTY
        self._deploy_busy = False
        self._conflict_hint = ""
        self._tag_deploy_hint = ""
        self._peer_mods: list[tuple[str, str]] = []
        self._peer_candidates: list[dict] = []
        self._offline_worker = None
        self._metadata_worker = None
        self._metadata_progress_dialog = None
        self._refresh_btn_timer: QTimer | None = None
        self._refresh_btn_state = "idle"
        self._op_status_timer: QTimer | None = None
        self._current_platform = PLATFORM_STEAM
        self._source_url_value = ""
        self._batch_mod_ids: list[str] = []
        self._batch_game_name = ""
        self._batch_game_id = 0
        # (mod_id, managed_path, platform) for multi-select offline save
        self._batch_entries: list[tuple[str, Path, str]] = []
        self._offline_batch_queue: list[tuple[str, Path, str]] = []
        self._offline_batch_active = False
        self._offline_batch_errors: list[str] = []
        self._files_role_updating = False

        self._build_ui()
        self.clear()

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        hint.setWidth(min(max(hint.width(), 360), 420))
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        # Cap so long footer labels cannot force the panel past the splitter max.
        hint.setWidth(min(max(hint.width(), 350), 420))
        return hint

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_mod(
        self,
        managed_path: str | Path | None = None,
        *,
        mod_id: str | int | None = None,
    ) -> None:
        """Load metadata via the unified resolver (no Steam I/O)."""
        from ui.popup_trace import log_popup
        from services.mod_metadata_resolver import resolve_mod_metadata

        log_popup("slot:show_mod", detail=str(mod_id or managed_path or ""))
        self._batch_mod_ids = []
        self._batch_game_name = ""
        self._batch_game_id = 0
        self._batch_entries = []
        self._offline_batch_queue = []
        self._offline_batch_active = False
        self._offline_batch_errors = []
        if hasattr(self, "btn_edit_info"):
            self.btn_edit_info.setText("编辑信息")
            self.btn_edit_info.setToolTip("编辑显示名称、介绍与源链接")
            self.btn_edit_info.setEnabled(True)

        resolved = resolve_mod_metadata(mod_id, managed_path)
        path = Path(managed_path) if managed_path is not None else None
        if resolved is not None and resolved.managed_path:
            path = Path(resolved.managed_path)
        if path is None:
            self.clear()
            return
        self._managed_path = path
        try:
            self._library_root = self._managed_path.parents[1]
        except IndexError:
            self._library_root = self._managed_path.parent

        self._resolved = resolved
        self._folder_absent = bool(resolved is None or not resolved.folder_present)
        if resolved is not None:
            meta = resolved.to_mod_metadata()
        else:
            meta = ModMetadata(
                published_file_id=(
                    str(mod_id or "").strip()
                    or (
                        self._managed_path.name
                        if self._managed_path.name.isdigit()
                        else ""
                    )
                ),
                title=self._managed_path.name,
                managed_path=str(self._managed_path),
            )
        meta.managed_path = str(self._managed_path)
        meta.local_path = str(self._managed_path)
        self._metadata = meta

        self._display_info = None
        mid = str(mod_id or meta.published_file_id or "").strip()
        if str(mid).isdigit():
            try:
                self._display_info = get_db().get_mod_display_info(mid)
            except Exception:  # noqa: BLE001
                self._display_info = None

        self._stack.setCurrentWidget(self._view_page)
        self._mode = MODE_VIEW
        self._fill_view()
        self.setEnabled(True)

    def clear(self) -> None:
        """Empty / unselected state."""
        self._managed_path = None
        self._library_root = None
        self._folder_absent = False
        self._resolved = None
        self._metadata = None
        self._display_info = None
        self._batch_mod_ids = []
        self._batch_game_name = ""
        self._batch_game_id = 0
        self._batch_entries = []
        self._offline_batch_queue = []
        self._offline_batch_active = False
        self._offline_batch_errors = []
        self._mode = MODE_EMPTY
        self._deploy_busy = False
        self._stack.setCurrentWidget(self._empty_page)
        self.setEnabled(True)
        if hasattr(self, "btn_edit_info"):
            self.btn_edit_info.setText("编辑信息")
            self.btn_edit_info.setEnabled(False)
        self.btn_deploy.setEnabled(False)
        self.btn_deploy.setText("部署")
        self.btn_redeploy.setEnabled(False)
        self.btn_undeploy.setEnabled(False)
        if hasattr(self, "btn_download_offline"):
            self.btn_download_offline.setEnabled(False)
        if hasattr(self, "btn_relocate"):
            self.btn_relocate.hide()
            self.btn_relocate.setEnabled(False)
        if hasattr(self, "backup_status_badge"):
            self.backup_status_badge.hide()
            self.backup_status_badge.clear()
        if hasattr(self, "view_favorite"):
            self.view_favorite.hide()
            self.view_favorite.clear()
        if hasattr(self, "op_status_label"):
            self._clear_op_status()
        if hasattr(self, "btn_add_dependency"):
            self.btn_add_dependency.setEnabled(False)
        if hasattr(self, "dep_summary_label"):
            self.dep_summary_label.setText("依赖于 —")
        if hasattr(self, "_files_section_frame"):
            self._files_section_frame.hide()
        self.view_deploy.clear()
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self.view_deploy_type.clear()
        self.view_deploy_error.clear()
        self.view_deploy_conflict.clear()
        self._conflict_hint = ""
        self._tag_deploy_hint = ""
        self._peer_mods = []
        self._peer_candidates = []
        if hasattr(self, "view_tag_deploy_hint"):
            self.view_tag_deploy_hint.clear()
        if hasattr(self, "tag_conflict_list"):
            self.tag_conflict_list.clear()
        if hasattr(self, "tag_invalid_check"):
            self.tag_invalid_check.setChecked(False)
            self.tag_conflict_check.setChecked(False)
        if hasattr(self, "tag_invalid_reason"):
            self.tag_invalid_reason.clear()
        self._hide_status_banner()
        if hasattr(self, "status_reason_edit"):
            self._reset_status_widgets()
        if hasattr(self, "_rel_lists"):
            for lst in self._rel_lists.values():
                lst.clear()

    def enter_edit(self) -> None:
        """Open the Edit Mod dialog (display name / description / source URL)."""
        self.open_edit_info_dialog()

    def show_batch_selection(
        self,
        mod_ids: list[str],
        *,
        game_name: str = "",
        game_id: int = 0,
        platform: str = PLATFORM_STEAM,
        entries: list[tuple[str, str | Path, str]] | None = None,
    ) -> None:
        """Multi-select state: batch edit + optional batch offline save."""
        from core.mod_platform import normalize_platform

        ids = [str(m).strip() for m in mod_ids if str(m).strip()]
        if len(ids) <= 1:
            return
        self._batch_mod_ids = ids
        self._batch_game_name = str(game_name or "").strip()
        self._batch_game_id = int(game_id or 0)
        parsed: list[tuple[str, Path, str]] = []
        if entries:
            for mid, path, plat in entries:
                mid_s = str(mid or "").strip()
                if not mid_s:
                    continue
                parsed.append(
                    (mid_s, Path(path), normalize_platform(plat))
                )
        if not parsed:
            parsed = [(mid, Path(), normalize_platform(platform)) for mid in ids]
        self._batch_entries = parsed
        self._managed_path = None
        self._metadata = None
        self._display_info = None
        self._current_platform = normalize_platform(
            parsed[-1][2] if parsed else platform
        )
        # Library root from first entry that has a path.
        self._library_root = None
        for _mid, path, _plat in parsed:
            if path.parts:
                try:
                    self._library_root = path.parents[1]
                except IndexError:
                    self._library_root = path.parent
                break
        self._mode = MODE_VIEW
        self._stack.setCurrentWidget(self._view_page)
        self.setEnabled(True)

        self._set_header_title(f"已选 {len(ids)} 个 Mod")
        if hasattr(self, "view_name"):
            self.view_name.setText(f"已选 {len(ids)} 个 Mod")
        elif hasattr(self, "view_name_caption"):
            self.view_name_caption.setText(f"已选 {len(ids)} 个 Mod")
        if hasattr(self, "view_source_url"):
            self.view_source_url.setText("—")
        self._hide_status_banner()

        for btn in (
            getattr(self, "btn_folder", None),
            getattr(self, "btn_steam", None),
            getattr(self, "btn_offline", None),
            getattr(self, "btn_deploy", None),
            getattr(self, "btn_redeploy", None),
            getattr(self, "btn_undeploy", None),
        ):
            if btn is not None:
                btn.setEnabled(False)
        self.btn_edit_info.setText("批量编辑")
        self.btn_edit_info.setToolTip("批量修改所选 Mod 的来源平台")
        self.btn_edit_info.setEnabled(True)
        if hasattr(self, "btn_refresh_mod"):
            self._set_refresh_button_state("idle")

        # Enable「保存离线页面」when at least one selected Mod can auto-save.
        has_saveable = any(
            supports_offline_page_download(plat) and path.parts
            for _mid, path, plat in self._batch_entries
        )
        if hasattr(self, "btn_download_offline"):
            self.btn_download_offline.setText("保存离线页面")
            self.btn_download_offline.setToolTip(
                "批量保存离线页面（Nexus 等仅支持导入的来源将静默跳过）"
                if has_saveable
                else "所选 Mod 均不支持自动保存离线页面"
            )
            self.btn_download_offline.setAccessibleName("保存离线页面")
            self.btn_download_offline.setEnabled(has_saveable)

    def open_edit_info_dialog(self) -> None:
        """
        Popup editor for display metadata.

        Updates SQLite only — never renames the managed Mod folder.
        Batch mode updates ``platform`` only for every selected Mod.
        """
        if self._batch_mod_ids and len(self._batch_mod_ids) > 1:
            self._open_batch_edit_dialog()
            return
        if self._managed_path is None or self._metadata is None:
            return
        meta = self._metadata
        info = self._display_info
        mid = str(meta.published_file_id)
        steam = ""
        if info:
            steam = info.steam_name
        elif meta:
            steam = (meta.title or "").strip()
        managed_before = Path(self._managed_path)

        game_id = int(info.app_id) if info else 0
        game_name = str(meta.game_name or "").strip() if meta else ""
        if not game_name and game_id:
            try:
                game = get_db().get_game(game_id)
                if game is not None:
                    game_name = str(game.name or "").strip()
            except Exception:  # noqa: BLE001
                pass

        game_root = ""
        if game_id:
            try:
                cfg = get_db().get_game_deploy_config(game_id)
                if cfg is not None:
                    game_root = str(cfg.install_path or "").strip()
            except Exception:  # noqa: BLE001
                pass

        dlg = EditModDialog(
            self,
            mod_id=mid,
            display_name=info.user_display_name if info else "",
            steam_name=steam,
            description=info.custom_description if info else "",
            source_url=self._current_source_url(),
            platform=self._current_platform,
            game_name=game_name,
            game_id=game_id,
            game_root=game_root,
            custom_deploy_path=(
                info.custom_deploy_path if info else ""
            ),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.values()
        if not mid.isdigit():
            QMessageBox.warning(self, "保存失败", "缺少有效的 Mod ID。")
            return
        try:
            self._display_info = get_db().update_mod_user_metadata(
                mid,
                {
                    "display_name": values["display_name"],
                    "custom_description": values["custom_description"],
                    "user_notes": (info.user_notes if info else "")
                    or (meta.custom_notes or ""),
                    "favorite": bool(info.favorite) if info else False,
                    "platform": values["platform"],
                    "source_url": values["source_url"],
                    "custom_deploy_path": values.get("custom_deploy_path", ""),
                },
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        # Absolute red line: physical folder must stay put.
        if Path(self._managed_path) != managed_before or not managed_before.is_dir():
            QMessageBox.warning(
                self,
                "保存异常",
                "检测到目录路径变化，已中止刷新。显示名称不会重命名文件夹。",
            )
            return

        if self._resolved is not None:
            name = str(values.get("display_name") or "").strip()
            if name:
                self._resolved.display_name = name
            desc = str(values.get("custom_description") or "").strip()
            if desc:
                self._resolved.description = desc
            url = str(values.get("source_url") or "").strip()
            if url:
                self._resolved.source_url = url
            plat = str(values.get("platform") or "").strip()
            if plat:
                self._resolved.platform = plat

        self._mode = MODE_VIEW
        self._stack.setCurrentWidget(self._view_page)
        self._fill_view()
        self._persist_info_sidecar()
        self.metadata_saved.emit(managed_before)

    def _open_batch_edit_dialog(self) -> None:
        from core.mod_platform import normalize_platform

        ids = list(self._batch_mod_ids)
        if len(ids) <= 1:
            return
        dlg = EditModDialog(
            self,
            mod_ids=ids,
            platform=self._current_platform,
            game_name=self._batch_game_name,
            game_id=self._batch_game_id,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()
        plat = str(values.get("platform") or "").strip()
        if not plat:
            return
        try:
            updated = get_db().batch_update_platform(ids, plat)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._current_platform = normalize_platform(plat)
        QMessageBox.information(
            self,
            "批量编辑完成",
            f"已更新 {updated} 个 Mod 的来源为「{format_platform_name(plat)}」。",
        )
        self.batch_platform_saved.emit(list(ids))
        self._persist_sidecars_for_mod_ids(ids)

    def _persist_sidecars_for_mod_ids(self, mod_ids: list[str]) -> None:
        """Dual-write platform edits to ``.info/metadata.json`` (best-effort)."""
        if self._library_root is None:
            return
        try:
            from services.file_ops import ModFileManager
            from services.info_sidecar import write_sidecar_for_mod

            mgr = ModFileManager(self._library_root)
            for mid in mod_ids:
                folder = mgr.find_by_published_id(str(mid))
                if folder is not None:
                    write_sidecar_for_mod(folder, mid)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._empty_page = self._build_empty_page()
        self._view_page = self._build_view_page()
        self._edit_page = self._build_edit_page()
        self._stack.addWidget(self._empty_page)
        self._stack.addWidget(self._view_page)
        self._stack.addWidget(self._edit_page)

        self.setStyleSheet(PANEL_STYLE)

    def _build_empty_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("detailPanelInner")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 24, 20, 24)
        label = QLabel("选择一个 Mod\n在右侧查看详情")
        label.setObjectName("detailEmptyHint")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _build_view_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._view_scroll = QScrollArea()
        self._view_scroll.setWidgetResizable(True)
        self._view_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._view_scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QFrame()
        body.setObjectName("detailPanelInner")
        body.setMinimumWidth(0)
        body.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        self._name_value = ""
        self._id_value = ""
        self._source_url_value = ""

        layout.addWidget(self._build_header_section())
        layout.addWidget(self._build_status_banner())
        layout.addWidget(self._build_metadata_section())
        # Multi-file Mods only — hidden when file count ≤ 1 (see _fill_mod_files_list).
        layout.addWidget(self._build_files_section())
        layout.addWidget(self._build_flag_tags_section())
        layout.addWidget(self._build_dependency_pill_section())
        # Legacy sections kept off-screen for fill/wire/test attribute compatibility.
        self._legacy_host = QWidget()
        self._legacy_host.hide()
        legacy = QVBoxLayout(self._legacy_host)
        legacy.setContentsMargins(0, 0, 0, 0)
        legacy.setSpacing(0)
        legacy.addWidget(self._build_status_section())
        legacy.addWidget(self._build_version_section())
        legacy.addWidget(self._build_relations_section())
        legacy.addWidget(self._build_legacy_user_tags_section())
        layout.addWidget(self._legacy_host)

        layout.addStretch(1)
        self._view_scroll.setWidget(body)
        self._view_scroll.setMinimumWidth(0)
        outer.addWidget(self._view_scroll, stretch=1)
        # Single ops container: secondary actions + deploy (same parent / style).
        outer.addWidget(self._build_actions_footer())
        self._wire_view_actions()
        return page

    def _make_icon_button(
        self,
        tooltip: str,
        icon: QStyle.StandardPixmap,
        *,
        object_name: str = "panelIconButton",
    ) -> QPushButton:
        """Compact secondary action — icon + tooltip, no truncatable text label."""
        btn = QPushButton()
        btn.setObjectName(object_name)
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setIcon(self.style().standardIcon(icon))
        btn.setIconSize(QSize(16, 16))
        btn.setFixedSize(32, 28)
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return btn

    def _build_header_section(self) -> QFrame:
        """Cover + title only — actions live in ``_build_action_area``."""
        header = QFrame()
        header.setObjectName("detailSection")
        header.setMinimumWidth(0)
        header.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        outer = QVBoxLayout(header)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_W, COVER_H)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_col = QVBoxLayout()
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(6)
        cover_col.addWidget(
            self.cover_label, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        self.btn_change_cover = QPushButton("更换封面")
        self.btn_change_cover.setObjectName("panelActionButton")
        self.btn_change_cover.setToolTip(
            "选择 png / jpg / jpeg / jfif / webp 作为展示图"
        )
        self.btn_change_cover.setFixedWidth(100)
        self.btn_change_cover.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.btn_change_cover.setStyleSheet(
            "QPushButton#panelActionButton { padding: 2px 6px; font-size: 11px; }"
        )
        cover_col.addWidget(
            self.btn_change_cover, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        top.addLayout(cover_col, stretch=0)

        title_host = QWidget()
        title_host.setMinimumWidth(0)
        title_host.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        title_col = QVBoxLayout(title_host)
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(6)

        self._title_full_text = ""
        self.view_title = QLabel()
        self.view_title.setObjectName("detailPanelTitle")
        self.view_title.setWordWrap(True)
        self.view_title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.view_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # Ignored horizontal: shrink to parent width so wrap can engage.
        self.view_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.view_title.setMinimumWidth(0)
        title_col.addWidget(self.view_title)

        badge_row = QHBoxLayout()
        badge_row.setSpacing(8)
        self.header_platform_badge = QLabel()
        self.header_platform_badge.setObjectName("detailPlatformBadge")
        self.header_platform_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_row.addWidget(self.header_platform_badge)
        self.size_badge = QLabel()
        self.size_badge.setObjectName("detailPlatformBadge")
        self.size_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.size_badge.hide()
        badge_row.addWidget(self.size_badge)
        badge_row.addStretch(1)
        title_col.addLayout(badge_row)
        # Backup remains in DB / diagnostics — never a metadata-row layout child.
        self.backup_status_badge = QLabel(header)
        self.backup_status_badge.hide()

        self.view_favorite = QLabel()
        self.view_favorite.setObjectName("detailFavoriteLabel")
        self.view_favorite.hide()
        title_col.addWidget(self.view_favorite)

        title_col.addStretch(1)

        self.btn_refresh_mod = QPushButton("⟳ 刷新信息")
        self.btn_refresh_mod.setObjectName("detailRefreshButton")
        self.btn_refresh_mod.setToolTip(
            "刷新 Steam 元数据（失败/Unknown 重试）并重新扫描目录"
        )
        self.btn_refresh_mod.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_mod.setFlat(False)
        self.btn_refresh_mod.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        refresh_layout = QHBoxLayout()
        refresh_layout.setContentsMargins(0, 0, 0, 0)
        refresh_layout.setSpacing(0)
        refresh_layout.addStretch(1)
        refresh_layout.addWidget(self.btn_refresh_mod)
        title_col.addLayout(refresh_layout)

        top.addWidget(title_host, stretch=1)
        outer.addLayout(top)

        # Hidden stubs for removed header actions (API / older tests).
        self._header_actions = QWidget(header)
        self._header_actions.hide()
        self.btn_edit = QPushButton("编辑", header)
        self.btn_edit.hide()
        self.btn_remove_mod = QPushButton("删除", header)
        self.btn_remove_mod.hide()
        self.btn_header_copy_id = QPushButton("复制 ID", header)
        self.btn_header_copy_id.setToolTip("复制 ID")
        self.btn_header_copy_id.hide()
        self.btn_copy_info = QPushButton("复制全部信息", header)
        self.btn_copy_info.setToolTip("复制全部信息")
        self.btn_copy_info.hide()
        self.btn_copy_link = QPushButton("复制链接", header)
        self.btn_copy_link.setToolTip("复制链接")
        self.btn_copy_link.hide()

        return header

    def _build_status_banner(self) -> QFrame:
        """Hidden by default; shown for status messages (success or failure)."""
        self._status_banner = QFrame()
        self._status_banner.setObjectName("detailStatusBanner")
        self._status_banner.setProperty("tone", "error")
        layout = QVBoxLayout(self._status_banner)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        title = QLabel("状态")
        title.setObjectName("detailPanelSection")
        layout.addWidget(title)
        self._status_banner_body = QLabel()
        self._status_banner_body.setObjectName("detailStatusBannerBody")
        self._status_banner_body.setWordWrap(True)
        self._status_banner_body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._status_banner_body)
        self._status_banner.hide()
        return self._status_banner

    def _set_status_banner_tone(self, tone: str) -> None:
        """Apply success/error presentation to the shared status banner."""
        if not hasattr(self, "_status_banner"):
            return
        key = str(tone or "error").strip().lower()
        if key not in {"success", "error"}:
            key = "error"
        self._status_banner.setProperty("tone", key)
        # Qt picks up dynamic property selectors only after re-polish.
        style = self._status_banner.style()
        if style is not None:
            style.unpolish(self._status_banner)
            style.polish(self._status_banner)
        self._status_banner.update()

    def _show_status_banner(self, message: str, *, tone: str = "error") -> None:
        """Show the shared status banner with the correct success/error style."""
        text = str(message or "").strip()
        if not text or not hasattr(self, "_status_banner"):
            return
        try:
            from ui.popup_trace import log_popup

            log_popup("detailStatusBanner.show", detail=text[:120])
        except Exception:  # noqa: BLE001
            pass
        self._set_status_banner_tone(tone)
        self._status_banner_body.setText(text)
        self._status_banner.show()

    def _hide_status_banner(self) -> None:
        if hasattr(self, "_status_banner"):
            self._status_banner_body.clear()
            self._set_status_banner_tone("error")
            self._status_banner.hide()

    def _clear_op_status(self) -> None:
        if getattr(self, "_op_status_timer", None) is not None:
            try:
                self._op_status_timer.stop()
                self._op_status_timer.deleteLater()
            except RuntimeError:
                pass
            self._op_status_timer = None
        if hasattr(self, "op_status_label"):
            self.op_status_label.clear()
            self.op_status_label.setProperty("tone", "")
            style = self.op_status_label.style()
            if style is not None:
                style.unpolish(self.op_status_label)
                style.polish(self.op_status_label)

    def _set_op_status(
        self,
        message: str,
        *,
        tone: str = "",
        auto_clear_ms: int = 0,
    ) -> None:
        """Compact footer feedback — fixed height, no layout jump."""
        if not hasattr(self, "op_status_label"):
            return
        if getattr(self, "_op_status_timer", None) is not None:
            try:
                self._op_status_timer.stop()
                self._op_status_timer.deleteLater()
            except RuntimeError:
                pass
            self._op_status_timer = None
        text = str(message or "").strip()
        self.op_status_label.setText(text)
        key = str(tone or "").strip().lower()
        self.op_status_label.setProperty("tone", key)
        style = self.op_status_label.style()
        if style is not None:
            style.unpolish(self.op_status_label)
            style.polish(self.op_status_label)
        if auto_clear_ms > 0 and text:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._clear_op_status)
            self._op_status_timer = timer
            timer.start(int(auto_clear_ms))

    def _show_deploy_failure_banner(self, reason: str) -> None:
        msg = humanize_deploy_error(str(reason or "").strip() or "未知错误")
        # Never show bare "部署失败" without a reason line.
        if msg in ("部署失败。", "部署失败"):
            msg = "未知错误"
        # Extractor messages are already Chinese UI copy — render as-is.
        if msg.startswith(("部署失败", "解压失败", "RAR 部署失败")):
            body = msg
        else:
            body = f"部署失败：\n{msg}"
        self._show_status_banner(body, tone="error")

    def _build_flag_tags_section(self) -> QFrame:
        """Conflict / Invalid toggle chips — active chip moves to front."""
        frame = QFrame()
        frame.setObjectName("detailSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        caption = QLabel("标记")
        caption.setObjectName("detailPanelSection")
        layout.addWidget(caption)

        self._flag_tags_row = QHBoxLayout()
        self._flag_tags_row.setContentsMargins(0, 0, 0, 0)
        self._flag_tags_row.setSpacing(8)

        self.btn_tag_conflict = QPushButton("冲突")
        self.btn_tag_conflict.setObjectName("detailFlagChip")
        self.btn_tag_conflict.setCheckable(True)
        self.btn_tag_conflict.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_tag_conflict.setToolTip("标记为冲突")
        self.btn_tag_conflict.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        self.btn_tag_invalid = QPushButton("失效")
        self.btn_tag_invalid.setObjectName("detailFlagChip")
        self.btn_tag_invalid.setCheckable(True)
        self.btn_tag_invalid.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_tag_invalid.setToolTip("标记为失效")
        self.btn_tag_invalid.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        self.btn_tag_abandoned = QPushButton("停更")
        self.btn_tag_abandoned.setObjectName("detailFlagChip")
        self.btn_tag_abandoned.setCheckable(True)
        self.btn_tag_abandoned.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_tag_abandoned.setToolTip("标记为停更")
        self.btn_tag_abandoned.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        self._flag_tags_row.addWidget(self.btn_tag_conflict)
        self._flag_tags_row.addWidget(self.btn_tag_invalid)
        self._flag_tags_row.addWidget(self.btn_tag_abandoned)
        self._flag_tags_row.addStretch(1)
        layout.addLayout(self._flag_tags_row)
        return frame

    def _build_actions_footer(self) -> QFrame:
        """Single ops container: two fixed rows — browse/offline, then deploy/edit."""
        self._view_footer = QFrame()
        self._view_footer.setObjectName("detailFooter")
        actions = QVBoxLayout(self._view_footer)
        actions.setContentsMargins(8, 10, 8, 12)
        actions.setSpacing(8)

        caption = QLabel("操作")
        caption.setObjectName("detailPanelSection")
        actions.addWidget(caption)

        # Fixed-height op feedback — never grows the footer when messages appear.
        self.op_status_label = QLabel("")
        self.op_status_label.setObjectName("detailOpStatus")
        self.op_status_label.setFixedHeight(16)
        self.op_status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        actions.addWidget(self.op_status_label)
        self._op_status_timer: QTimer | None = None

        self.btn_folder = QPushButton("打开目录")
        self.btn_relocate = QPushButton("重新定位目录")
        self.btn_steam = QPushButton("打开官网")
        self.btn_offline = QPushButton("打开离线页面")
        self.btn_download_offline = QPushButton("导入离线页面")
        self.btn_deploy = QPushButton("部署")
        self.btn_redeploy = QPushButton("重新部署")
        self.btn_undeploy = QPushButton("取消部署")
        self.btn_edit_info = QPushButton("编辑信息")

        def _style_action(btn: QPushButton, tip: str) -> None:
            btn.setObjectName("panelActionButton")
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumWidth(0)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(6)
        for btn, tip in (
            (self.btn_folder, "打开本地 Mod 目录"),
            (self.btn_relocate, "为缺失的 Mod 选择新目录（仅更新路径，不移动文件）"),
            (self.btn_steam, "在浏览器中打开来源网页"),
            (self.btn_offline, "打开已保存的离线页面"),
            (self.btn_download_offline, "导入离线页面"),
        ):
            _style_action(btn, tip)
            row1.addWidget(btn, stretch=1)
        self.btn_relocate.hide()
        actions.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)
        for btn, tip in (
            (self.btn_deploy, "部署到游戏目录"),
            (self.btn_redeploy, "重新部署"),
            (self.btn_undeploy, "取消部署"),
            (self.btn_edit_info, "编辑显示名称、介绍与源链接"),
        ):
            _style_action(btn, tip)
            row2.addWidget(btn, stretch=1)
        # Only「部署」keeps primary highlight; all others share panelActionButton.
        self.btn_deploy.setObjectName("panelPrimaryButton")
        actions.addLayout(row2)
        return self._view_footer

    def _build_dependency_pill_section(self) -> QFrame:
        """Compact dependency block between 标记 and 操作 — no layout growth."""
        frame = QFrame()
        frame.setObjectName("detailDependencyBlock")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("依赖")
        title.setObjectName("detailPanelSection")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_add_dependency = QPushButton("+ 添加依赖")
        self.btn_add_dependency.setObjectName("dependencyPillButton")
        self.btn_add_dependency.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_dependency.setToolTip(
            "绑定依赖 Mod（输入 Workspace ID；部署时先安装依赖）"
        )
        head.addWidget(self.btn_add_dependency, stretch=0)
        layout.addLayout(head)

        self.dep_summary_label = QLabel("依赖于 —")
        self.dep_summary_label.setObjectName("detailPanelMeta")
        self.dep_summary_label.setWordWrap(True)
        layout.addWidget(self.dep_summary_label)
        return frame

    def _build_status_section(self) -> QFrame:
        frame = self._make_section("Status")
        body = frame.layout()
        assert isinstance(body, QVBoxLayout)

        self.status_conflict_label = QLabel("[Conflict] —")
        self.status_conflict_label.setObjectName("detailPanelBody")
        body.addWidget(self.status_conflict_label)

        self.status_invalid_label = QLabel("[Validity] —")
        self.status_invalid_label.setObjectName("detailPanelBody")
        body.addWidget(self.status_invalid_label)

        self.status_enabled_label = QLabel("[Enabled] —")
        self.status_enabled_label.setObjectName("detailPanelBody")
        body.addWidget(self.status_enabled_label)

        self.view_deploy = QLabel("[Deploy] —")
        self.view_deploy.setObjectName("detailPanelMeta")
        self.view_deploy.setWordWrap(True)
        self.view_deploy.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.view_deploy)

        self.view_offline = QLabel("[Offline] —")
        self.view_offline.setObjectName("detailPanelMeta")
        self.view_offline.setWordWrap(True)
        body.addWidget(self.view_offline)

        # Compact deploy details (errors/path) under Status summary
        self.view_deploy_error = QLabel()
        self.view_deploy_error.setObjectName("detailPanelMeta")
        self.view_deploy_error.setWordWrap(True)
        self._apply_tone(self.view_deploy_error, "error")
        body.addWidget(self.view_deploy_error)

        self.view_deploy_conflict = QLabel()
        self.view_deploy_conflict.setObjectName("detailPanelMeta")
        self.view_deploy_conflict.setWordWrap(True)
        self._apply_tone(self.view_deploy_conflict, "warning")
        body.addWidget(self.view_deploy_conflict)

        self.view_deploy_path = QLabel()
        self.view_deploy_path.setObjectName("detailPanelMeta")
        self.view_deploy_path.setWordWrap(True)
        self.view_deploy_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.view_deploy_path)

        self.view_deploy_time = QLabel()
        self.view_deploy_time.setObjectName("detailPanelMeta")
        self.view_deploy_time.setWordWrap(True)
        body.addWidget(self.view_deploy_time)

        self.view_deploy_type = QLabel()
        self.view_deploy_type.setObjectName("detailPanelMeta")
        self.view_deploy_type.setWordWrap(True)
        body.addWidget(self.view_deploy_type)

        # Kept for lifecycle fill compatibility (not primary Status rows).
        self.status_run_label = QLabel("运行状态：—")
        self.status_run_label.setObjectName("detailPanelMeta")
        self.status_run_label.hide()
        body.addWidget(self.status_run_label)

        body.addWidget(self._field_caption("原因"))
        self.status_reason_edit = QLineEdit()
        self.status_reason_edit.setPlaceholderText("失效原因或冲突备注")
        body.addWidget(self.status_reason_edit)

        body.addWidget(self._field_caption("Quick Actions"))
        status_btns = QHBoxLayout()
        status_btns.setSpacing(6)
        self.btn_enable_mod = QPushButton("Enable")
        self.btn_disable_mod = QPushButton("Disable")
        self.btn_mark_valid = QPushButton("Clear Invalid")
        self.btn_clear_conflict = QPushButton("Resolve Conflict")
        # Kept for wiring/tests; not part of Phase C Quick Actions strip.
        self.btn_mark_invalid = QPushButton("Mark Invalid")
        self.btn_mark_conflict = QPushButton("Mark Conflict")
        for btn in (
            self.btn_enable_mod,
            self.btn_disable_mod,
            self.btn_mark_valid,
            self.btn_clear_conflict,
            self.btn_mark_invalid,
            self.btn_mark_conflict,
        ):
            btn.setObjectName("panelActionButton")
            status_btns.addWidget(btn)
        self.btn_mark_invalid.hide()
        self.btn_mark_conflict.hide()
        status_btns.addStretch(1)
        body.addLayout(status_btns)

        self.btn_view_conflicts = QPushButton("冲突详情")
        self.btn_view_conflicts.setObjectName("panelActionButton")
        self.btn_view_conflicts.setCheckable(True)
        body.addWidget(self.btn_view_conflicts)

        self.status_conflict_detail = QLabel()
        self.status_conflict_detail.setObjectName("detailPanelMeta")
        self.status_conflict_detail.setWordWrap(True)
        self._apply_tone(self.status_conflict_detail, "error")
        self.status_conflict_detail.hide()
        body.addWidget(self.status_conflict_detail)
        return frame

    def _build_files_section(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("detailSection")
        self._files_section_frame = frame
        body = QVBoxLayout(frame)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(6)

        # Title left · file actions · stretch absorbs leftover (never squeeze buttons).
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        self._files_section_label = self._section_label("文件")
        title_row.addWidget(self._files_section_label, 0)
        self.btn_files_select_all = QPushButton("Select All")
        self.btn_files_main_only = QPushButton("Main Only")
        self.btn_files_clear_optional = QPushButton("Clear")
        self.btn_files_reset_default = QPushButton("Reset")
        for btn in (
            self.btn_files_select_all,
            self.btn_files_main_only,
            self.btn_files_clear_optional,
            self.btn_files_reset_default,
        ):
            btn.setObjectName("detailFilesActionButton")
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            title_row.addWidget(btn, 0)
        title_row.addStretch(1)
        body.addLayout(title_row)
        self.btn_files_select_all.clicked.connect(self._on_files_select_all)
        self.btn_files_main_only.clicked.connect(self._on_files_main_only)
        self.btn_files_clear_optional.clicked.connect(self._on_files_clear_optional)
        self.btn_files_reset_default.clicked.connect(self._on_files_reset_default)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(8)
        self.files_summary_label = QLabel()
        self.files_summary_label.setObjectName("detailFilesSummary")
        self.files_status_label = QLabel()
        self.files_status_label.setObjectName("detailFilesStatusReady")
        summary_row.addWidget(self.files_summary_label, 1)
        summary_row.addWidget(self.files_status_label, 0)
        body.addLayout(summary_row)

        self.mod_files_host = QWidget()
        self.mod_files_layout = QVBoxLayout(self.mod_files_host)
        self.mod_files_layout.setContentsMargins(0, 0, 0, 0)
        self.mod_files_layout.setSpacing(4)
        self.view_mod_files = QLabel("1 file")
        self.view_mod_files.setObjectName("detailPanelBody")
        self.view_mod_files.setWordWrap(True)
        self.mod_files_layout.addWidget(self.view_mod_files)
        body.addWidget(self.mod_files_host)
        frame.hide()  # shown only when file count > 1
        return frame

    def _build_version_section(self) -> QFrame:
        frame, body = self._make_collapsible_section("Version", expanded=False)
        self.status_version_label = QLabel("Current Version：—")
        self.status_version_label.setObjectName("detailPanelBody")
        self.status_version_label.setWordWrap(True)
        body.addWidget(self.status_version_label)

        self.status_installed_label = QLabel("Installed Version：—")
        self.status_installed_label.setObjectName("detailPanelBody")
        body.addWidget(self.status_installed_label)

        self.status_update_label = QLabel("Update Status：—")
        self.status_update_label.setObjectName("detailPanelMeta")
        body.addWidget(self.status_update_label)

        self.status_check_time_label = QLabel("Last Checked：—")
        self.status_check_time_label.setObjectName("detailPanelMeta")
        body.addWidget(self.status_check_time_label)
        return frame

    def _build_metadata_section(self) -> QFrame:
        """Inline Chinese metadata — one field per line, no stacked captions."""
        frame = QFrame()
        frame.setObjectName("detailSection")
        body = QVBoxLayout(frame)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(4)
        caption = QLabel("元数据")
        caption.setObjectName("detailPanelSection")
        body.addWidget(caption)

        self.meta_rich_label = QLabel()
        self.meta_rich_label.setObjectName("detailMetaLine")
        self.meta_rich_label.setWordWrap(True)
        self.meta_rich_label.setTextFormat(Qt.TextFormat.RichText)
        self.meta_rich_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.meta_rich_label)

        self.meta_name_line = QLabel("名称：—")
        self.meta_desc_line = QLabel("介绍：—")
        self.meta_source_line = QLabel("来源：—")
        self.meta_workspace_line = QLabel("Workspace ID: —")
        self.meta_author_line = QLabel("作者：—")
        self.meta_version_line = QLabel("版本：—")
        self.meta_updated_line = QLabel("更新时间：—")
        for lab in (
            self.meta_name_line,
            self.meta_desc_line,
            self.meta_source_line,
            self.meta_workspace_line,
            self.meta_author_line,
            self.meta_version_line,
            self.meta_updated_line,
        ):
            lab.setObjectName("detailMetaLine")
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            lab.hide()
            lab.setParent(frame)

        # Legacy hidden fields kept for fill / copy helpers / older tests.
        # 「原名」must NOT be a standalone QLabel — it lives in meta_rich_label only.
        self.view_platform = QLabel()
        self.view_name_caption = QLabel("名称：")
        self.view_steam = QLabel()
        self.view_id_caption = QLabel("ID：")
        self.view_id = QLabel()
        self.view_external_id = QLabel()
        self.view_source_caption = QLabel("来源：")
        self.view_source_url = QLabel()
        self.view_game = QLabel()
        self.view_custom_desc = QLabel()
        self.view_notes = QLabel()
        self.btn_copy_name = QPushButton("复制")
        self.btn_copy_id = QPushButton("复制")
        self.btn_copy_source_url = QPushButton("复制链接")
        self.btn_copy_source_url.setToolTip("复制链接")
        for w in (
            self.view_platform,
            self.view_name_caption,
            self.view_steam,
            self.view_id_caption,
            self.view_id,
            self.view_external_id,
            self.view_source_caption,
            self.view_source_url,
            self.view_game,
            self.view_custom_desc,
            self.view_notes,
            self.btn_copy_name,
            self.btn_copy_id,
            self.btn_copy_source_url,
        ):
            w.hide()
            w.setParent(frame)
        return frame

    def _build_relations_section(self) -> QFrame:
        frame, body = self._make_collapsible_section("Tags & Relations", expanded=False)

        body.addWidget(self._field_caption("Category Tags"))
        self.category_tags_label = QLabel("（无标签）")
        self.category_tags_label.setObjectName("detailPanelBody")
        self.category_tags_label.setWordWrap(True)
        body.addWidget(self.category_tags_label)
        cat_row = QHBoxLayout()
        self.category_tag_edit = QLineEdit()
        self.category_tag_edit.setPlaceholderText("Gameplay / Fix / Graphics …")
        self.btn_add_category_tag = QPushButton("+ Add Tag")
        self.btn_add_category_tag.setObjectName("panelActionButton")
        self.btn_remove_category_tag = QPushButton("Remove Tag")
        self.btn_remove_category_tag.setObjectName("panelActionButton")
        cat_row.addWidget(self.category_tag_edit, stretch=1)
        cat_row.addWidget(self.btn_add_category_tag)
        cat_row.addWidget(self.btn_remove_category_tag)
        body.addLayout(cat_row)

        self._rel_lists: dict[str, QListWidget] = {}
        self._rel_add_buttons: dict[str, QPushButton] = {}
        for key, caption, add_label, rtype in (
            ("dependencies", "Dependencies", "+ Add Mod", RELATIONSHIP_DEPENDENCY),
            ("conflicts", "Conflicts", "+ Add Conflict", RELATIONSHIP_CONFLICT),
            ("addons", "Addons", "+ Add Extension", RELATIONSHIP_ADDON),
            ("patches", "Patches", "+ Add Patch", RELATIONSHIP_PATCH),
        ):
            body.addWidget(self._field_caption(caption))
            lst = QListWidget()
            lst.setObjectName("detailList")
            lst.setMinimumHeight(48)
            lst.setMaximumHeight(90)
            lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self._rel_lists[key] = lst
            body.addWidget(lst)
            row = QHBoxLayout()
            add_btn = QPushButton(add_label)
            add_btn.setObjectName("panelActionButton")
            rem_btn = QPushButton("Remove")
            rem_btn.setObjectName("panelActionButton")
            add_btn.clicked.connect(
                lambda _=False, t=rtype: self._on_add_relationship(t)
            )
            rem_btn.clicked.connect(
                lambda _=False, k=key: self._on_remove_relationship(k)
            )
            row.addWidget(add_btn)
            row.addWidget(rem_btn)
            row.addStretch(1)
            body.addLayout(row)
            self._rel_add_buttons[key] = add_btn
        return frame

    def _build_legacy_user_tags_section(self) -> QFrame:
        """Hidden Phase C: keep widgets for tests / data compatibility."""
        tags_sec = self._make_section("用户标记")
        tags_body = tags_sec.layout()
        assert isinstance(tags_body, QVBoxLayout)

        self.tag_invalid_check = QCheckBox("已失效（标签）")
        tags_body.addWidget(self.tag_invalid_check)

        tags_body.addWidget(self._field_caption("失效原因（标签）"))
        self.tag_invalid_reason = QLineEdit()
        self.tag_invalid_reason.setPlaceholderText("可选：说明失效原因")
        tags_body.addWidget(self.tag_invalid_reason)

        self.tag_conflict_check = QCheckBox("存在冲突（关联）")
        tags_body.addWidget(self.tag_conflict_check)

        tags_body.addWidget(self._field_caption("冲突 Mod（勾选其它 Mod）"))
        self.tag_conflict_list = QListWidget()
        self.tag_conflict_list.setObjectName("detailList")
        self.tag_conflict_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.tag_conflict_list.setMinimumHeight(100)
        self.tag_conflict_list.setMaximumHeight(160)
        tags_body.addWidget(self.tag_conflict_list)

        self.btn_save_tags = QPushButton("保存标记")
        self.btn_save_tags.setObjectName("panelPrimaryButton")
        tags_body.addWidget(self.btn_save_tags)

        self.view_tag_deploy_hint = QLabel()
        self.view_tag_deploy_hint.setObjectName("detailPanelMeta")
        self.view_tag_deploy_hint.setWordWrap(True)
        self._apply_tone(self.view_tag_deploy_hint, "warning")
        tags_body.addWidget(self.view_tag_deploy_hint)

        tags_sec.hide()
        return tags_sec

    def _wire_view_actions(self) -> None:
        self.btn_enable_mod.clicked.connect(self._on_enable_mod)
        self.btn_disable_mod.clicked.connect(self._on_disable_mod)
        self.btn_mark_invalid.clicked.connect(self._on_mark_invalid)
        self.btn_mark_valid.clicked.connect(self._on_mark_valid)
        self.btn_mark_conflict.clicked.connect(self._on_mark_conflict)
        self.btn_clear_conflict.clicked.connect(self._on_clear_conflict)
        self.btn_view_conflicts.toggled.connect(self._on_toggle_conflict_detail)
        self.btn_add_category_tag.clicked.connect(self._on_add_category_tag)
        self.btn_remove_category_tag.clicked.connect(self._on_remove_category_tag)
        self.tag_invalid_check.toggled.connect(self._on_tag_invalid_toggled)
        self.tag_conflict_check.toggled.connect(self._on_tag_conflict_toggled)
        self.btn_save_tags.clicked.connect(self._save_user_tags)
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_relocate.clicked.connect(self._relocate_mod_folder)
        self.btn_offline.clicked.connect(self._open_offline)
        self.btn_download_offline.clicked.connect(self._download_offline_page)
        self.btn_steam.clicked.connect(self._open_steam)
        self.btn_edit_info.clicked.connect(self.open_edit_info_dialog)
        self.btn_change_cover.clicked.connect(self._change_cover)
        self.btn_refresh_mod.clicked.connect(self._on_refresh_mod)
        self.btn_copy_name.clicked.connect(self._copy_name)
        try:
            from services.cover_loader import CoverLoaderManager

            CoverLoaderManager.instance().path_release_requested.connect(
                self._on_cover_path_release_requested
            )
        except Exception:  # noqa: BLE001
            pass
        self.btn_copy_id.clicked.connect(self._copy_id)
        self.btn_header_copy_id.clicked.connect(self._copy_id)
        self.btn_copy_source_url.clicked.connect(self._copy_source_url)
        self.btn_copy_link.clicked.connect(self._copy_source_url)
        self.btn_copy_info.clicked.connect(self._copy_mod_info)
        self.btn_deploy.clicked.connect(self._request_deploy)
        self.btn_redeploy.clicked.connect(self._request_redeploy)
        self.btn_undeploy.clicked.connect(self._request_undeploy)
        if hasattr(self, "btn_add_dependency"):
            self.btn_add_dependency.clicked.connect(self._on_add_dependency_pill)
        self.btn_edit.clicked.connect(self.enter_edit)
        # Remove Mod entry removed from UI — keep stub unconnected.
        self.btn_tag_conflict.toggled.connect(self._on_flag_conflict_toggled)
        self.btn_tag_invalid.toggled.connect(self._on_flag_invalid_toggled)
        self.btn_tag_abandoned.toggled.connect(self._on_flag_abandoned_toggled)

    def _make_collapsible_section(
        self, title: str, *, expanded: bool = False
    ) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("detailSection")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        toggle = QToolButton(frame)
        toggle.setObjectName("collapsibleSection")
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        toggle.setText(title)
        toggle.setAutoRaise(True)
        outer.addWidget(toggle)

        content = QWidget(frame)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        outer.addWidget(content)
        # Visibility only AFTER parenting — setVisible(True) on parentless flashes.
        content.setVisible(expanded)

        def _on_toggled(checked: bool, btn=toggle, box=content) -> None:
            box.setVisible(checked)
            btn.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )

        toggle.toggled.connect(_on_toggled)
        return frame, body

    def _make_copy_field_row(
        self,
        caption: QLabel,
        value: QLabel,
        button: QPushButton,
        *,
        stacked: bool = False,
    ) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        caption.setObjectName("detailPanelMeta")
        col.addWidget(caption)
        col.addWidget(value)
        row.addLayout(col, stretch=1)
        button.setObjectName("panelActionButton")
        button.setFixedWidth(64 if button.text() == "复制" else 72)
        row.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
        return host

    def _build_edit_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QFrame()
        body.setObjectName("detailPanelInner")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("编辑 Mod 信息")
        heading.setObjectName("detailPanelTitle")
        layout.addWidget(heading)

        self.edit_hint = QLabel()
        self.edit_hint.setObjectName("detailPanelMeta")
        self.edit_hint.setWordWrap(True)
        layout.addWidget(self.edit_hint)

        layout.addWidget(self._section_label("显示名称"))
        self.edit_display_name = QLineEdit()
        self.edit_display_name.setPlaceholderText("留空则使用 Steam 原名")
        layout.addWidget(self.edit_display_name)

        layout.addWidget(self._section_label("自定义介绍"))
        self.edit_custom_desc = QTextEdit()
        self.edit_custom_desc.setAcceptRichText(False)
        self.edit_custom_desc.setPlaceholderText("可选")
        self.edit_custom_desc.setMinimumHeight(80)
        layout.addWidget(self.edit_custom_desc)

        layout.addWidget(self._section_label("备注"))
        self.edit_notes = QTextEdit()
        self.edit_notes.setAcceptRichText(False)
        self.edit_notes.setPlaceholderText("个人备注…")
        self.edit_notes.setMinimumHeight(80)
        layout.addWidget(self.edit_notes)

        self.edit_favorite = QCheckBox("收藏")
        layout.addWidget(self.edit_favorite)

        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        footer = QFrame()
        footer.setObjectName("detailFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(12, 10, 12, 12)
        buttons.setSpacing(8)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("panelPrimaryButton")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("panelActionButton")
        buttons.addStretch(1)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        outer.addWidget(footer)

        self.btn_save.clicked.connect(self._save_edit)
        self.btn_cancel.clicked.connect(self._cancel_edit)
        return page

    def _make_section(self, title: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("detailSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(self._section_label(title))
        return frame

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailPanelSection")
        return label

    @staticmethod
    def _field_caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailPanelField")
        return label

    @staticmethod
    def _apply_tone(label: QLabel, tone: str) -> None:
        """Apply semantic text color via Design Tokens (dynamic status only)."""
        color = {
            "success": ACCENT_SUCCESS,
            "warning": ACCENT_WARNING,
            "error": ACCENT_ERROR,
            "secondary": TEXT_SECONDARY,
        }.get(tone, TEXT_SECONDARY)
        label.setStyleSheet(f"color: {color};")

    def _set_header_title(self, text: str) -> None:
        """Store full title; show wrapped text; tooltip always full."""
        self._title_full_text = str(text or "")
        self.view_title.setToolTip(self._title_full_text)
        self._elide_header_title()

    def _elide_header_title(self) -> None:
        # Soft-break long unspaced tokens so QLabel word-wrap can wrap them.
        full = getattr(self, "_title_full_text", "") or ""
        self.view_title.setText(_insert_zwsp_breaks(full))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide_header_title()

    def _render_header_platform_badge(self, platform: str) -> None:
        from ui.platform_labels import platform_badge_label

        key = str(platform or PLATFORM_STEAM).strip().lower()
        styles = {
            PLATFORM_STEAM: (PLATFORM_STEAM_BG, PLATFORM_STEAM_FG, PLATFORM_STEAM_BORDER),
            PLATFORM_NEXUS: (PLATFORM_NEXUS_BG, PLATFORM_NEXUS_FG, PLATFORM_NEXUS_BORDER),
            PLATFORM_GITHUB: (
                PLATFORM_GITHUB_BG,
                PLATFORM_GITHUB_FG,
                PLATFORM_GITHUB_BORDER,
            ),
            PLATFORM_MODIO: (
                PLATFORM_MODIO_BG,
                PLATFORM_MODIO_FG,
                PLATFORM_MODIO_BORDER,
            ),
            PLATFORM_OTHER: (
                PLATFORM_OTHER_BG,
                PLATFORM_OTHER_FG,
                PLATFORM_OTHER_BORDER,
            ),
        }
        bg, fg, border = styles.get(
            key, (BACKGROUND_BUTTON_PRESSED, TEXT_SECONDARY, BORDER_STRONG)
        )
        text = platform_badge_label(key)
        self.header_platform_badge.setText(text)
        # Dynamic platform badge — token-based exception.
        badge_qss = (
            f"QLabel#detailPlatformBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 4px;"
            f"font-size: 11px; font-weight: 600; padding: 2px 8px;"
            f"}}"
        )
        self.header_platform_badge.setStyleSheet(badge_qss)
        self.header_platform_badge.adjustSize()
        self.header_platform_badge.show()
        if hasattr(self, "size_badge"):
            self.size_badge.setStyleSheet(badge_qss)

    def _render_header_size_badge(self, size_text: str = "") -> None:
        """Mirror platform badge styling; hide when size is unavailable."""
        if not hasattr(self, "size_badge"):
            return
        label = str(size_text or "").strip()
        if not label:
            self.size_badge.clear()
            self.size_badge.hide()
            return
        self.size_badge.setText(label)
        self.size_badge.setToolTip(f"Mod 大小：{label}")
        self.size_badge.adjustSize()
        self.size_badge.show()

    # ------------------------------------------------------------------
    # Fill / actions
    # ------------------------------------------------------------------

    def _fill_view(self) -> None:
        meta = self._metadata
        info = self._display_info
        resolved = getattr(self, "_resolved", None)
        assert meta is not None

        if resolved is not None:
            shown = resolved.display_name or meta.display_name
            steam = resolved.title or shown
            platform = resolved.platform or PLATFORM_STEAM
            source_url = resolved.source_url
            workspace_id = resolved.workspace_id
            desc_text = resolved.description
            self._folder_absent = not resolved.folder_present
        else:
            shown = info.display_name if info else meta.display_name
            steam = info.steam_name if info else (meta.title or "").strip()
            platform = PLATFORM_STEAM
            source_url = ""
            workspace_id = ""
            desc_text = ""
            if info is not None:
                platform = info.platform or PLATFORM_STEAM
                source_url = (info.source_url or "").strip()
                workspace_id = str(info.workspace_id or "").strip()
                desc_text = str(info.custom_description or "").strip()
            if not desc_text:
                desc_text = str(meta.description or "").strip()

        external_id = ""
        files_bundle = None
        if info is not None:
            if not resolved:
                platform = info.platform or platform
            external_id = (info.external_id or "").strip()
            files_bundle = info.mod_files
        if not source_url and (meta.url or "").strip():
            source_url = str(meta.url).strip()
        if not source_url and platform == PLATFORM_STEAM and meta.published_file_id:
            source_url = meta.workshop_url

        self._set_header_title(shown)
        self._render_header_platform_badge(platform)
        self._render_header_size_badge("计算中…")
        self._request_size_badge_async()

        labels = get_platform_metadata_labels(platform)
        self._current_platform = str(platform or PLATFORM_STEAM).strip().lower()

        name_value = (shown or steam or "").strip()
        self._name_value = name_value
        self.view_name_caption.setText(f"{labels.name}：")
        self.view_steam.setText(name_value or "—")
        self.view_steam.setToolTip(name_value or "")
        self.btn_copy_name.setEnabled(bool(name_value))
        self.meta_name_line.setText(f"名称：{name_value or '—'}")

        if desc_text:
            self.meta_desc_line.setText(f"介绍：{desc_text}")
        else:
            self.meta_desc_line.clear()

        steam_name = (steam or "").strip()

        id_value = format_external_id(
            platform,
            external_id,
            source_url=source_url,
            published_file_id=str(meta.published_file_id or ""),
        )
        self._id_value = id_value
        self.view_id_caption.setText(f"{labels.external_id}：")
        self.view_id.setText(id_value or "—")
        self.view_external_id.setText(f"外部 ID：{id_value or '—'}")
        self.btn_copy_id.setEnabled(bool(id_value))
        self.btn_header_copy_id.setEnabled(bool(id_value))

        from ui.platform_labels import platform_badge_label

        platform_name = platform_badge_label(platform)
        self.view_platform.setText(f"{labels.platform}：{format_platform_name(platform)}")
        self.meta_source_line.setText(f"来源：{platform_name}")
        if resolved is None:
            workspace_id = ""
            if info is not None:
                workspace_id = str(info.workspace_id or "").strip()
        self.meta_workspace_line.setText(
            f"Workspace ID: {workspace_id or '—'}"
        )
        self._source_url_value = source_url
        self.view_source_caption.setText(f"{labels.source}：")
        if source_url:
            safe = source_url.replace("&", "&amp;")
            self.view_source_url.setText(f'<a href="{safe}">{safe}</a>')
            self.btn_copy_source_url.setEnabled(True)
            self.btn_copy_link.setEnabled(True)
        else:
            self.view_source_url.setText("—")
            self.btn_copy_source_url.setEnabled(False)
            self.btn_copy_link.setEnabled(False)

        self._fill_mod_files_list(files_bundle)

        game = ""
        if self._library_root is not None:
            game = ModFileManager(self._library_root).game_name_for_path(
                self._managed_path  # type: ignore[arg-type]
            )
        game = meta.game_name or game or "—"
        app = f" · AppID {meta.app_id}" if meta.app_id else ""
        self.view_game.setText(f"游戏：{game}{app}")

        # Optional metadata rows (hide when empty).
        author = ""
        if resolved is not None:
            author = str(resolved.author or "").strip()
        if not author and meta is not None:
            author = str(getattr(meta, "author", "") or "").strip()
        version = ""
        updated = ""
        if info is not None:
            version = (
                (info.mod_version or "").strip()
                or (info.installed_version or "").strip()
            )
            updated = (info.offline_updated_at or info.version_checked_at or "").strip()
        if author:
            self.meta_author_line.setText(f"作者：{author}")
        else:
            self.meta_author_line.clear()
        if version:
            self.meta_version_line.setText(f"版本：{version}")
        else:
            self.meta_version_line.clear()
        if updated:
            self.meta_updated_line.setText(f"更新时间：{updated}")
        else:
            self.meta_updated_line.clear()

        self._render_metadata_rich_block(
            name_value=name_value,
            original_name=steam_name,
            desc_text=desc_text,
            platform_name=platform_name,
            workspace_id=workspace_id,
            author=author,
            version=version,
            updated=updated,
        )

        self._refresh_offline_status_label()
        self._update_offline_download_button()

        if not self._deploy_busy:
            self._fill_deploy_status_from_db()

        fav = bool(info.favorite) if info else False
        self.view_favorite.setText("★ 收藏" if fav else "")
        self.view_favorite.setVisible(fav)

        custom = (info.custom_description if info else "").strip()
        self.view_custom_desc.setText(custom or "（无）")

        notes = (info.user_notes if info else "").strip() or (
            meta.custom_notes or ""
        ).strip()
        self.view_notes.setText(notes or "（无）")

        self._fill_lifecycle_status()
        self._sync_flag_tags_from_status()
        self._fill_relationships()
        self._fill_user_tags()
        mid = self.current_mod_id()
        if mid:
            self._fill_category_tags(mid)
        else:
            self.category_tags_label.setText("（无标签）")
        self.view_tag_deploy_hint.setText(self._tag_deploy_hint)

        cover = self._resolve_cover_path()
        self._set_cover(cover)
        if getattr(self, "_folder_absent", False):
            self._set_files_section_visible(False)
            self.view_deploy.setText("⚠ 内容目录不存在")
            self._apply_tone(self.view_deploy, "warning")
            self.view_deploy_error.setText("请使用「重新定位目录」选择新的 Mod 文件夹")
        self._render_backup_status_badge()
        self._refresh_action_buttons()

    def _render_content_status_badge(self) -> None:
        """No-op: content_status is not shown in Detail Header Metadata Row."""
        badge = getattr(self, "content_status_badge", None)
        if badge is not None:
            badge.hide()
            badge.clear()

    def _render_backup_status_badge(self) -> None:
        """Keep badge hidden and out of the Source/Size metadata row."""
        badge = getattr(self, "backup_status_badge", None)
        if badge is not None:
            badge.hide()
            badge.clear()

    def _relocate_mod_folder(self) -> None:
        """Pick a new folder for a missing Mod — path update only."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from services.library_status import CONTENT_FOLDER_MISSING
        from services.mod_relocate import relocate_mod_folder

        mid = self.current_mod_id()
        if not mid:
            return
        # Only meaningful when folder is missing
        content_status = ""
        try:
            from core.db_manager import get_db
            from services.library_status import row_content_status

            brow = get_db().get_mod_backup_row(mid)
            if brow is not None:
                content_status = row_content_status(brow)
        except Exception:  # noqa: BLE001
            pass
        if not getattr(self, "_folder_absent", False) and content_status != CONTENT_FOLDER_MISSING:
            QMessageBox.information(
                self, "重新定位", "当前 Mod 目录存在，无需重新定位。"
            )
            return
        start = ""
        if self._library_root is not None:
            start = str(self._library_root)
        chosen = QFileDialog.getExistingDirectory(self, "选择 Mod 目录", start)
        if not chosen:
            return
        result = relocate_mod_folder(mid, chosen)
        if not result.success:
            QMessageBox.warning(
                self, "重新定位失败", str(result.error or "身份不匹配")
            )
            return
        QMessageBox.information(
            self,
            "重新定位成功",
            f"已更新路径：\n{result.path}\n匹配方式：{result.matched_by}",
        )
        self.relocate_completed.emit(mid)

    def _refresh_action_buttons(self) -> None:
        """Enable browse / source / offline actions from runtime paths + metadata."""
        if self._mode != MODE_VIEW or self._batch_mod_ids:
            return
        folder_ok = (
            self._managed_path is not None
            and self._managed_path.is_dir()
            and not getattr(self, "_folder_absent", False)
        )
        if hasattr(self, "btn_folder"):
            self.btn_folder.setEnabled(folder_ok)
            if not folder_ok and getattr(self, "_folder_absent", False):
                self.btn_folder.setToolTip("内容缺失，无法操作")
        if hasattr(self, "btn_relocate"):
            show_relocate = bool(
                getattr(self, "_folder_absent", False) and self.current_mod_id()
            )
            self.btn_relocate.setVisible(show_relocate)
            self.btn_relocate.setEnabled(show_relocate)
        if hasattr(self, "btn_steam"):
            self.btn_steam.setEnabled(bool(self._current_source_url()))
        if hasattr(self, "btn_offline"):
            self.btn_offline.setEnabled(self._has_offline_page())
        if hasattr(self, "btn_edit_info"):
            self.btn_edit_info.setEnabled(not getattr(self, "_folder_absent", False))
        if hasattr(self, "btn_refresh_mod"):
            self.btn_refresh_mod.setEnabled(folder_ok)
        if hasattr(self, "btn_add_dependency"):
            mid_ok = bool(self.current_mod_id())
            self.btn_add_dependency.setEnabled(mid_ok and folder_ok)
        self._set_files_actions_visible(folder_ok)

    def _request_size_badge_async(self) -> None:
        """Compute directory size off the UI thread; token guards stale results."""
        if self._managed_path is None or getattr(self, "_folder_absent", False):
            self._render_header_size_badge("")
            return
        root = Path(self._managed_path)
        if not root.is_dir():
            self._render_header_size_badge("")
            return
        mid = self.current_mod_id() or ""
        token = f"{mid}:{root}"
        self._size_token = token

        from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

        class _SizeSignals(QObject):
            finished = Signal(str, int)

        class _SizeTask(QRunnable):
            def __init__(self, tok: str, path: Path, signals: _SizeSignals) -> None:
                super().__init__()
                self._tok = tok
                self._path = path
                self._signals = signals

            def run(self) -> None:  # noqa: D401
                try:
                    total = int(get_directory_size(self._path))
                except Exception:  # noqa: BLE001
                    total = -1
                self._signals.finished.emit(self._tok, total)

        # Keep QObject alive until the callback fires.
        signals = _SizeSignals(self)
        self._size_signals = signals
        signals.finished.connect(self._on_size_badge_ready)
        QThreadPool.globalInstance().start(_SizeTask(token, root, signals))

    def _on_size_badge_ready(self, token: str, total: int) -> None:
        if str(token) != getattr(self, "_size_token", ""):
            return
        if self._mode != MODE_VIEW:
            return
        if total < 0:
            self._render_header_size_badge("")
            return
        self._render_header_size_badge(format_size(int(total)))

    def _refresh_size_badge(self) -> None:
        """Legacy sync path — prefer ``_request_size_badge_async``."""
        if self._mode != MODE_VIEW:
            return
        self._render_header_size_badge(self._mod_size_label())

    def _mod_size_label(self) -> str:
        """Formatted size of the managed Mod folder (empty when unknown)."""
        if self._managed_path is None:
            return ""
        try:
            root = Path(self._managed_path)
            if not root.is_dir():
                return ""
            return format_size(get_directory_size(root))
        except OSError:
            return ""

    def _render_metadata_rich_block(
        self,
        *,
        name_value: str,
        original_name: str = "",
        desc_text: str,
        platform_name: str,
        workspace_id: str,
        author: str,
        version: str,
        updated: str,
    ) -> None:
        """Rich-text metadata block — bold prefixes, isolated long description."""
        esc = html_module.escape

        def _line(inner: str) -> str:
            # Zero top/bottom margin so QLabel RichText does not sink content.
            return f'<p style="margin:0;padding:0;line-height:1.45;">{inner}</p>'

        parts: list[str] = [
            _line(f"<b>名称：</b> {esc(name_value or '—')}"),
        ]
        orig = str(original_name or "").strip()
        if orig and orig != str(name_value or "").strip():
            parts.append(_line(f"<b>原名：</b> {esc(orig)}"))
        if desc_text:
            desc_html = _strip_leading_html_blank(
                _format_description_rich_html(desc_text)
            )
            parts.append(_line("<b>介绍：</b>"))
            parts.append(
                f"<div style='margin:0;padding:0;line-height:1.55;'>{desc_html}</div>"
            )
        parts.append(_line(f"<b>来源：</b> {esc(platform_name)}"))
        parts.append(
            _line(f"<b>Workspace ID:</b> {esc(workspace_id or '—')}")
        )
        if author:
            parts.append(_line(f"<b>作者：</b> {esc(author)}"))
        if version:
            parts.append(_line(f"<b>版本：</b> {esc(version)}"))
        if updated:
            parts.append(_line(f"<b>更新时间：</b> {esc(updated)}"))

        html_body = _strip_leading_html_blank("".join(parts))
        self.meta_rich_label.setText(html_body)

    def _resolve_cover_path(self) -> Path | None:
        from services.mod_metadata_resolver import resolve_cover_path

        return resolve_cover_path(self.current_mod_id() or None, self._managed_path)

    def _change_cover(self) -> None:
        if self._managed_path is None or self._metadata is None:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "更换封面",
            "",
            "Images (*.png *.jpg *.jpeg *.jfif *.webp);;All files (*.*)",
        )
        if not chosen:
            return
        from services.importers.image_picker import apply_cover_to_mod

        mid = self.current_mod_id() or self._metadata.published_file_id
        try:
            rel = apply_cover_to_mod(
                self._managed_path, chosen, mod_id=mid, update_db=True
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更换封面", str(exc))
            return
        if not rel:
            QMessageBox.warning(self, "更换封面", "无法保存封面图片")
            return
        self.show_mod(self._managed_path)

    def _recheck_missing_content(self, managed_path: Path | None = None) -> bool:
        """If payload files exist, clear ``is_missing_content`` and persist."""
        path = Path(managed_path) if managed_path is not None else self._managed_path
        if path is None:
            return False
        try:
            from services.file_ops import clear_missing_content_if_present

            return bool(clear_missing_content_if_present(path))
        except Exception:  # noqa: BLE001
            return False

    def _on_refresh_mod(self) -> None:
        """Steam: refresh current mod metadata only; local archive rescan for single Mod."""
        import logging

        _log = logging.getLogger(__name__)
        if self._metadata_worker is not None and self._metadata_worker.isRunning():
            return
        if self._refresh_btn_state == "running":
            return

        platform = str(getattr(self, "_current_platform", "") or "").strip().lower()
        _log.info("Metadata refresh requested platform=%s", platform or "(empty)")

        if self._managed_path is not None and self._recheck_missing_content(
            self._managed_path
        ):
            self.tags_saved.emit(self._managed_path)

        # Multi-select → batch metadata refresh (all platforms; Nexus id wash silent).
        if self._batch_mod_ids and len(self._batch_mod_ids) > 1:
            self._set_refresh_button_state("running")
            self._start_batch_metadata_refresh()
            return

        if self._managed_path is None:
            return

        self._set_refresh_button_state("running")
        mid = self.current_mod_id()

        _log.info("Selecting metadata provider platform=%s", platform or "(empty)")

        from .metadata_refresh_thread import ModRefreshWorker

        source_url = ""
        if self._display_info is not None:
            source_url = str(self._display_info.source_url or "").strip()
        if not source_url and self._metadata is not None:
            source_url = str(self._metadata.url or "").strip()

        worker = ModRefreshWorker(
            self._managed_path,
            mod_id=mid,
            library_root=self._library_root,
            platform=platform or getattr(self, "_current_platform", "") or "",
            source_url=source_url,
            parent=self,
        )
        worker.refresh_started.connect(self._on_metadata_refresh_started)
        worker.refresh_finished.connect(self._on_metadata_refresh_finished)
        worker.refresh_failed.connect(self._on_metadata_refresh_failed)
        worker.finished.connect(self._on_metadata_worker_finished)
        self._metadata_worker = worker
        worker.start()

    def _set_refresh_button_state(
        self,
        state: str,
        *,
        detail: str = "",
        restore_ms: int = 1800,
    ) -> None:
        """
        Visible refresh-button states: idle / running / success / failure.

        *detail* (failure) is shown in the existing status banner.
        """
        if not hasattr(self, "btn_refresh_mod"):
            return
        # Cancel pending idle restore when entering a new state.
        if self._refresh_btn_timer is not None:
            try:
                self._refresh_btn_timer.stop()
                self._refresh_btn_timer.deleteLater()
            except RuntimeError:
                pass
            self._refresh_btn_timer = None

        key = str(state or "idle").strip().lower()
        self._refresh_btn_state = key
        btn = self.btn_refresh_mod

        if key == "running":
            btn.setText("刷新中…")
            btn.setToolTip("正在刷新 Mod 信息，请稍候")
            btn.setEnabled(False)
            self._set_op_status("◌ 正在刷新 Mod 信息…", tone="warning")
            return

        if key == "success":
            btn.setText("✓ 已更新")
            btn.setToolTip("Mod 信息已更新")
            btn.setEnabled(False)
            self._schedule_refresh_button_idle(restore_ms)
            return

        if key == "failure":
            btn.setText("⚠ 刷新失败")
            err = (detail or "").strip() or "元数据刷新失败"
            btn.setToolTip(err)
            btn.setEnabled(False)
            self._show_status_banner(f"刷新失败：\n{err}", tone="error")
            self._schedule_refresh_button_idle(restore_ms)
            return

        # idle
        batch = bool(self._batch_mod_ids) and len(self._batch_mod_ids) > 1
        btn.setText("⟳ 刷新信息")
        if batch:
            btn.setToolTip("批量刷新 Steam 元数据（跳过已成功项）")
        else:
            btn.setToolTip(
                "刷新 Steam 元数据（失败/Unknown 重试）并重新扫描目录"
            )
        btn.setEnabled(True)
        self._refresh_btn_state = "idle"

    def _schedule_refresh_button_idle(self, restore_ms: int) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._set_refresh_button_state("idle"))
        self._refresh_btn_timer = timer
        timer.start(max(400, int(restore_ms)))

    def _start_batch_metadata_refresh(self) -> None:
        entries = list(self._batch_entries or [])
        if not entries:
            # Fallback: ids only (platform unknown → treat as steam for legacy).
            entries = [
                (mid, Path(), getattr(self, "_current_platform", PLATFORM_STEAM) or PLATFORM_STEAM)
                for mid in (self._batch_mod_ids or [])
            ]
        if not entries:
            self._set_refresh_button_state("idle")
            return

        from PySide6.QtWidgets import QProgressDialog

        from .metadata_refresh_thread import MetadataBatchRefreshWorker

        progress = QProgressDialog(
            f"Refreshing metadata:\n0 / {len(entries)}",
            "取消",
            0,
            len(entries),
            self,
        )
        progress.setWindowTitle("刷新元数据")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        self._metadata_progress_dialog = progress

        worker = MetadataBatchRefreshWorker(
            entries,
            library_root=self._library_root,
            max_workers=2,
            parent=self,
        )
        worker.progress.connect(self._on_metadata_batch_progress)
        worker.refresh_finished.connect(self._on_metadata_batch_finished)
        worker.refresh_failed.connect(self._on_metadata_batch_failed)
        worker.finished.connect(self._on_metadata_worker_finished)
        progress.canceled.connect(worker.requestInterruption)
        self._metadata_worker = worker
        worker.start()

    def _on_metadata_refresh_started(self) -> None:
        self._set_refresh_button_state("running")

    def _on_metadata_refresh_finished(self, result: object) -> None:
        from services.metadata_refresh import MetadataRefreshResult

        if not isinstance(result, MetadataRefreshResult):
            self._set_refresh_button_state("idle")
            self._clear_op_status()
            return
        path = result.managed_path or self._managed_path
        if path is not None:
            try:
                from services.dir_size import invalidate_directory_size

                invalidate_directory_size(path)
            except Exception:  # noqa: BLE001
                pass
            self._recheck_missing_content(path)
            self.show_mod(path)
            self.metadata_saved.emit(path)
            self.tags_saved.emit(path)
        if result.success or result.skipped:
            msg = str(getattr(result, "message", "") or "").strip()
            if msg:
                self._show_status_banner(msg, tone="success")
            else:
                self._hide_status_banner()
            self._set_refresh_button_state("success")
            self._set_op_status("✓ 刷新完成", tone="success", auto_clear_ms=1800)
            return
        self._set_refresh_button_state(
            "failure",
            detail=result.error or "元数据刷新失败",
        )
        self._set_op_status("⚠ 刷新失败", tone="error", auto_clear_ms=2400)

    def _on_metadata_refresh_failed(self, error: str) -> None:
        err = (error or "").strip() or "元数据刷新失败"
        self._set_refresh_button_state("failure", detail=err)
        self._set_op_status("⚠ 刷新失败", tone="error", auto_clear_ms=2400)
        if self._managed_path is not None:
            self.show_mod(self._managed_path)

    def _on_metadata_batch_progress(self, done: int, total: int, message: str) -> None:
        dlg = self._metadata_progress_dialog
        if dlg is None:
            return
        dlg.setMaximum(max(int(total), 1))
        dlg.setValue(min(int(done), int(total)))
        dlg.setLabelText(message or f"Refreshing metadata:\n{done} / {total}")
        # Keep button in running state with live progress tip.
        if hasattr(self, "btn_refresh_mod"):
            self.btn_refresh_mod.setText("刷新中…")
            self.btn_refresh_mod.setToolTip(
                message or f"Refreshing metadata: {done} / {total}"
            )

    def _on_metadata_batch_finished(self, results: object) -> None:
        dlg = self._metadata_progress_dialog
        if dlg is not None:
            dlg.reset()
            dlg.deleteLater()
            self._metadata_progress_dialog = None

        rows = list(results or [])
        ok = sum(1 for r in rows if getattr(r, "success", False))
        skipped = sum(1 for r in rows if getattr(r, "skipped", False))
        failed = [
            r
            for r in rows
            if not getattr(r, "success", False) and not getattr(r, "skipped", False)
        ]
        # Notify library for each renamed / updated path.
        for r in rows:
            path = getattr(r, "managed_path", None)
            if path is not None and getattr(r, "success", False):
                self._recheck_missing_content(Path(path))
                self.metadata_saved.emit(Path(path))

        msg = f"完成：成功 {ok}（跳过健康 {skipped}）"
        if failed:
            detail = "\n".join(
                f"- {getattr(r, 'mod_id', '?')}: {getattr(r, 'error', '')}"
                for r in failed[:8]
            )
            self._set_refresh_button_state(
                "failure",
                detail=f"{msg}\n失败 {len(failed)}：\n{detail}",
                restore_ms=2500,
            )
        else:
            self._hide_status_banner()
            self._set_refresh_button_state("success")

    def _on_metadata_batch_failed(self, error: str) -> None:
        dlg = self._metadata_progress_dialog
        if dlg is not None:
            dlg.reset()
            dlg.deleteLater()
            self._metadata_progress_dialog = None
        self._set_refresh_button_state(
            "failure",
            detail=(error or "").strip() or "批量刷新失败",
        )

    def _on_metadata_worker_finished(self) -> None:
        self._metadata_worker = None
        # Leave success/failure flash + timer in control of the button label.
        if self._refresh_btn_state == "running":
            self._set_refresh_button_state("idle")

    def _persist_info_sidecar(self) -> None:
        """Write ``.info/metadata.json`` for the current Mod (best-effort)."""
        if self._managed_path is None:
            return
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            from services.info_sidecar import write_sidecar_for_mod

            write_sidecar_for_mod(self._managed_path, mid)
        except Exception:  # noqa: BLE001
            pass

    def _clear_mod_files_widgets(self) -> None:
        while self.mod_files_layout.count():
            item = self.mod_files_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Detach immediately so findChildren / layout refresh
                # cannot see stale rows still pending deleteLater.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _set_files_summary(
        self,
        selected_n: int,
        total_n: int,
        *,
        legacy_workshop: bool = False,
    ) -> None:
        summary, status = files_summary_lines(
            selected_n, total_n, legacy_workshop=legacy_workshop
        )
        if hasattr(self, "files_summary_label"):
            self.files_summary_label.setText(summary)
        if hasattr(self, "files_status_label"):
            self.files_status_label.setText(status)
            ready = status == "Ready"
            self.files_status_label.setObjectName(
                "detailFilesStatusReady" if ready else "detailFilesStatusEmpty"
            )
            # Force stylesheet refresh after objectName change
            self.files_status_label.style().unpolish(self.files_status_label)
            self.files_status_label.style().polish(self.files_status_label)
        if hasattr(self, "_files_section_label"):
            self._files_section_label.setText(f"文件 ({total_n})" if total_n else "文件")

    def _set_files_section_visible(self, visible: bool) -> None:
        frame = getattr(self, "_files_section_frame", None)
        if frame is not None:
            frame.setVisible(bool(visible))

    def _fill_mod_files_list(self, files_bundle) -> None:
        prev_updating = self._files_role_updating
        self._files_role_updating = True
        try:
            self._fill_mod_files_list_impl(files_bundle)
        finally:
            self._files_role_updating = prev_updating

    def _fill_mod_files_list_impl(self, files_bundle) -> None:
        self._clear_mod_files_widgets()
        files = list(files_bundle.files) if files_bundle else []
        # UI 绝对兜底：剔除「历史版本」漏网缓存
        files = filter_out_history_version_entries(files)
        platform = str(
            getattr(self, "_current_platform", PLATFORM_STEAM) or PLATFORM_STEAM
        ).strip().lower()

        # Steam / legacy empty bundle → whole-mod (count as 1) → hide section.
        if not files and platform == PLATFORM_STEAM:
            self._set_files_summary(1, 1, legacy_workshop=True)
            self._set_files_actions_visible(False)
            self._set_files_section_visible(False)
            return

        selected_n = count_selected(files)
        total_n = len(files)
        self._set_files_summary(selected_n, total_n)

        # Absolute rule: hide entire Files block when ≤ 1 file entry.
        if total_n <= 1:
            self._set_files_actions_visible(False)
            self._set_files_section_visible(False)
            return

        self._set_files_section_visible(True)
        show_actions = platform in (PLATFORM_NEXUS, PLATFORM_GITHUB)
        self._set_files_actions_visible(show_actions)

        mod_source = format_platform_name(platform)
        is_nexus = (
            platform == PLATFORM_NEXUS
            or mod_source == "Nexus Mods"
            or "nexus" in str(mod_source).lower()
            or "nexus" in platform
        )
        if is_nexus:
            self._build_nexus_files_list(files)
        elif platform == PLATFORM_GITHUB or mod_source == "GitHub":
            self._build_github_tree(sort_files_for_detail(files))
        else:
            self._build_github_tree(sort_files_for_detail(files))

    def _build_github_tree(self, files) -> None:
        """GitHub / default multi-file list — flat checkbox + badge rows."""
        first_widget = None
        for entry in files:
            if is_history_version_entry(entry):
                continue
            if "历史版本" in str(getattr(entry, "name", "") or "") or "历史版本" in str(
                getattr(entry, "path", "") or ""
            ):
                continue
            row = self._add_mod_file_row(entry)
            if first_widget is None:
                first_widget = row
        if first_widget is not None:
            self.view_mod_files = first_widget

    def _build_nexus_files_list(self, files) -> None:
        """Nexus multi-file list — 100% same flat row widget as GitHub."""
        first_widget = None
        for entry in sort_files_for_nexus_flat(files):
            if is_history_version_entry(entry):
                continue
            if "历史版本" in str(getattr(entry, "name", "") or "") or "历史版本" in str(
                getattr(entry, "path", "") or ""
            ):
                continue
            row = self._add_mod_file_row(entry)
            if first_widget is None:
                first_widget = row
        if first_widget is not None:
            self.view_mod_files = first_widget

    def _set_files_actions_visible(self, visible: bool) -> None:
        for btn in (
            getattr(self, "btn_files_select_all", None),
            getattr(self, "btn_files_main_only", None),
            getattr(self, "btn_files_clear_optional", None),
            getattr(self, "btn_files_reset_default", None),
        ):
            if btn is not None:
                btn.setVisible(bool(visible))

    _FILES_CHECK_COL_W = 22
    _FILES_BADGE_W = 48
    _FILES_BADGE_H = 20

    def _add_mod_file_row(self, entry) -> QWidget:
        """Unified row: checkbox | badge(48x20) | name | desc? | edit(24x24)."""
        platform = str(
            getattr(self, "_current_platform", PLATFORM_STEAM) or PLATFORM_STEAM
        ).strip().lower()
        badge_text, badge_style = file_list_badge(entry, platform)
        is_nexus = platform == PLATFORM_NEXUS
        is_github_source = (
            not is_nexus
            and badge_style == "alt"
            and str(badge_text or "").casefold() == "source"
        )
        show_checkbox = not is_github_source
        # Nexus: always show edit. GitHub: Other only (no Main/Source edit).
        if is_nexus:
            show_edit = True
        else:
            show_edit = badge_text is None
        fid = str(entry.id or "")
        full_name = file_combo_label(entry) or file_primary_label(entry)

        # Build off-screen: never show a parentless row; children always parented
        # to ``row`` before any polish / visibility. Layout addWidget reparents.
        row = QWidget()
        row.setObjectName("detailFilesRow")
        row.setProperty("file_id", fid)
        row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        row.customContextMenuRequested.connect(
            lambda pos, e=entry, w=row: self._on_file_row_context_menu(w, pos, e)
        )

        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 2, 0, 2)
        row_lay.setSpacing(6)

        # Checkbox — Fixed (parent before any polish / signal / visibility)
        check_host = QWidget(row)
        check_host.setFixedWidth(self._FILES_CHECK_COL_W)
        check_host.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        check_lay = QHBoxLayout(check_host)
        check_lay.setContentsMargins(0, 0, 0, 0)
        check_lay.setSpacing(0)
        if show_checkbox:
            cb = QCheckBox(check_host)
            cb.setObjectName("detailFilesCheckbox")
            stored = getattr(entry, "selected_for_deploy", None)
            if is_nexus:
                if badge_text == "Main":
                    checked = True if stored is None else bool(stored)
                else:
                    checked = False if stored is None else bool(stored)
            elif badge_style == "main":
                checked = True if stored is None else bool(stored)
            else:
                checked = bool(is_entry_selected_for_deploy(entry))
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
            cb.setEnabled(True)
            cb.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, False
            )
            cb.setProperty("file_id", fid)
            cb.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            cb.toggled.connect(
                lambda checked, file_id=fid: self._on_mod_file_toggled(
                    file_id, checked
                )
            )
            check_lay.addWidget(cb, 0, Qt.AlignmentFlag.AlignVCenter)
        row_lay.addWidget(check_host, 0)

        # Badge — short display labels; category property keeps full whitelist name
        display_text = badge_text or ""
        if display_text == "Miscellaneous":
            display_text = "Misc"
        elif display_text == "Optional":
            display_text = "Opt"
        badge = QLabel(display_text, row)
        badge.setFixedSize(38, 18)
        badge_font = badge.font()
        badge_font.setPointSize(8)
        badge_font.setBold(True)
        badge.setFont(badge_font)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setWordWrap(False)
        badge.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        if badge_text and badge_style:
            if is_nexus:
                badge.setObjectName("detailFileCategoryBadge")
                badge.setProperty("category", str(badge_text))
            else:
                badge.setObjectName(
                    "detailFileBadgeMain"
                    if badge_style == "main"
                    else "detailFileBadgeSource"
                )
                badge.setProperty("category", "")
        else:
            badge.setObjectName("detailFileBadgeSpacer")
            badge.setProperty("category", "")
            badge.clear()
        # Crush QSS padding that would otherwise inflate the badge.
        badge.setStyleSheet(
            (badge.styleSheet() or "") + "; padding: 0px; margin: 0px;"
        )
        sty = badge.style()
        if sty is not None:
            sty.unpolish(badge)
            sty.polish(badge)
        row_lay.addWidget(badge, 0)

        # Filename / desc stretch 3:2 so description keeps readable width
        name_lab = ElideLabel(full_name, row)
        name_lab.setObjectName("detailFilesPrimary")
        name_lab.setWordWrap(False)
        name_lab.setToolTip(full_name)
        name_lab.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        name_lab.setMinimumWidth(0)
        row_lay.addWidget(name_lab, 3)

        # Description (optional) + edit — Nexus always; GitHub Other only
        desc_text = file_description(entry) if show_edit else ""
        if show_edit and desc_text:
            desc_lab = ElideLabel(desc_text, row)
            desc_lab.setObjectName("detailFileDesc")
            desc_lab.setWordWrap(False)
            desc_lab.setToolTip(desc_text)
            desc_lab.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            desc_lab.setMinimumWidth(0)
            row_lay.addWidget(desc_lab, 2)
        if show_edit:
            # Parent + layout BEFORE any visibility — never parentless show.
            edit_btn = QPushButton("✎", row)
            edit_btn.setObjectName("detailFilesEditButton")
            edit_btn.setFixedSize(24, 24)
            edit_btn.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            edit_btn.setFlat(True)
            edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            edit_btn.setToolTip("编辑该文件的作用说明")
            edit_btn.clicked.connect(
                lambda _=False, file_id=fid, cur=desc_text: (
                    self._on_edit_file_description(file_id, cur)
                )
            )
            row_lay.addWidget(edit_btn, 0)

        self.mod_files_layout.addWidget(row)
        return row

    def _on_file_row_context_menu(self, row: QWidget, pos, entry) -> None:
        fid = str(getattr(entry, "id", "") or "")
        if not fid:
            return
        platform = str(
            getattr(self, "_current_platform", PLATFORM_STEAM) or PLATFORM_STEAM
        ).strip().lower()
        is_nexus = platform == PLATFORM_NEXUS
        menu = QMenu(row)
        if is_nexus:
            self._build_nexus_context_menu(menu, entry)
        else:
            self._build_github_context_menu(menu, entry)
        chosen = menu.exec(row.mapToGlobal(pos))
        if chosen is None:
            return
        data = chosen.data()
        if not isinstance(data, tuple) or len(data) != 2:
            return
        kind, value = data
        if kind == "nexus":
            self._apply_nexus_category(fid, str(value))
        elif kind == "github":
            self._apply_file_badge_role(fid, value)

    def _build_github_context_menu(self, menu: QMenu, entry) -> None:
        """GitHub / non-Nexus: Main / Source / clear — unchanged semantics."""
        act_main = menu.addAction("设为 Main (主文件)")
        act_main.setData(("github", "Main"))
        act_source = menu.addAction("设为 Source (源码)")
        act_source.setData(("github", "Source"))
        act_other = menu.addAction("取消特殊标记 (设为普通文件)")
        act_other.setData(("github", None))

    def _build_nexus_context_menu(self, menu: QMenu, entry) -> None:
        """Nexus-only: strict 5-category whitelist actions."""
        for label, cat in (
            ("设为 Main (主文件)", "Main"),
            ("设为 Optional (可选文件)", "Optional"),
            ("设为 Miscellaneous (杂项)", "Miscellaneous"),
            ("设为 汉化", "汉化"),
            ("设为 Other (其他/普通文件)", "Other"),
        ):
            act = menu.addAction(label)
            act.setData(("nexus", cat))

    def _apply_nexus_category(self, file_id: str, category: str) -> None:
        """Persist Nexus whitelist category, then refresh badge + checkbox."""
        mid = self.current_mod_id()
        if not mid or not file_id:
            return
        try:
            ModFilesJsonManager(get_db()).set_nexus_file_category(
                mid, file_id, category
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新文件失败", str(exc))
            return
        self._reload_files_after_mutation()

    def _apply_file_badge_role(self, file_id: str, kind: str | None) -> None:
        """Assign Main / Source / Other via exclusive role mapping, then refresh."""
        mid = self.current_mod_id()
        if not mid or not file_id:
            return
        platform = str(
            getattr(self, "_current_platform", PLATFORM_STEAM) or PLATFORM_STEAM
        ).strip().lower()
        try:
            mgr = ModFilesJsonManager(get_db())
            files = mgr.get_files(mid)
            main_id = ""
            source_id = ""
            for entry in files:
                badge = file_badge_kind(entry)
                eid = str(entry.id or "")
                if badge == "Main" and not main_id:
                    main_id = eid
                elif badge == "Source" and not source_id:
                    source_id = eid
            if kind == "Main":
                main_id = file_id
                if source_id == file_id:
                    source_id = ""
            elif kind == "Source":
                source_id = file_id
                if main_id == file_id:
                    main_id = ""
            else:
                if main_id == file_id:
                    main_id = ""
                if source_id == file_id:
                    source_id = ""
            mgr.set_file_role_mapping(
                mid,
                main_file_id=main_id or None,
                source_file_id=source_id or None,
                platform=platform,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新文件失败", str(exc))
            return
        self._reload_files_after_mutation()

    def _on_edit_file_description(self, file_id: str, current: str = "") -> None:
        mid = self.current_mod_id()
        if not mid or not file_id:
            return
        text, ok = QInputDialog.getText(
            self,
            "文件说明",
            "请输入该文件的作用说明",
            QLineEdit.EchoMode.Normal,
            current or "",
        )
        if not ok:
            return
        try:
            ModFilesJsonManager(get_db()).set_file_description(mid, file_id, text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新文件失败", str(exc))
            return
        self._reload_files_after_mutation()

    def _reload_files_after_mutation(self, *, emit_tags_saved: bool = True) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            pass
        files_bundle = self._display_info.mod_files if self._display_info else None
        self._fill_mod_files_list(files_bundle)
        self._persist_info_sidecar()
        # File checkbox toggles must stay quiet — tags_saved → show_mod flash/popup.
        if emit_tags_saved and self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_mod_file_toggled(self, file_id: str, checked: bool) -> None:
        """Persist deploy selection quietly — no toast / tooltip / row rebuild flash."""
        try:
            from ui.popup_trace import log_popup

            log_popup(
                "slot:_on_mod_file_toggled",
                detail=f"file_id={file_id!r} checked={checked}",
            )
        except Exception:  # noqa: BLE001
            pass
        mid = self.current_mod_id()
        if not mid or not file_id:
            return
        QToolTip.hideText()
        try:
            ModFilesJsonManager(get_db()).set_file_selection(mid, file_id, checked)
        except Exception:  # noqa: BLE001
            # Revert checkbox UI from DB without emitting card-refresh signals.
            self._reload_files_after_mutation(emit_tags_saved=False)
            QToolTip.hideText()
            return
        # Update summary only — do not rebuild rows (avoids QToolTip flash under cursor).
        try:
            self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            pass
        files_bundle = self._display_info.mod_files if self._display_info else None
        files = list(files_bundle.files) if files_bundle else []
        files = filter_out_history_version_entries(files)
        self._set_files_summary(count_selected(files), len(files))
        self._persist_info_sidecar()
        QToolTip.hideText()

    def _on_files_select_all(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            ModFilesJsonManager(get_db()).set_all_selection(mid, True)
        except Exception:  # noqa: BLE001
            self._reload_files_after_mutation(emit_tags_saved=False)
            return
        self._reload_files_after_mutation(emit_tags_saved=False)

    def _on_files_main_only(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            ModFilesJsonManager(get_db()).select_main_only(mid)
        except Exception:  # noqa: BLE001
            self._reload_files_after_mutation(emit_tags_saved=False)
            return
        self._reload_files_after_mutation(emit_tags_saved=False)

    def _on_files_clear_optional(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            ModFilesJsonManager(get_db()).clear_optional_selection(mid)
        except Exception:  # noqa: BLE001
            self._reload_files_after_mutation(emit_tags_saved=False)
            return
        self._reload_files_after_mutation(emit_tags_saved=False)

    def _on_files_reset_default(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            ModFilesJsonManager(get_db()).reset_default_selection(mid)
        except Exception:  # noqa: BLE001
            self._reload_files_after_mutation(emit_tags_saved=False)
            return
        self._reload_files_after_mutation(emit_tags_saved=False)
    def set_peer_mods(self, peers) -> None:
        """
        Other library Mods for pickers.

        Accepts ``list[tuple[mod_id, name]]`` or ``list[dict]`` with
        ``mod_id`` / ``title`` / ``platform`` / ``game_name``.
        """
        self._peer_mods = []
        self._peer_candidates = []
        for entry in peers or []:
            if isinstance(entry, dict):
                mid = str(entry.get("mod_id") or "").strip()
                if not mid.isdigit():
                    continue
                title = str(entry.get("title") or mid)
                self._peer_mods.append((mid, title))
                self._peer_candidates.append(
                    {
                        "mod_id": mid,
                        "title": title,
                        "platform": str(entry.get("platform") or "steam"),
                        "game_name": str(entry.get("game_name") or ""),
                    }
                )
            else:
                mid = str(entry[0]).strip()
                if not mid.isdigit():
                    continue
                title = str(entry[1] if len(entry) > 1 else mid)
                self._peer_mods.append((mid, title))
                self._peer_candidates.append(
                    {
                        "mod_id": mid,
                        "title": title,
                        "platform": "steam",
                        "game_name": "",
                    }
                )
        if self._mode == MODE_VIEW and self._metadata is not None:
            selected = self._selected_conflict_ids()
            self._rebuild_conflict_list(preselect=selected)

    def _fill_relationships(self) -> None:
        if not hasattr(self, "_rel_lists"):
            return
        for lst in self._rel_lists.values():
            lst.clear()
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        try:
            grouped = get_db().get_mod_relationships(mid)
        except Exception:  # noqa: BLE001
            return
        for key, items in grouped.items():
            lst = self._rel_lists.get(key)
            if lst is None:
                continue
            for item in items:
                title = str(item.get("title") or item.get("mod_id") or "")
                tid = str(item.get("mod_id") or "")
                label = title if title else tid
                row = QListWidgetItem(label)
                row.setData(Qt.ItemDataRole.UserRole, int(item.get("id") or 0))
                row.setToolTip(f"ID {tid}")
                lst.addItem(row)
        self._refresh_dependency_pill()

    def _refresh_dependency_pill(self) -> None:
        if not hasattr(self, "dep_summary_label"):
            return
        mid = self.current_mod_id()
        lines: list[str] = []
        if mid and mid.isdigit():
            try:
                grouped = get_db().get_mod_relationships(mid)
                for item in grouped.get("dependencies") or []:
                    title = str(item.get("title") or "").strip()
                    tid = str(item.get("mod_id") or "").strip()
                    if title and tid:
                        lines.append(title)
                    elif title or tid:
                        lines.append(title or tid)
            except Exception:  # noqa: BLE001
                pass
            resolved = getattr(self, "_resolved", None)
            if resolved is not None:
                known = {ln.split("\n", 1)[0] for ln in lines}
                for wid in resolved.dependencies or []:
                    text = str(wid or "").strip()
                    if text and text not in known and not any(
                        text in ln for ln in lines
                    ):
                        lines.append(text)
        if lines:
            self.dep_summary_label.setText("\n".join(lines))
        else:
            self.dep_summary_label.setText("依赖于 —")
        if hasattr(self, "btn_add_dependency"):
            self.btn_add_dependency.setEnabled(
                bool(mid and mid.isdigit())
                and not getattr(self, "_folder_absent", False)
            )

    def _on_add_dependency_pill(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        if getattr(self, "_folder_absent", False):
            return
        text, ok = QInputDialog.getText(
            self,
            "添加依赖",
            "请输入被依赖 Mod 的 Workspace ID：",
        )
        if not ok:
            return
        wid = str(text or "").strip()
        if not wid:
            return
        db = get_db()
        target = db.find_mod_id_by_workspace_id(wid)
        if not target:
            QMessageBox.warning(
                self,
                "添加依赖失败",
                f"本地库中未找到 Workspace ID：{wid}",
            )
            return
        if target == mid:
            QMessageBox.warning(self, "添加依赖失败", "不能将自身设为依赖")
            return
        try:
            db.add_mod_relationship(mid, target, RELATIONSHIP_DEPENDENCY)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "添加依赖失败", str(exc))
            return

        if self._managed_path is not None:
            try:
                from services.file_ops import (
                    persist_unified_metadata_dict,
                    read_info_metadata_dict,
                )

                data = read_info_metadata_dict(self._managed_path) or {}
                deps = [
                    str(x).strip()
                    for x in (data.get("dependencies") or [])
                    if str(x or "").strip()
                ]
                if wid not in deps:
                    deps.append(wid)
                data["dependencies"] = deps
                persist_unified_metadata_dict(self._managed_path, data)
            except Exception:  # noqa: BLE001
                try:
                    from services.info_sidecar import write_sidecar_for_mod

                    write_sidecar_for_mod(self._managed_path, mid, db=db)
                except Exception:  # noqa: BLE001
                    pass

        self._fill_relationships()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_add_relationship(self, relationship_type: str) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        from ui.mod_picker_dialog import ModPickerDialog

        candidates = [
            c
            for c in self._peer_candidates
            if str(c.get("mod_id")) != mid
        ]
        dlg = ModPickerDialog(
            candidates,
            title=f"Add {relationship_type}",
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        target = dlg.selected_mod_id()
        if not target:
            return
        try:
            get_db().add_mod_relationship(mid, target, relationship_type)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "添加关系失败", str(exc))
            return
        self._fill_relationships()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_remove_relationship(self, group_key: str) -> None:
        lst = self._rel_lists.get(group_key)
        if lst is None:
            return
        item = lst.currentItem()
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if not rid:
            return
        try:
            get_db().remove_mod_relationship(int(rid))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除关系失败", str(exc))
            return
        self._fill_relationships()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def set_tag_deploy_hint(self, text: str) -> None:
        """Show pre-deploy invalid/conflict warning (hint only)."""
        self._tag_deploy_hint = str(text or "").strip()
        if hasattr(self, "view_tag_deploy_hint"):
            self.view_tag_deploy_hint.setText(self._tag_deploy_hint)

    def _reset_status_widgets(self) -> None:
        self.status_run_label.setText("运行状态：—")
        if hasattr(self, "status_enabled_label"):
            self.status_enabled_label.setText("[Enabled] —")
        self.status_invalid_label.setText("[Validity] —")
        self.status_conflict_label.setText("[Conflict] —")
        if hasattr(self, "status_version_label"):
            self.status_version_label.setText("Current Version：—")
            self.status_installed_label.setText("Installed Version：—")
            self.status_update_label.setText("Update Status：—")
        self.status_reason_edit.clear()
        self.status_check_time_label.setText("Last Checked：—")
        self.btn_view_conflicts.blockSignals(True)
        self.btn_view_conflicts.setChecked(False)
        self.btn_view_conflicts.blockSignals(False)
        self.status_conflict_detail.clear()
        self.status_conflict_detail.hide()
        if hasattr(self, "category_tags_label"):
            self.category_tags_label.setText("（无标签）")
            self.category_tag_edit.clear()
        self._update_quick_actions(enabled=True, invalid=False, conflict=False)

    def _fill_lifecycle_status(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            self._reset_status_widgets()
            return
        try:
            st = get_db().get_mod_status(mid)
        except Exception:  # noqa: BLE001
            st = ModStatus()
        # Enabled flag must be cached before quick-action visibility runs.
        self._fill_version_and_enabled(mid)
        self._apply_status_to_widgets(st)
        self._fill_category_tags(mid)

    def _fill_version_and_enabled(self, mid: str) -> None:
        try:
            enabled = get_db().is_mod_enabled(mid)
        except Exception:  # noqa: BLE001
            enabled = True
        self.status_enabled_label.setText(
            f"[Enabled] {'Enabled' if enabled else 'Disabled'}"
        )
        try:
            ver = get_db().get_mod_version(mid)
        except Exception:  # noqa: BLE001
            from core.db_manager import ModVersionInfo

            ver = ModVersionInfo(mod_id=mid)
        self.status_version_label.setText(
            f"Current Version：{ver.mod_version or '—'}"
        )
        self.status_installed_label.setText(
            f"Installed Version：{ver.installed_version or '—'}"
        )
        self.status_update_label.setText(f"Update Status：{ver.status_label}")
        self._cached_enabled = enabled

    def _fill_category_tags(self, mid: str) -> None:
        try:
            tags = get_db().get_category_tags(mid)
        except Exception:  # noqa: BLE001
            tags = []
        if tags:
            self.category_tags_label.setText("  ".join(f"[{t}]" for t in tags))
        else:
            self.category_tags_label.setText("（无标签）")

    def _on_enable_mod(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        get_db().enable_mod(mid)
        self._fill_lifecycle_status()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_disable_mod(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        get_db().disable_mod(mid)
        self._fill_lifecycle_status()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_add_category_tag(self) -> None:
        mid = self.current_mod_id()
        tag = self.category_tag_edit.text().strip()
        if not mid or not mid.isdigit() or not tag:
            return
        try:
            get_db().add_category_tag(mid, tag)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "添加标签失败", str(exc))
            return
        self.category_tag_edit.clear()
        self._fill_category_tags(mid)
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_remove_category_tag(self) -> None:
        mid = self.current_mod_id()
        tag = self.category_tag_edit.text().strip()
        if not mid or not mid.isdigit() or not tag:
            return
        get_db().remove_category_tag(mid, tag)
        self.category_tag_edit.clear()
        self._fill_category_tags(mid)
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _request_remove(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        reply = QMessageBox.question(
            self,
            "Remove Mod",
            "该操作会删除：\n\n"
            "- Mod 文件\n"
            "- 部署状态\n\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.remove_requested.emit(mid)

    def _apply_status_to_widgets(self, st: ModStatus) -> None:
        self.status_run_label.setText(f"运行状态：{st.run_label}")
        invalid = bool(st.invalid)
        self.status_invalid_label.setText(
            f"[Validity] {'Invalid' if invalid else '正常'}"
        )
        self._apply_tone(
            self.status_invalid_label, "warning" if invalid else "secondary"
        )
        conflict_map = {
            CONFLICT_STATUS_NONE: "正常",
            CONFLICT_STATUS_WARNING: "警告",
            CONFLICT_STATUS_CONFLICT: "冲突",
        }
        conflict_text = conflict_map.get(st.conflict_status, st.conflict_status)
        self.status_conflict_label.setText(f"[Conflict] {conflict_text}")
        has_conflict = st.conflict_status in (
            CONFLICT_STATUS_CONFLICT,
            CONFLICT_STATUS_WARNING,
        )
        self._apply_tone(
            self.status_conflict_label, "error" if has_conflict else "secondary"
        )
        # Prefer invalid_reason when invalid; else conflict_note
        reason = ""
        if invalid:
            reason = st.invalid_reason or ""
        elif has_conflict:
            reason = st.conflict_note or st.invalid_reason or ""
        else:
            reason = st.invalid_reason or st.conflict_note or ""
        self.status_reason_edit.setText(reason)
        check = st.last_check_time or "—"
        self.status_check_time_label.setText(f"Last Checked：{check}")
        if not self.btn_view_conflicts.isChecked():
            self.status_conflict_detail.hide()
        enabled = getattr(self, "_cached_enabled", True)
        self.status_enabled_label.setText(
            f"[Enabled] {'Enabled' if enabled else 'Disabled'}"
        )
        self._apply_tone(
            self.status_enabled_label, "secondary" if enabled else "warning"
        )
        self._update_quick_actions(
            enabled=enabled,
            invalid=invalid,
            conflict=has_conflict,
        )

    def _update_quick_actions(
        self, *, enabled: bool, invalid: bool, conflict: bool
    ) -> None:
        """Show only meaningful Status quick actions (card priority).

        Priority: Conflict > Invalid > Disabled > normal Enable/Disable.
        Mark Invalid / Mark Conflict stay hidden (lifecycle editor removed from IA).
        """
        # Always hide create-flag actions from Quick Actions strip.
        self.btn_mark_invalid.hide()
        self.btn_mark_conflict.hide()

        show_resolve = bool(conflict)
        show_clear_invalid = bool(invalid) and not conflict
        show_enable = (not enabled) and not conflict and not invalid
        show_disable = bool(enabled) and not conflict and not invalid

        self.btn_clear_conflict.setVisible(show_resolve)
        self.btn_view_conflicts.setVisible(show_resolve)
        self.btn_mark_valid.setVisible(show_clear_invalid)
        self.btn_enable_mod.setVisible(show_enable)
        self.btn_disable_mod.setVisible(show_disable)
        self.btn_enable_mod.setEnabled(show_enable)
        self.btn_disable_mod.setEnabled(show_disable)

    def _status_reason_text(self) -> str:
        return self.status_reason_edit.text().strip()

    def _persist_status(self, **kwargs) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        try:
            st = get_db().update_mod_status(mid, touch_check_time=True, **kwargs)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新状态失败", str(exc))
            return
        self._apply_status_to_widgets(st)
        try:
            self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            pass
        if hasattr(self, "btn_tag_conflict"):
            self._sync_flag_tags_from_status()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_mark_invalid(self) -> None:
        self._persist_status(
            invalid=True,
            invalid_reason=self._status_reason_text() or "已标记失效",
        )

    def _on_mark_valid(self) -> None:
        self._persist_status(invalid=False, invalid_reason="")

    def _on_mark_conflict(self) -> None:
        note = self._status_reason_text() or "已标记冲突"
        self._persist_status(
            conflict_status=CONFLICT_STATUS_CONFLICT,
            conflict_note=note,
        )

    def _on_clear_conflict(self) -> None:
        self._persist_status(
            conflict_status=CONFLICT_STATUS_NONE,
            conflict_note="",
        )

    def _sync_flag_tags_from_status(self) -> None:
        """Mirror DB invalid/conflict onto flag chips without recursive toggles."""
        info = self._display_info
        invalid = bool(info.is_invalid) if info is not None else False
        conflict = False
        if info is not None:
            conflict = info.conflict_status in (
                CONFLICT_STATUS_CONFLICT,
                CONFLICT_STATUS_WARNING,
            )
        for btn, checked in (
            (self.btn_tag_conflict, conflict),
            (self.btn_tag_invalid, invalid),
            (self.btn_tag_abandoned, self._is_abandoned_tagged()),
        ):
            btn.blockSignals(True)
            btn.setChecked(checked)
            btn.blockSignals(False)
        self._reorder_flag_chips()

    def _reorder_flag_chips(self) -> None:
        """Active chips move to the front; inactive keep relative order after."""
        row = self._flag_tags_row
        while row.count():
            item = row.takeAt(0)
            if item is None:
                break
        active = [
            b
            for b in (
                self.btn_tag_conflict,
                self.btn_tag_invalid,
                self.btn_tag_abandoned,
            )
            if b.isChecked()
        ]
        inactive = [
            b
            for b in (
                self.btn_tag_conflict,
                self.btn_tag_invalid,
                self.btn_tag_abandoned,
            )
            if not b.isChecked()
        ]
        for btn in active + inactive:
            row.addWidget(btn)
        row.addStretch(1)

    def _on_flag_conflict_toggled(self, checked: bool) -> None:
        if checked:
            self._persist_status(
                conflict_status=CONFLICT_STATUS_CONFLICT,
                conflict_note=self._status_reason_text() or "已标记冲突",
            )
        else:
            self._persist_status(
                conflict_status=CONFLICT_STATUS_NONE,
                conflict_note="",
            )
        self._reorder_flag_chips()

    def _on_flag_invalid_toggled(self, checked: bool) -> None:
        if checked:
            self._persist_status(
                invalid=True,
                invalid_reason=self._status_reason_text() or "已标记失效",
            )
        else:
            self._persist_status(invalid=False, invalid_reason="")
        self._reorder_flag_chips()

    def _is_abandoned_tagged(self) -> bool:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return False
        try:
            return any(
                str(tag.tag_type or "") == TAG_TYPE_ABANDONED
                for tag in get_db().get_mod_tags(mid)
            )
        except Exception:  # noqa: BLE001
            return False

    def _on_flag_abandoned_toggled(self, checked: bool) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        try:
            if checked:
                get_db().add_mod_tag(mid, TAG_TYPE_ABANDONED, tag_value="")
            else:
                get_db().remove_mod_tag(mid, TAG_TYPE_ABANDONED)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新停更标记失败", str(exc))
            self.btn_tag_abandoned.blockSignals(True)
            self.btn_tag_abandoned.setChecked(not checked)
            self.btn_tag_abandoned.blockSignals(False)
            return
        self._reorder_flag_chips()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_toggle_conflict_detail(self, checked: bool) -> None:
        if not checked:
            self.status_conflict_detail.hide()
            return
        mid = self.current_mod_id()
        text = self._format_conflict_detail(mid)
        self.status_conflict_detail.setText(text or "当前无检测到的文件冲突。")
        self.status_conflict_detail.show()

    def _format_conflict_detail(self, mid: str | None) -> str:
        if not mid or self._library_root is None:
            # Fall back to stored note
            try:
                st = get_db().get_mod_status(mid) if mid else None
            except Exception:  # noqa: BLE001
                st = None
            if st and st.conflict_note:
                return f"冲突备注：{st.conflict_note}"
            return ""
        try:
            from services.conflict import ConflictDetector

            report = ConflictDetector(
                self._library_root, db=get_db()
            ).check_mod(mid, persist=False)
        except Exception as exc:  # noqa: BLE001
            return f"检测失败：{exc}"
        if not report.conflicts:
            try:
                st = get_db().get_mod_status(mid)
            except Exception:  # noqa: BLE001
                st = None
            if st and st.conflict_note:
                return f"冲突备注：{st.conflict_note}"
            return ""
        lines: list[str] = []
        for entry in report.conflicts:
            name = Path(entry.file).name or entry.file
            lines.append(f"冲突文件：{name}")
            # Resolve display names when possible
            sources: list[str] = []
            for other in entry.mods:
                label = other
                try:
                    info = get_db().get_mod_display_info(other)
                    if info and info.display_name:
                        label = f"{info.display_name} ({other})"
                except Exception:  # noqa: BLE001
                    pass
                sources.append(label)
            lines.append("来源：")
            for s in sources:
                lines.append(f"  · {s}")
            lines.append("")
        return "\n".join(lines).strip()

    def _on_tag_invalid_toggled(self, checked: bool) -> None:
        self.tag_invalid_reason.setEnabled(bool(checked))

    def _on_tag_conflict_toggled(self, checked: bool) -> None:
        self.tag_conflict_list.setEnabled(bool(checked))

    def _selected_conflict_ids(self) -> set[str]:
        out: set[str] = set()
        for i in range(self.tag_conflict_list.count()):
            item = self.tag_conflict_list.item(i)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                mid = str(item.data(Qt.ItemDataRole.UserRole) or "")
                if mid.isdigit():
                    out.add(mid)
        return out

    def _rebuild_conflict_list(self, *, preselect: set[str] | None = None) -> None:
        selected = preselect if preselect is not None else set()
        self.tag_conflict_list.clear()
        for mid, name in self._peer_mods:
            item = QListWidgetItem(f"{name}  ({mid})")
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setData(Qt.ItemDataRole.UserRole, mid)
            item.setCheckState(
                Qt.CheckState.Checked
                if mid in selected
                else Qt.CheckState.Unchecked
            )
            self.tag_conflict_list.addItem(item)

    def _fill_user_tags(self) -> None:
        mid = self.current_mod_id()
        invalid = False
        reason = ""
        conflict = False
        conflict_ids: set[str] = set()
        if mid:
            try:
                db = get_db()
                for tag in db.get_mod_tags(mid):
                    if tag.tag_type == TAG_TYPE_INVALID:
                        invalid = True
                        reason = tag.tag_value or ""
                    elif tag.tag_type == TAG_TYPE_CONFLICT:
                        conflict = True
                for rel in db.get_mod_relations(mid):
                    conflict_ids.add(rel.target_mod_id)
                    conflict = True
            except Exception:  # noqa: BLE001
                pass

        self.tag_invalid_check.blockSignals(True)
        self.tag_conflict_check.blockSignals(True)
        self.tag_invalid_check.setChecked(invalid)
        self.tag_invalid_reason.setText(reason)
        self.tag_invalid_reason.setEnabled(invalid)
        self.tag_conflict_check.setChecked(conflict)
        self.tag_conflict_list.setEnabled(conflict)
        self.tag_invalid_check.blockSignals(False)
        self.tag_conflict_check.blockSignals(False)
        self._rebuild_conflict_list(preselect=conflict_ids)

    def _save_user_tags(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            db = get_db()
            if self.tag_invalid_check.isChecked():
                db.add_mod_tag(
                    mid,
                    TAG_TYPE_INVALID,
                    tag_value=self.tag_invalid_reason.text().strip(),
                )
            else:
                db.remove_mod_tag(mid, TAG_TYPE_INVALID)

            if self.tag_conflict_check.isChecked():
                targets = sorted(self._selected_conflict_ids())
                db.set_mod_conflict_targets(mid, targets)
                if not targets:
                    # Conflict marked without peers — still keep the tag
                    db.add_mod_tag(mid, TAG_TYPE_CONFLICT, tag_value="")
            else:
                db.set_mod_conflict_targets(mid, [])
                db.remove_mod_tag(mid, TAG_TYPE_CONFLICT)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存标记失败", str(exc))
            return

        self._fill_user_tags()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _fill_edit_form(self) -> None:
        info = self._display_info
        meta = self._metadata
        steam = ""
        if info:
            steam = info.steam_name
        elif meta:
            steam = (meta.title or "").strip()
        self.edit_hint.setText(
            f"Mod ID：{meta.published_file_id if meta else '—'}"
            + (f"\nSteam 原名：{steam}" if steam else "")
        )
        self.edit_display_name.setText(info.user_display_name if info else "")
        self.edit_display_name.setPlaceholderText(steam or "留空则使用 Steam 原名")
        self.edit_custom_desc.setPlainText(info.custom_description if info else "")
        notes = ""
        if info:
            notes = info.user_notes
        elif meta:
            notes = meta.custom_notes or ""
        self.edit_notes.setPlainText(notes)
        self.edit_favorite.setChecked(bool(info.favorite) if info else False)

    def _offline_status_key(self) -> str:
        info = self._display_info
        if info is not None:
            status = normalize_offline_status(info.offline_status)
            if status != OFFLINE_STATUS_NONE:
                return status
        if self._has_offline_page():
            return OFFLINE_STATUS_ARCHIVED
        return OFFLINE_STATUS_NONE

    def _format_offline_updated_at(self) -> str:
        info = self._display_info
        raw = (info.offline_updated_at if info is not None else "") or ""
        text = str(raw).strip()
        if not text:
            return "—"
        # Prefer date portion for display (ISO / SQLite UTC).
        if "T" in text:
            return text.split("T", 1)[0]
        if " " in text:
            return text.split(" ", 1)[0]
        return text[:10] if len(text) >= 10 else text

    def _refresh_offline_status_label(self, *, busy: bool = False) -> None:
        """Show offline snapshot status (Status section summary)."""
        if busy:
            self.view_offline.setText("[Offline] 离线刷新中…")
            self._apply_tone(self.view_offline, "warning")
            return
        status = self._offline_status_key()
        updated = self._format_offline_updated_at()
        if status in (OFFLINE_STATUS_GENERATED, OFFLINE_STATUS_ARCHIVED):
            self.view_offline.setText(f"[Offline] 已保存 · {updated}")
            self._apply_tone(self.view_offline, "success")
        elif status == OFFLINE_STATUS_FAILED:
            self.view_offline.setText(f"[Offline] Failed · {updated}")
            self._apply_tone(self.view_offline, "error")
        else:
            # Keep「离线」「未保存」substrings for existing UI tests.
            self.view_offline.setText("[Offline] 离线未保存")
            self._apply_tone(self.view_offline, "warning")

    def _iter_offline_index_candidates(self):
        """Yield candidate offline index paths (canonical order)."""
        index = self._resolve_offline_page_path()
        if index is not None:
            yield index

    def _has_offline_page(self) -> bool:
        """Existence check only — does not read HTML content or hit the network."""
        return self._resolve_offline_page_path() is not None

    def _index_path(self) -> Path | None:
        return self._resolve_offline_page_path()

    def _resolve_offline_page_path(self) -> Path | None:
        from services.mod_metadata_resolver import resolve_offline_page

        return resolve_offline_page(self.current_mod_id() or None, self._managed_path)

    def _show_offline_missing_tooltip(self) -> None:
        # Intentionally silent — never pop floating tip / white toast on click.
        return

    def _on_cover_path_release_requested(self, path_key: str) -> None:
        """Drop detail cover pixmap when this Mod folder is about to rename."""
        if self._managed_path is None:
            return
        try:
            current = str(self._managed_path.expanduser().resolve())
        except OSError:
            current = str(self._managed_path)
        if current.lower().replace("/", "\\") != str(path_key or "").lower().replace(
            "/", "\\"
        ):
            return
        # Clear any file-backed pixmap; placeholder is memory-only.
        self.cover_label.clear()
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H))

    def _set_cover(self, path: Path | None) -> None:
        """Show placeholder immediately; decode/scale via CoverLoaderManager."""
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H))
        if path is None or not path.is_file():
            return
        from services.cover_loader import CoverLoaderManager

        mid = self.current_mod_id() or "0"
        token = f"detail:{mid}:{path}"
        prev = getattr(self, "_cover_token", "") or ""
        mgr = CoverLoaderManager.instance()
        if prev and prev != token:
            mgr.cancel(prev)
        self._cover_token = token
        if not getattr(self, "_cover_loader_connected", False):
            mgr.image_ready.connect(self._on_detail_cover_ready)
            self._cover_loader_connected = True
        managed = self._managed_path or path.parent
        mgr.request(
            token,
            managed,
            cover_ref=str(path),
            width=COVER_W,
            height=COVER_H,
        )

    def _on_detail_cover_ready(self, token: object, image: object) -> None:
        if str(token) != getattr(self, "_cover_token", ""):
            return
        from PySide6.QtGui import QImage

        if not isinstance(image, QImage) or image.isNull():
            return
        pix = QPixmap.fromImage(image)
        if pix.isNull():
            return
        self.cover_label.setPixmap(pix)

    def _open_folder(self) -> None:
        if self._managed_path is None:
            return
        folder = self._managed_path.resolve()
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def _update_offline_download_button(self) -> None:
        busy = self._offline_worker is not None and self._offline_worker.isRunning()
        plat = self._current_platform
        batch = bool(self._batch_mod_ids) and len(self._batch_mod_ids) > 1

        if busy or self._offline_batch_active:
            tip = "正在保存离线页面…"
            label = "保存中…"
            if not batch and plat == PLATFORM_NEXUS:
                tip = "正在导入离线页面…"
                label = "导入中…"
            self.btn_download_offline.setText(label)
            self.btn_download_offline.setToolTip(tip)
            self.btn_download_offline.setAccessibleName(tip)
            self.btn_download_offline.setEnabled(False)
            return
        if batch:
            has_saveable = any(
                supports_offline_page_download(p) and path.parts
                for _mid, path, p in self._batch_entries
            )
            self.btn_download_offline.setText("保存离线页面")
            self.btn_download_offline.setToolTip(
                "批量保存离线页面（Nexus 等仅支持导入的来源将静默跳过）"
                if has_saveable
                else "所选 Mod 均不支持自动保存离线页面"
            )
            self.btn_download_offline.setAccessibleName("保存离线页面")
            self.btn_download_offline.setEnabled(has_saveable)
            return
        if plat == PLATFORM_STEAM:
            label = "保存离线页面"
            tip = f"{label} — 抓取 Steam Workshop 网页并保存为离线页面"
        elif plat == PLATFORM_NEXUS:
            label = "导入离线页面"
            tip = (
                f"{label} — 选择浏览器另存为的 Nexus 网页 HTML"
                "（及同目录 *_files）导入为离线页面"
            )
        elif plat == PLATFORM_GITHUB:
            label = "保存离线页面"
            tip = f"{label} — 下载 GitHub 仓库网页快照（HTML + 资源）"
        elif plat == PLATFORM_MODIO:
            label = "保存离线页面"
            tip = f"{label} — 抓取 mod.io 网页并保存为离线页面"
        elif plat == PLATFORM_OTHER:
            label = "导入离线页面"
            tip = (
                f"{label} — 选择浏览器另存为的网页 HTML"
                "（*.html / *.mhtml）导入为离线页面"
            )
        else:
            label = "保存离线页面"
            tip = f"{label} — 下载来源网站页面快照"
        self.btn_download_offline.setText(label)
        self.btn_download_offline.setToolTip(tip)
        self.btn_download_offline.setAccessibleName(label)
        self.btn_download_offline.setEnabled(True)

    def _download_offline_page(self) -> None:
        if self._offline_worker is not None and self._offline_worker.isRunning():
            return
        if self._offline_batch_active:
            return
        # Multi-select: save for supported platforms; silently skip import-only.
        if self._batch_mod_ids and len(self._batch_mod_ids) > 1:
            self._start_batch_offline_save()
            return
        if self._managed_path is None or self._metadata is None:
            return
        if self._current_platform in (PLATFORM_NEXUS, PLATFORM_OTHER):
            self._import_nexus_offline_html()
            return
        from .offline_archive_thread import OfflineArchiveWorker

        worker = OfflineArchiveWorker(
            self._managed_path,
            platform=self._current_platform,
            published_file_id=self._metadata.published_file_id,
            metadata=self._metadata,
            library_root=self._library_root,
            parent=self,
        )
        worker.archive_started.connect(self._on_offline_archive_started)
        worker.archive_finished.connect(self._on_offline_archive_finished)
        worker.archive_failed.connect(self._on_offline_archive_failed)
        worker.finished.connect(self._on_offline_archive_thread_finished)
        self._offline_worker = worker
        self._update_offline_download_button()
        worker.start()

    def _start_batch_offline_save(self) -> None:
        """Queue auto-save for selected Mods; Nexus / import-only → continue."""
        queue: list[tuple[str, Path, str]] = []
        for mid, path, plat in self._batch_entries:
            # Import-only platforms (e.g. Nexus) — silent skip, never raise.
            if not supports_offline_page_download(plat):
                continue
            if not path.parts or not path.is_dir():
                continue
            queue.append((mid, path, plat))
        if not queue:
            return
        self._offline_batch_queue = queue
        self._offline_batch_errors = []
        self._offline_batch_active = True
        self._update_offline_download_button()
        self._run_next_offline_batch_item()

    def _run_next_offline_batch_item(self) -> None:
        if not self._offline_batch_queue:
            self._finish_offline_batch()
            return
        mid, path, plat = self._offline_batch_queue.pop(0)
        from .offline_archive_thread import OfflineArchiveWorker

        worker = OfflineArchiveWorker(
            path,
            platform=plat,
            published_file_id=mid,
            metadata=None,
            library_root=self._library_root,
            parent=self,
        )
        worker.archive_started.connect(self._on_offline_archive_started)
        worker.archive_finished.connect(self._on_offline_archive_finished)
        worker.archive_failed.connect(self._on_offline_archive_failed)
        worker.finished.connect(self._on_offline_archive_thread_finished)
        self._offline_worker = worker
        self._managed_path = path  # so finished handler can emit/update
        self._current_platform = plat
        self._update_offline_download_button()
        worker.start()

    def _finish_offline_batch(self) -> None:
        self._offline_batch_active = False
        self._offline_batch_queue = []
        errors = list(self._offline_batch_errors)
        self._offline_batch_errors = []
        self._managed_path = None
        self._update_offline_download_button()
        if errors:
            preview = "; ".join(errors[:3])
            if len(errors) > 3:
                preview += f" …（共 {len(errors)} 项）"
            QMessageBox.warning(self, "离线页面", f"部分保存失败：{preview}")

    def _import_nexus_offline_html(self) -> None:
        if self._managed_path is None or self._metadata is None:
            return
        plat = self._current_platform or PLATFORM_NEXUS
        dialog_title = (
            "选择离线页面"
            if plat == PLATFORM_OTHER
            else "选择 Nexus 离线页面"
        )
        path, _filter = QFileDialog.getOpenFileName(
            self,
            dialog_title,
            "",
            "Offline Web Page (*.html *.htm *.mhtml *.mht);;所有文件 (*.*)",
        )
        if not path:
            return
        from .offline_archive_thread import OfflineHtmlImportWorker

        worker = OfflineHtmlImportWorker(
            self._managed_path,
            path,
            platform=plat,
            published_file_id=self._metadata.published_file_id,
            library_root=self._library_root,
            parent=self,
        )
        worker.archive_started.connect(self._on_offline_archive_started)
        worker.archive_finished.connect(self._on_offline_archive_finished)
        worker.archive_failed.connect(self._on_offline_archive_failed)
        worker.finished.connect(self._on_offline_archive_thread_finished)
        self._offline_worker = worker
        self._update_offline_download_button()
        worker.start()

    def _on_offline_archive_started(self) -> None:
        if not self._offline_batch_active:
            self._refresh_offline_status_label(busy=True)
        self._update_offline_download_button()
        self._set_op_status("◌ 正在保存离线页面…", tone="warning")

    def _on_offline_archive_finished(self, path: str) -> None:
        del path
        if self._offline_batch_active:
            if self._managed_path is not None:
                self.offline_page_updated.emit(self._managed_path)
            self._update_offline_download_button()
            return
        if self._managed_path is not None:
            self.offline_page_updated.emit(self._managed_path)
            # Refresh local labels without full library rescan
            self.show_mod(self._managed_path)
        else:
            self._refresh_offline_status_label()
        self._update_offline_download_button()
        self._set_op_status("✓ 离线页面已保存", tone="success", auto_clear_ms=1800)

    def _on_offline_archive_failed(self, error: str) -> None:
        err = (error or "").strip() or "保存失败"
        if self._offline_batch_active:
            mid = ""
            if self._managed_path is not None:
                mid = self._managed_path.name
            self._offline_batch_errors.append(f"{mid or '?'}: {err}")
            self._update_offline_download_button()
            return
        self.view_offline.setText(f"[Offline] Failed — {err}")
        self._apply_tone(self.view_offline, "error")
        self._show_status_banner(f"离线页面保存失败：\n{err}", tone="error")
        self._set_op_status("⚠ 离线保存失败", tone="error", auto_clear_ms=2400)
        self._update_offline_download_button()

    def _on_offline_archive_thread_finished(self) -> None:
        self._offline_worker = None
        if self._offline_batch_active:
            self._run_next_offline_batch_item()
            return
        self._update_offline_download_button()

    def _open_offline(self) -> None:
        # Strict guards — never hand an empty / missing path to the OS browser.
        # Canonical resolver only (ignores stale metadata.offline_page_path).
        index = self._resolve_offline_page_path()
        if index is None:
            self._show_offline_missing_tooltip()
            return

        abs_path = str(Path(index).resolve())
        if not abs_path or not Path(abs_path).exists():
            self._show_offline_missing_tooltip()
            return

        # Keep in-memory metadata aligned with what we actually open.
        if self._metadata is not None:
            self._metadata.offline_page_path = abs_path

        # Prefer fromLocalFile so spaces / CJK paths (e.g. Anno 1800) encode correctly.
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(abs_path))
        if not ok:
            self._show_offline_missing_tooltip()

    def _open_steam(self) -> None:
        url = self._current_source_url()
        if not url:
            QMessageBox.information(self, "来源链接", "当前 Mod 没有可用的来源链接。")
            return
        webbrowser.open(url)

    def _current_source_url(self) -> str:
        if getattr(self, "_source_url_value", ""):
            return str(self._source_url_value).strip()
        if self._display_info and (self._display_info.source_url or "").strip():
            return self._display_info.source_url.strip()
        if self._metadata is not None and (self._metadata.url or "").strip():
            return str(self._metadata.url).strip()
        if self._metadata is not None and self._metadata.published_file_id:
            if self._current_platform == PLATFORM_STEAM:
                return self._metadata.workshop_url
        return ""

    def _copy_to_clipboard(self, text: str) -> bool:
        from PySide6.QtGui import QClipboard

        clip = QApplication.clipboard()
        if clip is None:
            return False
        clip.setText(text or "", QClipboard.Mode.Clipboard)
        return True

    def _copy_name(self) -> None:
        value = (getattr(self, "_name_value", "") or "").strip()
        if not value:
            QMessageBox.information(self, "复制", "当前没有可复制的名称。")
            return
        if self._copy_to_clipboard(value):
            QMessageBox.information(self, "复制", "已复制名称。")

    def _copy_id(self) -> None:
        value = (getattr(self, "_id_value", "") or "").strip()
        if not value:
            QMessageBox.information(self, "复制", "当前没有可复制的 ID。")
            return
        if self._copy_to_clipboard(value):
            QMessageBox.information(self, "复制", "已复制 ID。")

    def _copy_source_url(self) -> None:
        url = self._current_source_url()
        if not url:
            QMessageBox.information(self, "复制链接", "当前 Mod 没有可用的来源链接。")
            return
        if self._copy_to_clipboard(url):
            QMessageBox.information(self, "复制链接", "已复制来源链接。")
        else:
            QMessageBox.warning(self, "复制链接", "无法访问系统剪贴板。")

    def _file_names_for_copy(self) -> list[str]:
        info = self._display_info
        if info is not None:
            names = [
                (f.filename or f.name).strip()
                for f in info.mod_files.files
                if (f.filename or f.name).strip()
            ]
            if names:
                return names
        return []

    def _copy_mod_info(self) -> None:
        meta = self._metadata
        info = self._display_info
        if meta is None:
            return
        shown = info.display_name if info else meta.display_name
        steam = info.steam_name if info else (meta.title or "").strip()
        platform = (
            info.platform
            if info is not None
            else getattr(self, "_current_platform", PLATFORM_STEAM)
        )
        name = (shown or steam or "").strip()
        external_id = format_external_id(
            platform,
            (info.external_id if info else "") or "",
            source_url=self._current_source_url(),
            published_file_id=str(meta.published_file_id or ""),
        )
        payload = format_mod_info_clipboard(
            name=name,
            platform=platform,
            source_url=self._current_source_url(),
            external_id=external_id,
            files=self._file_names_for_copy(),
        )
        if self._copy_to_clipboard(payload):
            QMessageBox.information(self, "复制全部信息", "已复制 Mod 信息。")
        else:
            QMessageBox.warning(self, "复制全部信息", "无法访问系统剪贴板。")

    def _cancel_edit(self) -> None:
        if self._managed_path is not None:
            self._stack.setCurrentWidget(self._view_page)
            self._mode = MODE_VIEW
            self._fill_view()
        else:
            self.clear()

    def _save_edit(self) -> None:
        meta = self._metadata
        path = self._managed_path
        if meta is None or path is None:
            return
        mid = meta.published_file_id
        if not str(mid).isdigit():
            QMessageBox.warning(self, "保存失败", "缺少有效的 Mod ID。")
            return
        try:
            self._display_info = get_db().update_mod_user_metadata(
                mid,
                {
                    "display_name": self.edit_display_name.text(),
                    "custom_description": self.edit_custom_desc.toPlainText(),
                    "user_notes": self.edit_notes.toPlainText(),
                    "favorite": self.edit_favorite.isChecked(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self._stack.setCurrentWidget(self._view_page)
        self._mode = MODE_VIEW
        self._fill_view()
        self._persist_info_sidecar()
        self.metadata_saved.emit(path)

    # ------------------------------------------------------------------
    # Deploy UI (worker lives in library_view)
    # ------------------------------------------------------------------

    def current_mod_id(self) -> str:
        if self._metadata and str(self._metadata.published_file_id).isdigit():
            return str(self._metadata.published_file_id)
        return ""

    def _request_deploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.deploy_requested.emit(mid)

    def _request_redeploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.redeploy_requested.emit(mid)

    def _request_undeploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.undeploy_requested.emit(mid)

    def set_deploy_busy(self, busy: bool, *, action: str = "deploy") -> None:
        """Disable deploy buttons while a worker runs; show button + op status."""
        self._deploy_busy = bool(busy)
        if busy:
            label = {
                "undeploy": "正在取消部署…",
                "redeploy": "正在重新部署…",
            }.get(action, "正在部署…")
            self.view_deploy.setText(f"[Deploy] {label}")
            self._apply_tone(self.view_deploy, "warning")
            # Button-state feedback (anti double-click).
            self.btn_deploy.setText(
                "部署中…" if action == "deploy" else "部署"
            )
            self.btn_redeploy.setText(
                "部署中…" if action == "redeploy" else "重新部署"
            )
            self.btn_undeploy.setText(
                "取消中…" if action == "undeploy" else "取消部署"
            )
            for btn in (self.btn_deploy, self.btn_redeploy, self.btn_undeploy):
                btn.setEnabled(False)
            op = {
                "undeploy": "◌ 正在取消部署",
                "redeploy": "◌ 正在重新部署",
            }.get(action, "◌ 正在部署")
            self._set_op_status(op, tone="warning")
        else:
            self.btn_deploy.setText("部署")
            self.btn_undeploy.setText("取消部署")
            self._fill_deploy_status_from_db()

    def apply_deploy_result(self, result: dict) -> None:
        """Update panel from a DeployWorker result dict (no library rescan)."""
        self._deploy_busy = False
        self.btn_deploy.setText("部署")
        self.btn_undeploy.setText("取消部署")
        if not isinstance(result, dict):
            self.view_deploy.setText("[Deploy] 状态：部署失败")
            self._apply_tone(self.view_deploy, "error")
            self.view_deploy_error.setText("原因：未知错误")
            self._show_deploy_failure_banner("未知错误")
            self._set_deploy_buttons(DEPLOY_STATUS_FAILED)
            self._set_op_status("⚠ 部署失败", tone="error", auto_clear_ms=2400)
            return

        conflicts = result.get("conflicts")
        if isinstance(conflicts, dict):
            files = conflicts.get("files") or []
            entries = conflicts.get("conflicts") or []
            status = str(conflicts.get("status") or "")
            is_conflict = bool(conflicts.get("conflict")) or status in (
                CONFLICT_STATUS_WARNING,
                CONFLICT_STATUS_CONFLICT,
            )
            if is_conflict and (files or entries):
                n = len(files) if files else len(entries)
                sample = ""
                if files and isinstance(files[0], dict):
                    sample = str(files[0].get("existing_mod") or "")
                elif entries and isinstance(entries[0], dict):
                    mods = entries[0].get("mods") or []
                    mid_self = str(result.get("mod_id") or "")
                    sample = next(
                        (str(m) for m in mods if str(m) != mid_self),
                        str(mods[0]) if mods else "",
                    )
                self._conflict_hint = (
                    f"存在文件冲突（{n}）"
                    + (f"，例如已被 Mod {sample} 占用" if sample else "")
                    + "。本次仍继续部署，请人工确认。"
                )
            else:
                self._conflict_hint = ""
        else:
            self._conflict_hint = ""
        # Append relationship warnings (dependency disabled / known conflict)
        rel_warns = result.get("relationship_warnings") or []
        if isinstance(rel_warns, list) and rel_warns:
            extra = " | ".join(
                str(w.get("message") or "").replace("\n", " ").strip()
                for w in rel_warns
                if isinstance(w, dict) and w.get("message")
            )
            if extra:
                if self._conflict_hint:
                    self._conflict_hint = self._conflict_hint + " " + extra
                else:
                    self._conflict_hint = extra + "（仅提醒，未阻止部署）"
        self.view_deploy_conflict.setText(self._conflict_hint)

        if result.get("success"):
            self._hide_status_banner()
            self._fill_lifecycle_status()
            self._fill_relationships()

            # Undeploy success also returns success=True — refresh from DB instead
            # of assuming every success means "deployed".
            if "removed_files" in result:
                self.view_deploy_conflict.setText(self._conflict_hint)
                self._fill_deploy_status_from_db()
                self._set_op_status("✓ 已取消部署", tone="success", auto_clear_ms=1800)
                return

            self.view_deploy.setText("[Deploy] 状态：已部署")
            self._apply_tone(self.view_deploy, "success")
            self.view_deploy_error.clear()

            target = str(result.get("target") or "")
            self.view_deploy_path.setText(f"目标路径：{target}" if target else "目标路径：—")

            dtype = str(result.get("deploy_type") or "")
            self.view_deploy_type.setText(f"部署类型：{dtype}" if dtype else "部署类型：—")

            deploy_time = str(result.get("deploy_time") or "")
            if not deploy_time:
                try:
                    info = get_db().get_mod_deploy_info(self.current_mod_id())
                    deploy_time = str(info.deploy_time or "") if info else ""
                except Exception:  # noqa: BLE001
                    deploy_time = ""
            self.view_deploy_time.setText(
                f"部署时间：{deploy_time}" if deploy_time else "部署时间：—"
            )

            self._set_deploy_buttons(DEPLOY_STATUS_DEPLOYED)
            self._set_op_status("✓ 部署完成", tone="success", auto_clear_ms=1800)
            return

        # Keep the raw error for the banner — humanize preserves archive details.
        raw_error = str(result.get("error") or "").strip() or "未知错误"
        error = humanize_deploy_error(raw_error)
        self.view_deploy.setText("[Deploy] 状态：部署失败")
        self._apply_tone(self.view_deploy, "error")
        self.view_deploy_error.setText(f"原因：{error}")
        self._show_deploy_failure_banner(raw_error)
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self._set_deploy_buttons(DEPLOY_STATUS_FAILED)
        self._set_op_status("⚠ 部署失败", tone="error", auto_clear_ms=2400)

    def apply_deploy_failure(self, error: str) -> None:
        self._deploy_busy = False
        self.btn_deploy.setText("部署")
        self.btn_undeploy.setText("取消部署")
        raw = str(error or "").strip() or "未知错误"
        msg = humanize_deploy_error(raw)
        self.view_deploy.setText("[Deploy] 状态：部署失败")
        self._apply_tone(self.view_deploy, "error")
        self.view_deploy_error.setText(f"原因：{msg}")
        self._show_deploy_failure_banner(raw)
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self._set_deploy_buttons(DEPLOY_STATUS_FAILED)
        self._set_op_status("⚠ 部署失败", tone="error", auto_clear_ms=2400)

    def _set_deploy_buttons(self, status: str) -> None:
        mid_ok = bool(self.current_mod_id()) and self._mode == MODE_VIEW
        folder_ok = not getattr(self, "_folder_absent", False)
        busy = self._deploy_busy
        from services.deploy_status import (
            DEPLOYMENT_CONFLICT,
            DEPLOYMENT_DEPLOYED,
            DEPLOYMENT_OUTDATED,
            deploy_block_reason_for_content_status,
        )
        from services.library_status import (
            CONTENT_BACKUP_INVALID,
            CONTENT_FOLDER_MISSING,
            CONTENT_IDENTITY_CONFLICT,
            row_content_status,
        )

        content_blocked = False
        block_tip = ""
        try:
            brow = get_db().get_mod_backup_row(self.current_mod_id() or "")
            cs = row_content_status(brow)
            block_tip = deploy_block_reason_for_content_status(cs) or ""
            content_blocked = bool(block_tip) or cs in {
                CONTENT_FOLDER_MISSING,
                CONTENT_BACKUP_INVALID,
                CONTENT_IDENTITY_CONFLICT,
            }
        except Exception:  # noqa: BLE001
            pass

        deployed = status in (DEPLOY_STATUS_DEPLOYED, DEPLOYMENT_DEPLOYED, DEPLOYMENT_OUTDATED)
        outdated = status == DEPLOYMENT_OUTDATED
        conflict = status == DEPLOYMENT_CONFLICT
        # Deploy / redeploy require healthy source content; undeploy only needs
        # a recorded deployment (target cleanup is independent of source folder).
        deploy_can = mid_ok and folder_ok and not busy and not content_blocked
        undeploy_can = mid_ok and not busy

        self.btn_deploy.setEnabled(deploy_can and not deployed and not conflict)
        self.btn_redeploy.setEnabled(deploy_can and deployed and not conflict)
        self.btn_undeploy.setEnabled(undeploy_can and deployed and not conflict)
        self.btn_redeploy.setText("更新部署" if outdated else "重新部署")

        if content_blocked:
            tip = block_tip or "内容状态异常，无法部署"
            self.btn_deploy.setToolTip(tip)
            self.btn_redeploy.setToolTip(tip)
            if undeploy_can and deployed and not conflict:
                self.btn_undeploy.setToolTip("删除游戏中的部署（保留库内 Mod）")
            else:
                self.btn_undeploy.setToolTip(tip)
        elif not folder_ok and hasattr(self, "btn_deploy"):
            self.btn_deploy.setToolTip("内容目录不存在，无法部署")
            self.btn_redeploy.setToolTip("内容目录不存在，无法部署")
            if undeploy_can and deployed and not conflict:
                self.btn_undeploy.setToolTip("删除游戏中的部署（保留库内 Mod）")
            else:
                self.btn_undeploy.setToolTip("内容目录不存在，无法部署")
        elif conflict:
            tip = "目标目录冲突，无法部署"
            self.btn_deploy.setToolTip(tip)
            self.btn_redeploy.setToolTip(tip)
        else:
            self.btn_deploy.setToolTip("部署到游戏目录")
            self.btn_redeploy.setToolTip("更新部署" if outdated else "重新部署")
            self.btn_undeploy.setToolTip("删除游戏中的部署（保留库内 Mod）")

    def _resolve_deploy_type(self, mid: str) -> str:
        # Prefer manifest, then game config
        try:
            from services.deploy_rules import load_manifest

            if self._managed_path is not None:
                man = load_manifest(self._managed_path)
                if man and man.deploy_type:
                    return man.deploy_type
        except Exception:  # noqa: BLE001
            pass
        try:
            info = get_db().get_mod_deploy_info(mid)
            app_id = info.app_id if info else 0
            if not app_id and self._metadata:
                app_id = int(self._metadata.app_id or 0)
            if app_id:
                cfg = get_db().get_game_deploy_config(app_id)
                if cfg and cfg.deploy_type:
                    return cfg.deploy_type
        except Exception:  # noqa: BLE001
            pass
        return DEPLOY_TYPE_FOLDER_COPY

    def _fill_deploy_status_from_db(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            self._hide_status_banner()
            self.view_deploy.setText("[Deploy] 状态：—")
            self._apply_tone(self.view_deploy, "secondary")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            self.view_deploy_type.clear()
            self.view_deploy_error.clear()
            self.view_deploy_conflict.setText(self._conflict_hint)
            self._set_deploy_buttons(DEPLOY_STATUS_NOT_DEPLOYED)
            return
        try:
            info = get_db().get_mod_deploy_info(mid)
        except Exception:  # noqa: BLE001
            info = None

        dtype = self._resolve_deploy_type(mid)
        self.view_deploy_type.setText(f"部署类型：{dtype}")

        status = (
            info.deploy_status
            if info is not None
            else DEPLOY_STATUS_NOT_DEPLOYED
        ) or DEPLOY_STATUS_NOT_DEPLOYED

        # Prefer Phase 8 runtime deployment_status when Library root is known
        runtime = status
        try:
            from services.deploy_status import (
                DEPLOYMENT_CONFLICT,
                DEPLOYMENT_OUTDATED,
                resolve_deployment_status,
            )

            if self._library_root is not None:
                runtime = resolve_deployment_status(
                    mid,
                    library_root=self._library_root,
                    managed_path=self._managed_path,
                )
        except Exception:  # noqa: BLE001
            runtime = status

        if runtime == DEPLOYMENT_OUTDATED and info is not None:
            self._hide_status_banner()
            self.view_deploy.setText("[Deploy] 状态：需要更新")
            self._apply_tone(self.view_deploy, "warning")
            self.view_deploy_path.setText(
                f"目标路径：{info.deploy_path}" if info.deploy_path else "目标路径：—"
            )
            self.view_deploy_time.setText(
                f"部署时间：{info.deploy_time}" if info.deploy_time else "部署时间：—"
            )
            self.view_deploy_error.clear()
            status = DEPLOYMENT_OUTDATED
        elif runtime == DEPLOYMENT_CONFLICT:
            self.view_deploy.setText("[Deploy] 状态：冲突")
            self._apply_tone(self.view_deploy, "error")
            self.view_deploy_error.setText("目标目录已存在其他内容，无法部署")
            status = DEPLOYMENT_CONFLICT
        elif status == DEPLOY_STATUS_DEPLOYED and info is not None:
            self._hide_status_banner()
            self.view_deploy.setText("[Deploy] 状态：已部署")
            self._apply_tone(self.view_deploy, "success")
            self.view_deploy_path.setText(
                f"目标路径：{info.deploy_path}" if info.deploy_path else "目标路径：—"
            )
            self.view_deploy_time.setText(
                f"部署时间：{info.deploy_time}" if info.deploy_time else "部署时间：—"
            )
            self.view_deploy_error.clear()
        elif status == DEPLOY_STATUS_FAILED:
            self.view_deploy.setText("[Deploy] 状态：部署失败")
            self._apply_tone(self.view_deploy, "error")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            err = (info.deploy_error if info else "") or ""
            self.view_deploy_error.setText(f"原因：{err}" if err else "原因：—")
            self._show_deploy_failure_banner(err or "未知错误")
        else:
            self._hide_status_banner()
            self.view_deploy.setText("[Deploy] 状态：未部署")
            self._apply_tone(self.view_deploy, "secondary")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            self.view_deploy_error.clear()

        self.view_deploy_conflict.setText(self._conflict_hint)
        self._set_deploy_buttons(status)

def _placeholder(width: int, height: int) -> QPixmap:
    pix = QPixmap(width, height)
    pix.fill(QColor(BACKGROUND_BUTTON_PRESSED))
    painter = QPainter(pix)
    painter.setPen(QColor(BORDER_STRONG))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "No Preview")
    painter.end()
    return pix
