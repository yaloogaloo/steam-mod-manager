"""Card widget representing one managed Mod in the library grid."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
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
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, ModFileManager

CARD_WIDTH = 200
COVER_HEIGHT = 112
TEXT_WIDTH = CARD_WIDTH - 24
TITLE_LINES = 2
OFFLINE_INDEX_NAME = "index.html"
OFFLINE_SNAPSHOT_DIR = "offline"
OFFLINE_MISSING_LABEL = "离线页未同步"


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
    """Visual card for a single managed Workshop mod (selection → detail panel)."""

    selection_requested = Signal(object)  # Path managed_path
    # Kept for compatibility with older callers / tests.
    detail_requested = Signal(object)
    metadata_changed = Signal(object)
    # Context-menu actions — library_view wires these to existing handlers.
    edit_requested = Signal(object)  # Path
    deploy_requested = Signal(str)  # mod_id
    open_folder_requested = Signal(object)  # Path
    open_steam_requested = Signal(object)  # Path
    favorite_toggle_requested = Signal(str)  # mod_id

    def __init__(
        self,
        managed_path: Path,
        metadata: ModMetadata | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("modCard")
        self.managed_path = Path(managed_path)
        self.metadata = metadata
        self._selected = False
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(CARD_WIDTH - 20, COVER_HEIGHT)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setScaledContents(False)
        self.cover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._set_cover(self._resolve_cover())
        layout.addWidget(self.cover_label)

        # User-tag badges overlaid on cover — does not change card height/layout.
        self.tag_badge = QLabel(self.cover_label)
        self.tag_badge.setObjectName("modTagBadge")
        self.tag_badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.tag_badge.setFixedHeight(18)
        self.tag_badge.move(4, 4)
        self.tag_badge.hide()
        self.tag_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Platform badge — overlay top-right; does not change card height.
        self.platform_badge = QLabel(self.cover_label)
        self.platform_badge.setObjectName("modPlatformBadge")
        self.platform_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.platform_badge.setFixedHeight(18)
        self.platform_badge.hide()
        self.platform_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Relationship counts — overlay bottom-left of cover (no height change).
        self.relation_badge = QLabel(self.cover_label)
        self.relation_badge.setObjectName("modRelationBadge")
        self.relation_badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.relation_badge.setFixedHeight(16)
        self.relation_badge.hide()
        self.relation_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

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

        self.steam_label = QLabel()
        self.steam_label.setStyleSheet("color: #6b7c8f; font-size: 11px;")
        steam_h = _line_height(self.steam_label.fontMetrics())
        self.steam_label.setFixedHeight(steam_h)
        self.steam_label.setWordWrap(False)
        self.steam_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.steam_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.steam_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.steam_label)

        self.meta_label = QLabel()
        self.meta_label.setStyleSheet("color: #6b7c8f; font-size: 11px;")
        meta_h = _line_height(self.meta_label.fontMetrics())
        self.meta_label.setFixedHeight(meta_h)
        self.meta_label.setWordWrap(False)
        self.meta_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.meta_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.meta_label)

        # Offline status (fixed height)
        self.offline_label = QLabel()
        self.status_label = self.offline_label
        self.offline_label.setStyleSheet("color: #c9a227; font-size: 11px;")
        offline_h = _line_height(self.offline_label.fontMetrics())
        self.offline_label.setFixedHeight(offline_h)
        self.offline_label.setWordWrap(False)
        self.offline_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.offline_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.offline_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.offline_label)

        # Deploy badge (grey / green / red)
        self.deploy_badge = QLabel()
        self.deploy_badge.setObjectName("deployBadge")
        self.deploy_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.deploy_badge.setFixedHeight(22)
        self.deploy_badge.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.deploy_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.deploy_badge)

        margins = layout.contentsMargins()
        spacing = layout.spacing()
        sections = 6  # cover, title, steam, meta, offline, deploy badge
        badge_h = 22
        card_h = (
            margins.top()
            + margins.bottom()
            + COVER_HEIGHT
            + title_h
            + steam_h
            + meta_h
            + offline_h
            + badge_h
            + spacing * (sections - 1)
        )
        self.setFixedHeight(card_h)

        self._apply_titles()
        self._apply_offline_status()
        self._apply_deploy_status()
        self._apply_user_tag_badges()
        self._apply_relation_badge()
        self._apply_platform_badge()
        self.set_selected(False)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.selection_requested.emit(self.managed_path)
            self.detail_requested.emit(self.managed_path)
        super().mousePressEvent(event)

    def _build_context_menu(self) -> QMenu:
        """Build a card-owned context menu (never parent=None)."""
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
        menu.addSeparator()
        menu.addAction(act_fav)
        return menu

    def _exec_context_menu(self, menu: QMenu, global_pos) -> None:
        """Show popup at ``global_pos`` (never bare ``exec()``)."""
        menu.exec(global_pos)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = self._build_context_menu()
        try:
            # Always pass a global position — bare exec() can open at (0,0).
            self._exec_context_menu(menu, event.globalPos())
        finally:
            # Destroy after close so the popup cannot linger as a ghost window
            # when the card is later reparented / cleared.
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
        if self._selected:
            self.setProperty("selected", True)
            self.setStyleSheet(
                "QFrame#modCard {"
                "background-color: #1e2a3a;"
                "border: 2px solid #66c0f4;"
                "border-radius: 10px;"
                "}"
            )
        else:
            self.setProperty("selected", False)
            self.setStyleSheet("")
        self.style().unpolish(self)
        self.style().polish(self)

    def add_selected_style(self) -> None:
        self.set_selected(True)

    def remove_selected_style(self) -> None:
        self.set_selected(False)

    def refresh_display(self) -> None:
        """Reload titles / offline / deploy status from local metadata + SQLite."""
        self._apply_titles()
        self._apply_offline_status()
        self._apply_deploy_status()
        self._apply_user_tag_badges()
        self._apply_relation_badge()
        self._apply_platform_badge()

    def _mod_id(self) -> str:
        meta = self.metadata
        if meta and meta.published_file_id:
            return str(meta.published_file_id)
        if self.managed_path.name.isdigit():
            return self.managed_path.name
        # Local fallback for context menu when metadata was not passed in
        try:
            root = (
                self.managed_path.parents[1]
                if len(self.managed_path.parts) > 1
                else self.managed_path.parent
            )
            loaded = ModFileManager(root).load_metadata(self.managed_path)
        except Exception:  # noqa: BLE001
            loaded = None
        if loaded and loaded.published_file_id:
            self.metadata = loaded
            return str(loaded.published_file_id)
        return ""

    def _apply_titles(self) -> None:
        mod_id = self._mod_id()
        steam_name = ""
        display = ""
        favorite = False

        if mod_id:
            try:
                info = get_db().get_mod_display_info(mod_id)
            except Exception:  # noqa: BLE001
                info = None
            if info is not None:
                steam_name = info.steam_name
                display = info.display_name
                favorite = info.favorite

        if not steam_name and self.metadata:
            steam_name = (self.metadata.title or "").strip()
        if not display:
            display = steam_name or (
                self.metadata.effective_title()
                if self.metadata
                else self.managed_path.name
            )

        star = "★ " if favorite else ""
        shown = f"{star}{display}"
        self.title_label.setText(
            _elide_to_lines(shown, self.title_label.font(), TEXT_WIDTH, TITLE_LINES)
        )
        tip_parts = [display]
        if steam_name and steam_name != display:
            tip_parts.append(f"Steam: {steam_name}")
        if mod_id:
            tip_parts.append(f"Workshop ID: {mod_id}")
        self.title_label.setToolTip("\n".join(tip_parts))

        # Always reserve Steam Name row (elide + full tooltip).
        steam_full = steam_name or "—"
        steam_line = f"Steam: {steam_full}"
        metrics = self.steam_label.fontMetrics()
        self.steam_label.setText(
            metrics.elidedText(steam_line, Qt.TextElideMode.ElideRight, TEXT_WIDTH)
        )
        self.steam_label.setToolTip(steam_full if steam_name else "")

        id_full = f"Workshop ID: {mod_id}" if mod_id else "Workshop ID: —"
        self.meta_label.setText(
            metrics.elidedText(id_full, Qt.TextElideMode.ElideRight, TEXT_WIDTH)
            if mod_id
            else "Workshop ID: —"
        )
        self.meta_label.setToolTip(id_full if mod_id else "")

    def _has_offline_page(self) -> bool:
        """Existence check only — does not read HTML or hit the network."""
        root = self.managed_path
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            for relative in (
                (info_name, OFFLINE_SNAPSHOT_DIR, OFFLINE_INDEX_NAME),
                (info_name, OFFLINE_INDEX_NAME),
            ):
                index = root.joinpath(*relative)
                try:
                    if index.is_file() and index.stat().st_size > 0:
                        return True
                except OSError:
                    continue
        return False

    def _apply_offline_status(self) -> None:
        if self._has_offline_page():
            self.offline_label.setText("离线页已同步")
            self.offline_label.setStyleSheet("color: #6b9e78; font-size: 11px;")
            self.offline_label.setToolTip("本地已存在离线页面文件")
            return
        self.offline_label.setText(OFFLINE_MISSING_LABEL)
        self.offline_label.setStyleSheet("color: #c9a227; font-size: 11px;")
        self.offline_label.setToolTip(
            "请在「同步中心」使用「同步 Steam 离线网页」补全"
        )

    def _apply_deploy_status(self) -> None:
        """Show a grey / green / red deploy badge (independent of offline line)."""
        status = DEPLOY_STATUS_NOT_DEPLOYED
        tip = "尚未部署到游戏目录"
        mod_id = self._mod_id()
        if mod_id:
            try:
                info = get_db().get_mod_deploy_info(mod_id)
            except Exception:  # noqa: BLE001
                info = None
            if info is not None:
                status = info.deploy_status or DEPLOY_STATUS_NOT_DEPLOYED
                if status == DEPLOY_STATUS_DEPLOYED:
                    tip = info.deploy_path or "已部署到游戏 Mod 目录"
                elif status == DEPLOY_STATUS_FAILED:
                    tip = "最近一次部署失败"

        if status == DEPLOY_STATUS_DEPLOYED:
            text, bg, fg, border = "已部署", "#1a3d2e", "#6b9e78", "#2d6b4f"
        elif status == DEPLOY_STATUS_FAILED:
            text, bg, fg, border = "部署失败", "#3d1a1a", "#e07070", "#8b3a3a"
        else:
            text, bg, fg, border = "未部署", "#2a3038", "#8b9bb0", "#3d4654"

        self.deploy_badge.setText(text)
        self.deploy_badge.setToolTip(tip)
        self.deploy_badge.setStyleSheet(
            f"QLabel#deployBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 4px;"
            f"font-size: 11px; font-weight: 600; padding: 1px 6px;"
            f"}}"
        )

    def _apply_user_tag_badges(self) -> None:
        """
        Overlay status badges on cover — fixed height, no layout growth.

        Priority: Conflict > Invalid > Disabled. Platform stays separate.
        """
        mid = self._mod_id()
        conflict = False
        invalid = False
        disabled = False
        tip_parts: list[str] = []
        if mid:
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
            text, bg, fg, border = ("Conflict", "#3a1418", "#ff6b6b", "#8b2e2e")
        elif invalid:
            text, bg, fg, border = ("Invalid", "#3a2410", "#f0a040", "#8b5a20")
        elif disabled:
            text, bg, fg, border = ("Disabled", "#2a2a2a", "#b0b0b0", "#555555")
            tip_parts.append("已禁用")
        else:
            self.tag_badge.hide()
            self.tag_badge.clear()
            self.tag_badge.setToolTip("")
            return

        self.tag_badge.setText(text)
        self.tag_badge.setToolTip("\n".join(tip_parts) if tip_parts else text)
        self.tag_badge.setStyleSheet(
            f"QLabel#modTagBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 3px;"
            f"font-size: 10px; font-weight: 600; padding: 1px 5px;"
            f"}}"
        )
        self.tag_badge.adjustSize()
        self.tag_badge.move(4, 4)
        self.tag_badge.show()
        self.tag_badge.raise_()

    def _apply_relation_badge(self) -> None:
        """Small dependency / conflict count overlay — fixed cover height."""
        mid = self._mod_id()
        deps = 0
        confs = 0
        if mid:
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
            "QLabel#modRelationBadge {"
            "background-color: rgba(20, 24, 32, 200);"
            "color: #e8eef5;"
            "border-radius: 3px;"
            "font-size: 9px;"
            "font-weight: 600;"
            "padding: 0px 4px;"
            "}"
        )
        self.relation_badge.adjustSize()
        cover_h = self.cover_label.height() or COVER_HEIGHT
        y = max(4, cover_h - self.relation_badge.height() - 4)
        self.relation_badge.move(4, y)
        self.relation_badge.show()
        self.relation_badge.raise_()

    def _apply_platform_badge(self) -> None:
        """Overlay [Steam]/[Nexus]/[GitHub] — no layout height change."""
        platform = "steam"
        mid = self._mod_id()
        if mid:
            try:
                info = get_db().get_mod_display_info(mid)
            except Exception:  # noqa: BLE001
                info = None
            if info is not None:
                platform = getattr(info, "platform", "steam") or "steam"
        key = str(platform).strip().lower()
        from ui.platform_labels import platform_badge_label

        styles = {
            "steam": ("#1b2838", "#66c0f4", "#2a475e"),
            "nexus": ("#2a1f14", "#d4a017", "#6b4f1d"),
            "github": ("#1c1c1c", "#c9d1d9", "#484f58"),
        }
        text = platform_badge_label(key)
        bg, fg, border = styles.get(key, ("#2a3038", "#8b9bb0", "#3d4654"))
        self.platform_badge.setText(text)
        self.platform_badge.setToolTip(f"平台：{text}")
        self.platform_badge.setStyleSheet(
            f"QLabel#modPlatformBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border: 1px solid {border}; border-radius: 3px;"
            f"font-size: 10px; font-weight: 600; padding: 1px 5px;"
            f"}}"
        )
        self.platform_badge.adjustSize()
        cover_w = self.cover_label.width() or (CARD_WIDTH - 20)
        x = max(4, cover_w - self.platform_badge.width() - 4)
        self.platform_badge.move(x, 4)
        self.platform_badge.show()
        self.platform_badge.raise_()
        # Keep status / relation badges above platform when visible
        if self.tag_badge.isVisible():
            self.tag_badge.raise_()
        if self.relation_badge.isVisible():
            self.relation_badge.raise_()

    def _resolve_cover(self) -> Path | None:
        manager = ModFileManager(
            self.managed_path.parents[1]
            if len(self.managed_path.parts) > 1
            else self.managed_path.parent
        )
        found = manager.find_local_cover(self.managed_path)
        if found:
            return found
        if self.metadata and self.metadata.cover_path:
            path = Path(self.metadata.cover_path)
            if path.is_file():
                return path
        return None

    def _set_cover(self, path: Path | None) -> None:
        target_w = CARD_WIDTH - 20
        target_h = COVER_HEIGHT
        if path and path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
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


def _placeholder_cover(width: int, height: int) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#1b2838"))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#3d5a73"))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "No Preview")
    painter.end()
    return pixmap
