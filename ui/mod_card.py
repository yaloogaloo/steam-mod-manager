"""Card widget representing one managed Mod in the library grid (Phase B IA)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    get_db,
)
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_NONE,
    normalize_offline_status,
)
from core.models import ModMetadata
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    ModFileManager,
    read_is_missing_content,
)
from services.metadata_backup import is_mod_folder_absent
from ui.styles import (
    ACCENT_DISABLED_BG,
    ACCENT_DISABLED_BORDER,
    ACCENT_DISABLED_FG,
    ACCENT_ERROR,
    ACCENT_ERROR_BG,
    ACCENT_NEUTRAL_BG,
    ACCENT_NEUTRAL_BORDER,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    ACCENT_WARNING_BG,
    ACCENT_WARNING_BORDER,
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
    STATE_CONFLICT_BORDER,
    STATE_CONFLICT_FG,
    STATE_INVALID_BORDER,
    STATE_INVALID_FG,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

CARD_WIDTH = 200
COVER_HEIGHT = 112
COVER_WIDTH = CARD_WIDTH - 20
TEXT_WIDTH = CARD_WIDTH - 24
TITLE_LINES = 2
STATUS_STRIP_HEIGHT = 18
DEPLOY_DOT_SIZE = 10
DEPLOY_DOT_BORDER = "#121820"
OFFLINE_MISSING_LABEL = "Offline"


def _line_height(metrics: QFontMetrics) -> int:
    return max(metrics.height(), metrics.lineSpacing())


def _elide_to_lines(text: str, font: QFont, width: int, max_lines: int) -> str:
    """Wrap ``text`` to at most ``max_lines``, eliding the last line."""
    if not text or max_lines < 1 or width <= 0:
        return text
    metrics = QFontMetrics(font)

    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()

    lines: list[tuple[int, int]] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append((line.textStart(), line.textLength()))
        if len(lines) >= max_lines:
            break
    layout.endLayout()

    if not lines:
        return ""

    used = sum(length for _, length in lines)
    if used >= len(text) and len(lines) <= max_lines:
        return text

    if len(lines) < max_lines:
        return text

    start_last, _ = lines[-1]
    prefix = text[:start_last]
    remainder = text[start_last:]
    elided = metrics.elidedText(remainder, Qt.TextElideMode.ElideRight, width)
    return prefix + elided


class ModCardWidget(QFrame):
    """
    Quick-scan library card: Cover + Name + core status badges.

    Body text is limited to the display name. Identity / IDs / offline / deploy
    details live in cover overlays, status strip, and hover tooltip.
    """

    selection_requested = Signal(object)  # Path managed_path
    detail_requested = Signal(object)
    metadata_changed = Signal(object)
    edit_requested = Signal(object)  # Path
    deploy_requested = Signal(str)  # mod_id
    open_folder_requested = Signal(object)  # Path
    open_steam_requested = Signal(object)  # Path
    favorite_toggle_requested = Signal(str)  # mod_id
    context_menu_opening = Signal()
    set_category_requested = Signal(str)  # category label; "" = clear

    def __init__(
        self,
        managed_path: Path,
        metadata: ModMetadata | None = None,
        parent: QWidget | None = None,
        card_data=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("modCard")
        self.managed_path = Path(managed_path)
        self.metadata = metadata
        self._card_data = card_data
        self._selected = False
        self._category_options: list[str] = []
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(6)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_WIDTH, COVER_HEIGHT)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setScaledContents(False)
        self.cover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.cover_label.setPixmap(_placeholder_cover(COVER_WIDTH, COVER_HEIGHT))
        self._cover_token = ""
        layout.addWidget(self.cover_label)

        # Cover overlays (do not affect card height).
        self.state_badge = QLabel(self.cover_label)
        self.state_badge.setObjectName("modTagBadge")
        self.state_badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.state_badge.setFixedHeight(18)
        self.state_badge.move(4, 4)
        self.state_badge.hide()
        self.state_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Back-compat alias used by older badge helpers / tests.
        self.tag_badge = self.state_badge

        self.category_badge = QLabel(self.cover_label)
        self.category_badge.setObjectName("modCategoryBadge")
        self.category_badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.category_badge.move(4, 4)
        self.category_badge.hide()
        self.category_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.category_badge.setStyleSheet(
            "QLabel#modCategoryBadge {"
            "background-color: rgba(20, 24, 33, 0.85);"
            "color: #4da6ff;"
            "border: 1px solid rgba(77, 166, 255, 0.4);"
            "border-radius: 4px;"
            "padding: 2px 6px;"
            "font-size: 11px;"
            "font-weight: bold;"
            "}"
        )

        self.missing_badge = QLabel(self.cover_label)
        self.missing_badge.setObjectName("modMissingContentBadge")
        self.missing_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.missing_badge.setText("内容缺失")
        self.missing_badge.hide()
        self.missing_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.missing_badge.setStyleSheet(
            "QLabel#modMissingContentBadge {"
            "background-color: rgba(220, 53, 69, 0.85);"
            "color: white;"
            "border-radius: 6px;"
            "padding: 4px 10px;"
            "font-weight: bold;"
            "font-size: 12px;"
            "}"
        )

        self.platform_badge = QLabel(self.cover_label)
        self.platform_badge.setObjectName("modPlatformBadge")
        self.platform_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.platform_badge.setFixedHeight(18)
        self.platform_badge.hide()
        self.platform_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.relation_badge = QLabel(self.cover_label)
        self.relation_badge.setObjectName("modRelationBadge")
        self.relation_badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.relation_badge.setFixedHeight(16)
        self.relation_badge.hide()
        self.relation_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.deploy_dot = QLabel(self.cover_label)
        self.deploy_dot.setObjectName("deployDot")
        self.deploy_dot.setFixedSize(DEPLOY_DOT_SIZE, DEPLOY_DOT_SIZE)
        self.deploy_dot.hide()
        self.deploy_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.title_label = QLabel()
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        title_h = _line_height(QFontMetrics(title_font)) * TITLE_LINES
        self.title_label.setFixedHeight(title_h)
        self.title_label.setWordWrap(True)
        self.title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title_label)

        # Status strip: offline-missing only (fixed height for stable grid).
        self.status_strip = QWidget()
        self.status_strip.setFixedHeight(STATUS_STRIP_HEIGHT)
        self.status_strip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        strip_layout = QHBoxLayout(self.status_strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(4)

        self.offline_badge = QLabel(self.status_strip)
        self.offline_badge.setObjectName("cardOfflineBadge")
        self.offline_badge.setFixedHeight(STATUS_STRIP_HEIGHT)
        self.offline_badge.hide()
        self.offline_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        strip_layout.addWidget(self.offline_badge)
        strip_layout.addStretch(1)
        layout.addWidget(self.status_strip)

        margins = layout.contentsMargins()
        spacing = layout.spacing()
        # cover + title + status strip
        card_h = (
            margins.top()
            + margins.bottom()
            + COVER_HEIGHT
            + title_h
            + STATUS_STRIP_HEIGHT
            + spacing * 2
        )
        self.setFixedHeight(card_h)

        self.refresh_display()
        self.set_selected(False)
        from services.cover_loader import CoverLoaderManager

        CoverLoaderManager.instance().image_ready.connect(self._on_cover_image_ready)
        CoverLoaderManager.instance().path_release_requested.connect(
            self._on_cover_path_release_requested
        )
        self.destroyed.connect(self._on_card_destroyed)
        self._request_cover()

    def _on_cover_path_release_requested(self, path_key: str) -> None:
        """Cancel cover token and clear pixmap when this card's folder renames."""
        try:
            current = str(
                self.managed_path.expanduser().resolve()
                if self.managed_path.exists()
                else self.managed_path
            )
        except OSError:
            current = str(self.managed_path)
        if current.lower().replace("/", "\\") != str(path_key or "").lower().replace(
            "/", "\\"
        ):
            return
        from services.cover_loader import CoverLoaderManager

        tok = getattr(self, "_cover_token", "") or ""
        if tok:
            CoverLoaderManager.instance().cancel(tok)
        self._cover_token = ""
        self.cover_label.clear()
        self.cover_label.setPixmap(_placeholder_cover(COVER_WIDTH, COVER_HEIGHT))

    def _on_card_destroyed(self, *_args) -> None:
        """Cancel in-flight cover work and disconnect from the singleton loader."""
        try:
            from services.cover_loader import CoverLoaderManager

            mgr = CoverLoaderManager.instance()
            tok = getattr(self, "_cover_token", "") or ""
            if tok:
                mgr.cancel(tok)
            self._cover_token = ""
            try:
                mgr.image_ready.disconnect(self._on_cover_image_ready)
            except (RuntimeError, TypeError):
                pass
        except Exception:  # noqa: BLE001
            pass

    def rebind(
        self,
        managed_path: Path,
        metadata: ModMetadata | None = None,
        card_data=None,
    ) -> None:
        """Reuse this widget for another (or updated) Mod without recreating UI."""
        new_path = Path(managed_path)
        old_ref = ""
        if self.metadata and self.metadata.cover_path:
            old_ref = str(self.metadata.cover_path).strip()
        new_ref = ""
        if metadata and metadata.cover_path:
            new_ref = str(metadata.cover_path).strip()
        if card_data is not None:
            new_ref = str(getattr(card_data, "cover", "") or new_ref).strip()
        path_changed = new_path != self.managed_path
        cover_changed = path_changed or (new_ref != old_ref)

        self.managed_path = new_path
        self.metadata = metadata
        if card_data is not None:
            self._card_data = card_data
        self.refresh_display()
        if cover_changed:
            self.cover_label.setPixmap(_placeholder_cover(COVER_WIDTH, COVER_HEIGHT))
            self._request_cover()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            from ui.popup_trace import log_popup

            log_popup(
                "ModCardWidget.mousePressEvent",
                detail=str(self.managed_path),
            )
            self.selection_requested.emit(self.managed_path)
            self.detail_requested.emit(self.managed_path)
        super().mousePressEvent(event)

    def set_category_options(self, options: list[str]) -> None:
        """Game-scoped category labels for the context-menu submenu."""
        self._category_options = [
            str(o).strip() for o in (options or []) if str(o).strip()
        ]

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)

        act_detail = QAction("查看详情", menu)
        act_edit = QAction("编辑信息", menu)
        act_deploy = QAction("部署", menu)
        act_folder = QAction("打开目录", menu)
        act_steam = QAction("打开 Steam 页面", menu)
        fav = self._is_favorite()
        act_fav = QAction("取消收藏" if fav else "收藏", menu)

        act_detail.triggered.connect(self._emit_view_detail)
        act_edit.triggered.connect(
            lambda: self.edit_requested.emit(self.managed_path)
        )
        folder_absent = is_mod_folder_absent(self._mod_id(), self.managed_path)
        act_deploy.triggered.connect(self._emit_deploy)
        act_folder.triggered.connect(
            lambda: self.open_folder_requested.emit(self.managed_path)
        )
        act_steam.triggered.connect(
            lambda: self.open_steam_requested.emit(self.managed_path)
        )
        act_fav.triggered.connect(self._emit_favorite_toggle)

        menu.addAction(act_detail)
        menu.addAction(act_edit)
        menu.addSeparator()
        menu.addAction(act_deploy)
        menu.addAction(act_folder)
        menu.addAction(act_steam)
        if folder_absent:
            act_deploy.setEnabled(False)
            act_deploy.setToolTip("内容缺失，无法操作")
            act_folder.setEnabled(False)
            act_folder.setToolTip("内容缺失，无法操作")
            act_edit.setEnabled(False)
            act_edit.setToolTip("内容缺失，无法操作")
        menu.addSeparator()
        menu.addAction(act_fav)
        menu.addSeparator()

        cat_menu = menu.addMenu("设置分类")
        act_clear = cat_menu.addAction("（未分类）")
        act_clear.triggered.connect(
            lambda: self.set_category_requested.emit("")
        )
        seen: set[str] = set()
        for label in self._category_options:
            if not label or label in seen:
                continue
            seen.add(label)
            act = cat_menu.addAction(label)
            act.triggered.connect(
                lambda _=False, t=label: self.set_category_requested.emit(t)
            )
        return menu

    def _exec_context_menu(self, menu: QMenu, global_pos) -> None:
        menu.exec(global_pos)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.context_menu_opening.emit()
        menu = self._build_context_menu()
        try:
            self._exec_context_menu(menu, event.globalPos())
        finally:
            menu.hide()
            menu.deleteLater()
        event.accept()

    def _emit_view_detail(self) -> None:
        self.selection_requested.emit(self.managed_path)
        self.detail_requested.emit(self.managed_path)

    def _emit_deploy(self) -> None:
        mid = self._mod_id()
        if mid:
            self.deploy_requested.emit(mid)

    def _emit_favorite_toggle(self) -> None:
        mid = self._mod_id()
        if mid:
            self.favorite_toggle_requested.emit(mid)

    def _is_favorite(self) -> bool:
        data = getattr(self, "_card_data", None)
        if data is not None:
            return bool(data.favorite)
        mid = self._mod_id()
        if not mid:
            return False
        try:
            info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            return False
        return bool(info and info.favorite)

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.setProperty("selected", self._selected)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def add_selected_style(self) -> None:
        self.set_selected(True)

    def remove_selected_style(self) -> None:
        self.set_selected(False)

    def refresh_display(self) -> None:
        """Reload title / badges / tooltip from local metadata + SQLite."""
        self._apply_titles()
        self._render_state_badge()
        self._render_category_badge()
        self._render_platform_badge()
        self._render_deploy_indicator()
        self._render_offline_badge()
        self._render_relation_badge()
        self._render_missing_content_badge()
        self._render_tooltip()

    # Back-compat aliases (older tests / callers).
    def _apply_user_tag_badges(self) -> None:
        self._render_state_badge()

    def _apply_platform_badge(self) -> None:
        self._render_platform_badge()

    def _apply_deploy_status(self) -> None:
        self._render_deploy_indicator()

    def _apply_offline_status(self) -> None:
        self._render_offline_badge()

    def _apply_relation_badge(self) -> None:
        self._render_relation_badge()

    def _mod_id(self) -> str:
        data = getattr(self, "_card_data", None)
        if data is not None and str(getattr(data, "id", "") or "").strip():
            return str(data.id).strip()
        meta = self.metadata
        if meta and meta.published_file_id:
            return str(meta.published_file_id)
        if self.managed_path.name.isdigit():
            return self.managed_path.name
        try:
            from services.mod_metadata_resolver import resolve_mod_metadata

            resolved = resolve_mod_metadata(None, self.managed_path)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved is not None and resolved.published_file_id:
            if self.metadata is None:
                self.metadata = resolved.to_mod_metadata()
            return str(resolved.published_file_id)
        return ""

    def _display_info(self):
        data = getattr(self, "_card_data", None)
        if data is not None:
            from types import SimpleNamespace

            return SimpleNamespace(
                steam_name=data.steam_name,
                favorite=data.favorite,
                platform=data.platform,
                offline_status=data.offline_status,
                user_display_name=data.json_display_name or data.title,
                display_name=data.title,
            )
        mid = self._mod_id()
        if not mid:
            return None
        try:
            return get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            return None

    def _apply_titles(self) -> None:
        from ui.library_query import resolve_mod_library_title

        info = self._display_info()
        steam_name = ""
        db_display = ""
        favorite = False
        data = getattr(self, "_card_data", None)
        if data is not None:
            folder_ok = not bool(data.folder_absent)
        else:
            try:
                folder_ok = self.managed_path.is_dir()
            except OSError:
                folder_ok = False
        if info is not None:
            favorite = info.favorite
            if folder_ok:
                steam_name = (info.steam_name or "").strip()
                db_display = (info.user_display_name or "").strip()
        if not steam_name and self.metadata:
            steam_name = (self.metadata.title or "").strip()
        meta_display = (
            (self.metadata.json_display_name or "").strip() if self.metadata else ""
        )
        meta_title = (self.metadata.title or "").strip() if self.metadata else ""
        display = resolve_mod_library_title(
            metadata_display_name=meta_display,
            metadata_title=meta_title,
            db_display_name=db_display if folder_ok else "",
            db_steam_name=steam_name if folder_ok else "",
            folder_name=self.managed_path.name,
        )

        star = "★ " if favorite else ""
        shown = f"{star}{display}"
        self.title_label.setText(
            _elide_to_lines(shown, self.title_label.font(), TEXT_WIDTH, TITLE_LINES)
        )
        self._cached_display_name = display
        self._cached_steam_name = steam_name

    def _has_offline_page(self) -> bool:
        data = getattr(self, "_card_data", None)
        if data is not None:
            return bool(data.has_offline)
        from services.mod_metadata_resolver import resolve_offline_page

        found = resolve_offline_page(self._mod_id() or None, self.managed_path)
        if found is not None:
            return True
        ref = ""
        if self.metadata and self.metadata.offline_page_path:
            ref = str(self.metadata.offline_page_path).strip()
        if ref:
            try:
                return Path(ref).is_file()
            except OSError:
                return False
        return False

    def _offline_needs_attention(self) -> tuple[bool, str]:
        """
        Return ``(show_badge, tip)``.

        Show only when offline is missing / failed — never for successful sync.
        """
        info = self._display_info()
        status = OFFLINE_STATUS_NONE
        if info is not None:
            raw = getattr(info, "offline_status", OFFLINE_STATUS_NONE)
            if not isinstance(raw, str):
                raw = OFFLINE_STATUS_NONE
            status = normalize_offline_status(raw)
        has_page = self._has_offline_page()
        if status == OFFLINE_STATUS_FAILED:
            return True, "离线页面保存失败"
        if status in (OFFLINE_STATUS_GENERATED, OFFLINE_STATUS_ARCHIVED) or has_page:
            return False, "离线页面已保存"
        return True, "离线页面未保存"

    def _render_offline_badge(self) -> None:
        show, tip = self._offline_needs_attention()
        if not show:
            self.offline_badge.hide()
            self.offline_badge.clear()
            self.offline_badge.setToolTip("")
            return
        self.offline_badge.setText(OFFLINE_MISSING_LABEL)
        self.offline_badge.setToolTip(tip)
        self.offline_badge.setStyleSheet(
            f"QLabel#cardOfflineBadge {{"
            f"background-color: {ACCENT_WARNING_BG}; color: {ACCENT_WARNING};"
            f"border: 1px solid {ACCENT_WARNING_BORDER}; border-radius: 3px;"
            f"font-size: 10px; font-weight: 600; padding: 1px 5px;"
            f"}}"
        )
        self.offline_badge.adjustSize()
        self.offline_badge.show()

    def _render_state_badge(self) -> None:
        """Cover top-left: Conflict > Invalid > Disabled (mutually exclusive)."""
        mid = self._mod_id()
        conflict = False
        invalid = False
        disabled = False
        tip_parts: list[str] = []
        data = getattr(self, "_card_data", None)
        if data is not None:
            conflict = bool(data.conflict)
            invalid = bool(data.invalid)
            disabled = not bool(data.enabled)
            if conflict:
                tip_parts.append("存在冲突")
            if invalid:
                tip_parts.append("已失效")
            if disabled:
                tip_parts.append("已禁用")
        elif mid:
            try:
                st = get_db().get_mod_status(mid)
            except Exception:  # noqa: BLE001
                st = None
            try:
                enabled = get_db().is_mod_enabled(mid)
            except Exception:  # noqa: BLE001
                enabled = True
            disabled = not enabled
            if st is not None:
                if st.conflict_status in ("conflict", "warning"):
                    conflict = True
                    tip_parts.append(
                        "冲突"
                        + (
                            f"：{st.conflict_note}"
                            if (st.conflict_note or "").strip()
                            else ""
                        )
                    )
                if st.invalid:
                    invalid = True
                    tip_parts.append(
                        "已失效"
                        + (
                            f"：{st.invalid_reason}"
                            if (st.invalid_reason or "").strip()
                            else ""
                        )
                    )
            if not conflict and not invalid:
                try:
                    flags = get_db().get_mods_tag_flags([mid]).get(mid)
                except Exception:  # noqa: BLE001
                    flags = None
                if flags is not None:
                    if flags.conflict:
                        conflict = True
                        tip_parts.append("存在冲突")
                    if flags.invalid:
                        invalid = True
                        reason = (flags.invalid_reason or "").strip()
                        tip_parts.append("已失效" + (f"：{reason}" if reason else ""))

        if conflict:
            text, bg, fg, border = (
                "Conflict",
                ACCENT_ERROR_BG,
                STATE_CONFLICT_FG,
                STATE_CONFLICT_BORDER,
            )
        elif invalid:
            text, bg, fg, border = (
                "Invalid",
                ACCENT_WARNING_BG,
                STATE_INVALID_FG,
                STATE_INVALID_BORDER,
            )
        elif disabled:
            text, bg, fg, border = (
                "Disabled",
                ACCENT_DISABLED_BG,
                ACCENT_DISABLED_FG,
                ACCENT_DISABLED_BORDER,
            )
            tip_parts.append("已禁用")
        else:
            self.state_badge.hide()
            self.state_badge.clear()
            self.state_badge.setToolTip("")
            return

        self.state_badge.setText(text)
        self.state_badge.setToolTip("\n".join(tip_parts) if tip_parts else text)
        self.state_badge.setStyleSheet(
            f"QLabel#modTagBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 3px;"
            f"font-size: 10px; font-weight: 600; padding: 1px 5px;"
            f"}}"
        )
        self.state_badge.adjustSize()
        y = 4
        if self.category_badge.isVisible():
            y = self.category_badge.y() + self.category_badge.height() + 2
        self.state_badge.move(4, y)
        self.state_badge.show()
        self.state_badge.raise_()

    def _render_category_badge(self) -> None:
        mid = self._mod_id()
        label = ""
        data = getattr(self, "_card_data", None)
        if data is not None:
            label = str((data.category_tags or "").split()[:1][0] if (data.category_tags or "").split() else "").strip()
        elif mid:
            try:
                tags = get_db().get_category_tags(mid)
                label = str(tags[0] if tags else "").strip()
            except Exception:  # noqa: BLE001
                label = ""
        if not label:
            self.category_badge.hide()
            self.category_badge.clear()
            return
        self.category_badge.setText(label)
        self.category_badge.adjustSize()
        self.category_badge.move(4, 4)
        self.category_badge.show()
        self.category_badge.raise_()

    def _render_missing_content_badge(self) -> None:
        """Row-2 status badge (under source): healthy / missing / conflict."""
        from services.library_status import (
            CONTENT_CONTENT_MISSING,
            CONTENT_FOLDER_MISSING,
            CONTENT_HEALTHY,
            content_status_badge_label,
            content_status_badge_tip,
            library_status_to_content_status,
        )

        mid = self._mod_id()
        data = getattr(self, "_card_data", None)
        content_status = ""
        if data is not None:
            folder_absent = bool(data.folder_absent)
            missing_payload = bool(data.missing_content) and not folder_absent
            content_status = str(getattr(data, "content_status", "") or "").strip()
            if not content_status:
                content_status = library_status_to_content_status(
                    str(getattr(data, "library_status", "") or "")
                )
        else:
            folder_absent = is_mod_folder_absent(mid, self.managed_path)
            missing_payload = False if folder_absent else read_is_missing_content(
                self.managed_path
            )
        if not content_status:
            if folder_absent:
                content_status = CONTENT_FOLDER_MISSING
            elif missing_payload:
                content_status = CONTENT_CONTENT_MISSING
            else:
                content_status = CONTENT_HEALTHY

        status_label = content_status_badge_label(content_status)
        status_tip = content_status_badge_tip(content_status)
        if content_status != CONTENT_HEALTHY:
            status_label = f"⚠ {status_label}"

        self.missing_badge.setText(status_label)
        self.missing_badge.setToolTip(status_tip)
        self.missing_badge.adjustSize()
        cover_w = self.cover_label.width() or COVER_WIDTH
        # Row 2 under platform badge (top-right)
        x = max(4, cover_w - self.missing_badge.width() - 4)
        y = 24
        if hasattr(self, "platform_badge") and self.platform_badge.isVisible():
            y = self.platform_badge.y() + self.platform_badge.height() + 2
        self.missing_badge.move(x, y)
        # Hide "正常" to reduce noise — still available via tooltip on source row
        if content_status == CONTENT_HEALTHY:
            self.missing_badge.hide()
            return
        self.missing_badge.show()
        self.missing_badge.raise_()

    def _render_platform_badge(self) -> None:
        from services.library_status import (
            SOURCE_EXTERNAL,
            SOURCE_LOCAL,
            SOURCE_UNKNOWN,
            normalize_library_source,
            source_badge_label,
        )

        platform = "steam"
        data = getattr(self, "_card_data", None)
        if data is not None and str(getattr(data, "source_type", "") or "").strip():
            platform = str(data.source_type).strip()
        elif self.metadata and str(getattr(self.metadata, "source_type", "") or "").strip():
            platform = str(self.metadata.source_type).strip()
        else:
            info = self._display_info()
            if info is not None:
                platform = getattr(info, "platform", "steam") or "steam"
        key = normalize_library_source(platform)
        if key == SOURCE_UNKNOWN:
            key = str(platform).strip().lower() or "steam"

        styles = {
            "steam": (PLATFORM_STEAM_BG, PLATFORM_STEAM_FG, PLATFORM_STEAM_BORDER),
            "nexus": (PLATFORM_NEXUS_BG, PLATFORM_NEXUS_FG, PLATFORM_NEXUS_BORDER),
            "github": (PLATFORM_GITHUB_BG, PLATFORM_GITHUB_FG, PLATFORM_GITHUB_BORDER),
            "modio": (PLATFORM_MODIO_BG, PLATFORM_MODIO_FG, PLATFORM_MODIO_BORDER),
            "other": (PLATFORM_OTHER_BG, PLATFORM_OTHER_FG, PLATFORM_OTHER_BORDER),
            SOURCE_EXTERNAL: (ACCENT_NEUTRAL_BG, TEXT_SECONDARY, ACCENT_NEUTRAL_BORDER),
            SOURCE_LOCAL: (PLATFORM_OTHER_BG, PLATFORM_OTHER_FG, PLATFORM_OTHER_BORDER),
            SOURCE_UNKNOWN: (ACCENT_NEUTRAL_BG, TEXT_SECONDARY, ACCENT_NEUTRAL_BORDER),
        }
        text = source_badge_label(key) if key in (
            "steam", "nexus", "modio", "github", SOURCE_EXTERNAL, SOURCE_LOCAL, SOURCE_UNKNOWN
        ) else _platform_badge_label_fallback(key)
        bg, fg, border = styles.get(
            key, (ACCENT_NEUTRAL_BG, TEXT_SECONDARY, ACCENT_NEUTRAL_BORDER)
        )
        self.platform_badge.setText(text)
        self.platform_badge.setToolTip(f"来源：{text}")
        self.platform_badge.setStyleSheet(
            f"QLabel#modPlatformBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 3px;"
            f"font-size: 10px; font-weight: 600; padding: 1px 5px;"
            f"}}"
        )
        self.platform_badge.adjustSize()
        cover_w = self.cover_label.width() or COVER_WIDTH
        x = max(4, cover_w - self.platform_badge.width() - 4)
        self.platform_badge.move(x, 4)
        self.platform_badge.show()
        self.platform_badge.raise_()
        if self.category_badge.isVisible():
            self.category_badge.raise_()
        if self.state_badge.isVisible():
            self.state_badge.raise_()
        if self.relation_badge.isVisible():
            self.relation_badge.raise_()
        if hasattr(self, "missing_badge") and self.missing_badge.isVisible():
            self.missing_badge.raise_()
        if self.deploy_dot.isVisible():
            self.deploy_dot.raise_()

    def _render_deploy_indicator(self) -> None:
        """Cover bottom-right: green/red dot; hide when not deployed."""
        status = DEPLOY_STATUS_NOT_DEPLOYED
        tip = "尚未部署到游戏目录"
        mid = self._mod_id()
        data = getattr(self, "_card_data", None)
        if data is not None:
            status = data.deploy_status or DEPLOY_STATUS_NOT_DEPLOYED
            if status == DEPLOY_STATUS_DEPLOYED:
                tip = "已部署到游戏 Mod 目录"
            elif status == DEPLOY_STATUS_FAILED:
                tip = "最近一次部署失败"
        elif mid:
            try:
                info = get_db().get_mod_deploy_info(mid)
            except Exception:  # noqa: BLE001
                info = None
            if info is not None:
                status = info.deploy_status or DEPLOY_STATUS_NOT_DEPLOYED
                if status == DEPLOY_STATUS_DEPLOYED:
                    tip = info.deploy_path or "已部署到游戏 Mod 目录"
                elif status == DEPLOY_STATUS_FAILED:
                    tip = "最近一次部署失败"

        if status == DEPLOY_STATUS_DEPLOYED:
            color = ACCENT_SUCCESS
        elif status == DEPLOY_STATUS_FAILED:
            color = ACCENT_ERROR
        else:
            self.deploy_dot.hide()
            self.deploy_dot.clear()
            self.deploy_dot.setToolTip("")
            self._cached_deploy_label = "Not deployed"
            return

        self._cached_deploy_label = (
            "Installed" if status == DEPLOY_STATUS_DEPLOYED else "Failed"
        )
        self.deploy_dot.setToolTip(tip)
        self.deploy_dot.setStyleSheet(
            f"QLabel#deployDot {{"
            f"background-color: {color};"
            f"border: 1px solid {DEPLOY_DOT_BORDER};"
            f"border-radius: {DEPLOY_DOT_SIZE // 2}px;"
            f"}}"
        )
        cover_w = self.cover_label.width() or COVER_WIDTH
        cover_h = self.cover_label.height() or COVER_HEIGHT
        x = max(4, cover_w - DEPLOY_DOT_SIZE - 4)
        y = max(4, cover_h - DEPLOY_DOT_SIZE - 4)
        self.deploy_dot.move(x, y)
        self.deploy_dot.show()
        self.deploy_dot.raise_()

    def _render_relation_badge(self) -> None:
        """Cover bottom-left counts — overlay only, no layout height."""
        mid = self._mod_id()
        deps = 0
        confs = 0
        data = getattr(self, "_card_data", None)
        if data is not None:
            deps = int(data.relation_deps or 0)
            confs = int(data.relation_conflicts or 0)
        elif mid:
            try:
                counts = get_db().get_relationship_counts([mid]).get(mid, (0, 0))
                deps, confs = int(counts[0]), int(counts[1])
            except Exception:  # noqa: BLE001
                deps, confs = 0, 0
        parts: list[str] = []
        tips: list[str] = []
        if confs:
            parts.append(f"⚠ {confs}")
            tips.append(f"{confs} conflicts")
        if deps:
            parts.append(f"↑ {deps}")
            tips.append(f"{deps} dependencies")
        if not parts:
            self.relation_badge.hide()
            self.relation_badge.clear()
            self.relation_badge.setToolTip("")
            return
        self.relation_badge.setText("  ".join(parts))
        self.relation_badge.setToolTip("\n".join(tips))
        self.relation_badge.setStyleSheet(
            f"QLabel#modRelationBadge {{"
            f"background-color: rgba(20, 24, 32, 200);"
            f"color: {TEXT_PRIMARY};"
            f"border-radius: 3px;"
            f"font-size: 9px;"
            f"font-weight: 600;"
            f"padding: 0px 4px;"
            f"}}"
        )
        self.relation_badge.adjustSize()
        cover_h = self.cover_label.height() or COVER_HEIGHT
        y = max(4, cover_h - self.relation_badge.height() - 4)
        self.relation_badge.move(4, y)
        self.relation_badge.show()
        self.relation_badge.raise_()

    def _render_tooltip(self) -> None:
        """Simple title tooltip only — no yellow multi-line identity panel."""
        from ui.popup_trace import log_popup

        display = getattr(self, "_cached_display_name", "") or ""
        tip = display or self.managed_path.name
        log_popup("ModCardWidget.setToolTip", detail=repr(tip[:80]))
        self.setToolTip(tip)
        self.title_label.setToolTip(tip)

    def _request_cover(self) -> None:
        from services.cover_loader import CoverLoaderManager

        data = getattr(self, "_card_data", None)
        cover_ref = str(getattr(data, "cover", "") or "").strip() if data is not None else ""
        if not cover_ref:
            found = self._resolve_cover()
            cover_ref = str(found) if found is not None else ""
        if not cover_ref and self.metadata and self.metadata.cover_path:
            cover_ref = str(self.metadata.cover_path).strip()
        token = f"{id(self)}:{self.managed_path.resolve() if self.managed_path.exists() else self.managed_path}"
        prev = self._cover_token
        self._cover_token = token
        mgr = CoverLoaderManager.instance()
        if prev:
            mgr.cancel(prev)
        mgr.request(
            token,
            self.managed_path,
            cover_ref=cover_ref,
            width=COVER_WIDTH,
            height=COVER_HEIGHT,
        )

    def _on_cover_image_ready(self, token: str, image: object) -> None:
        if str(token) != getattr(self, "_cover_token", ""):
            return
        if not isinstance(image, QImage) or image.isNull():
            return
        if not self._cover_widget_alive():
            return
        self._apply_cover_image(image)

    def _cover_widget_alive(self) -> bool:
        try:
            from shiboken6 import isValid

            if not isValid(self):
                return False
            label = getattr(self, "cover_label", None)
            return label is not None and isValid(label)
        except Exception:  # noqa: BLE001
            return False

    def _apply_cover_image(self, qimage: QImage) -> None:
        """GUI-thread only: convert background QImage to QPixmap and paint."""
        if not self._cover_widget_alive():
            return
        target_w = COVER_WIDTH
        target_h = COVER_HEIGHT
        x = max(0, (qimage.width() - target_w) // 2)
        y = max(0, (qimage.height() - target_h) // 2)
        try:
            pixmap = QPixmap.fromImage(qimage).copy(x, y, target_w, target_h)
            self.cover_label.setPixmap(pixmap)
        except RuntimeError:
            return

    def _resolve_cover(self) -> Path | None:
        from services.mod_metadata_resolver import resolve_cover_path

        found = resolve_cover_path(self._mod_id() or None, self.managed_path)
        if found is not None:
            return found
        ref = ""
        if self.metadata and self.metadata.cover_path:
            ref = str(self.metadata.cover_path).strip()
        if not ref:
            return None
        try:
            direct = Path(ref)
            if not direct.is_file():
                return None
            if INFO_DIR_NAME in direct.parts and not self.managed_path.is_dir():
                return None
            return direct.resolve()
        except OSError:
            return None

    def _set_cover(self, path: Path | None) -> None:
        """Synchronous fallback — load via bytes to avoid Windows file locks."""
        target_w = COVER_WIDTH
        target_h = COVER_HEIGHT
        if path and path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            pixmap = QPixmap()
            if data and pixmap.loadFromData(data) and not pixmap.isNull():
                scaled = pixmap.scaled(
                    target_w,
                    target_h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = max(0, (scaled.width() - target_w) // 2)
                y = max(0, (scaled.height() - target_h) // 2)
                self.cover_label.setPixmap(scaled.copy(x, y, target_w, target_h))
                return
        self.cover_label.setPixmap(_placeholder_cover(target_w, target_h))


def _platform_badge_label_fallback(key: str) -> str:
    from ui.platform_labels import platform_badge_label

    return platform_badge_label(key)


def _placeholder_cover(width: int, height: int) -> QPixmap:
    from ui.styles import BACKGROUND_BUTTON_PRESSED, BORDER_STRONG

    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(BACKGROUND_BUTTON_PRESSED))
    painter = QPainter(pixmap)
    painter.setPen(QColor(BORDER_STRONG))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "No Preview")
    painter.end()
    return pixmap
