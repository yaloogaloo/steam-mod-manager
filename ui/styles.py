"""Application-wide Qt stylesheets and Design Tokens (Phase A)."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Design Tokens — single color source for Library UI
# ---------------------------------------------------------------------------

BACKGROUND_PRIMARY = "#121820"
BACKGROUND_SIDEBAR = "#171e28"
BACKGROUND_PANEL = "#151c26"
BACKGROUND_CARD = "#171e28"
BACKGROUND_CARD_SELECTED = "#1e2a3a"
BACKGROUND_SECTION = "#1a2330"
BACKGROUND_INPUT = "#1a2330"
BACKGROUND_BUTTON = "#2a475e"
BACKGROUND_BUTTON_HOVER = "#3d6a8a"
BACKGROUND_BUTTON_PRESSED = "#1b2838"
BACKGROUND_BUTTON_DISABLED = "#243040"
BACKGROUND_OVERLAY = "rgba(18, 24, 32, 220)"
BACKGROUND_OVERLAY_LIGHT = "rgba(18, 24, 32, 180)"

TEXT_PRIMARY = "#e8eef5"
TEXT_SECONDARY = "#8b9bb0"
TEXT_BODY = "#c7d5e0"
TEXT_MUTED = "#6b7c8f"
TEXT_STATUS = "#a8b8c8"

BORDER_SUBTLE = "#243044"
BORDER_DEFAULT = "#2c3a4d"
BORDER_STRONG = "#3d5a73"
BORDER_FOCUS = "#66c0f4"

ACCENT_PRIMARY = "#66c0f4"
ACCENT_PRIMARY_HOVER = "#8ed0f8"
ACCENT_PRIMARY_ON = "#0b1520"
ACCENT_PRIMARY_DISABLED = "#3a5568"
ACCENT_PRIMARY_DISABLED_TEXT = "#9bb0c0"
ACCENT_SELECTION = "#3d7ea6"

# Unified semantic colors (Phase A — single source)
ACCENT_SUCCESS = "#3fb950"
ACCENT_WARNING = "#d4a017"
ACCENT_ERROR = "#e06c75"

ACCENT_SUCCESS_BG = "#1a3d2e"
ACCENT_SUCCESS_BORDER = "#2d6b4f"
ACCENT_WARNING_BG = "#3a2410"
ACCENT_WARNING_BORDER = "#8b5a20"
ACCENT_ERROR_BG = "#3a1418"
ACCENT_ERROR_BORDER = "#8b3a3a"
ACCENT_NEUTRAL_BG = "#2a3038"
ACCENT_NEUTRAL_BORDER = "#3d4654"
ACCENT_DISABLED_BG = "#2a2a2a"
ACCENT_DISABLED_FG = "#b0b0b0"
ACCENT_DISABLED_BORDER = "#555555"

# Platform badge colors (allowed special-case — not semantic status)
PLATFORM_STEAM_BG = "#1b2838"
PLATFORM_STEAM_FG = ACCENT_PRIMARY
PLATFORM_STEAM_BORDER = BACKGROUND_BUTTON
PLATFORM_NEXUS_BG = "#2a1f14"
PLATFORM_NEXUS_FG = ACCENT_WARNING
PLATFORM_NEXUS_BORDER = "#6b4f1d"
PLATFORM_GITHUB_BG = "#1c1c1c"
PLATFORM_GITHUB_FG = "#c9d1d9"
PLATFORM_GITHUB_BORDER = "#484f58"
PLATFORM_MODIO_BG = "#14241f"
PLATFORM_MODIO_FG = "#3dd68c"
PLATFORM_MODIO_BORDER = "#1f6b4a"
PLATFORM_OTHER_BG = "#222833"
PLATFORM_OTHER_FG = "#a8b8c8"
PLATFORM_OTHER_BORDER = "#3d4654"

# Conflict / Invalid badge accents (state badges — dynamic OK)
STATE_CONFLICT_FG = "#ff6b6b"
STATE_CONFLICT_BORDER = "#8b2e2e"
STATE_INVALID_FG = "#f0a040"
STATE_INVALID_BORDER = "#8b5a20"


def _build_app_style() -> str:
    return f"""
QWidget {{
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}

QMainWindow, QDialog {{
    background-color: {BACKGROUND_PRIMARY};
    color: {TEXT_PRIMARY};
}}

QDialog {{
    background-color: {BACKGROUND_PRIMARY};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_DEFAULT};
}}

QMessageBox {{
    background-color: {BACKGROUND_PRIMARY};
    color: {TEXT_PRIMARY};
}}

QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
    background-color: transparent;
}}

/* Popups — never fall back to native white panels */
QMenu {{
    background-color: {BACKGROUND_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_DEFAULT};
    padding: 4px;
}}

QMenu::item {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    padding: 8px 28px 8px 16px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {BACKGROUND_BUTTON};
    color: {TEXT_PRIMARY};
}}

QMenu::item:disabled {{
    color: {TEXT_MUTED};
    background-color: transparent;
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER_SUBTLE};
    margin: 4px 8px;
}}

QToolTip {{
    /* No border-radius: on Windows Fusion it punches a white hole behind the tip. */
    background-color: {BACKGROUND_SECTION};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_DEFAULT};
    padding: 6px 8px;
    border-radius: 0px;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QSplitter::handle {{
    background-color: {BORDER_SUBTLE};
}}

QSplitter::handle:horizontal {{
    width: 2px;
}}

QSplitter::handle:vertical {{
    height: 2px;
}}

QLineEdit {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 8px 10px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_SELECTION};
}}

QLineEdit:focus {{
    border-color: {BORDER_FOCUS};
}}

QTextEdit, QTextBrowser {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 8px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_SELECTION};
}}

QTextEdit:focus, QTextBrowser:focus {{
    border-color: {BORDER_FOCUS};
}}

QCheckBox {{
    color: {TEXT_PRIMARY};
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 3px;
    background-color: {BACKGROUND_INPUT};
}}

QCheckBox::indicator:hover {{
    border-color: {BORDER_STRONG};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT_PRIMARY};
    border-color: {ACCENT_PRIMARY};
}}

QRadioButton {{
    color: {TEXT_PRIMARY};
    spacing: 8px;
}}

QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 8px;
    background-color: {BACKGROUND_INPUT};
}}

QRadioButton::indicator:hover {{
    border-color: {BORDER_STRONG};
}}

QRadioButton::indicator:checked {{
    background-color: {ACCENT_PRIMARY};
    border-color: {ACCENT_PRIMARY};
}}

QPushButton {{
    background-color: {BACKGROUND_BUTTON};
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    color: {TEXT_PRIMARY};
}}

QPushButton:hover {{
    background-color: {BACKGROUND_BUTTON_HOVER};
}}

QPushButton:pressed {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
}}

QPushButton:disabled {{
    background-color: {BACKGROUND_BUTTON_DISABLED};
    color: {TEXT_MUTED};
}}

QPushButton#syncButton {{
    background-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY_ON};
    font-weight: 600;
    font-size: 14px;
    padding: 12px 20px;
    border-radius: 8px;
}}

QPushButton#syncButton:hover {{
    background-color: {ACCENT_PRIMARY_HOVER};
}}

QPushButton#syncButton:disabled {{
    background-color: {ACCENT_PRIMARY_DISABLED};
    color: {ACCENT_PRIMARY_DISABLED_TEXT};
}}

QPushButton#browseButton {{
    min-width: 72px;
}}

QPushButton#panelDangerButton {{
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
    border-radius: 6px;
    padding: 8px 10px;
    color: {ACCENT_ERROR};
}}

QPushButton#panelDangerButton:hover {{
    background-color: #4a1c1c;
    border-color: {ACCENT_ERROR};
}}

QComboBox {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 8px 10px;
    color: {TEXT_PRIMARY};
    min-height: 20px;
}}

QComboBox:hover {{
    border-color: {BORDER_STRONG};
}}

QComboBox:disabled {{
    color: {TEXT_MUTED};
    background-color: {BACKGROUND_BUTTON_DISABLED};
}}

QComboBox:on {{
    border-color: {BORDER_FOCUS};
}}

QComboBox::drop-down {{
    border: none;
    background: transparent;
    width: 28px;
}}

QComboBox::down-arrow {{
    width: 12px;
    height: 12px;
}}

/* Dropdown popup — kill native white frame / focus ring on Windows */
QComboBox QAbstractItemView,
QComboBox QListView {{
    background-color: {BACKGROUND_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_DEFAULT};
    outline: none;
    outline-width: 0;
    outline-style: none;
    padding: 0px;
    margin: 0px;
    selection-background-color: {ACCENT_SELECTION};
    selection-color: {TEXT_PRIMARY};
    alternate-background-color: {BACKGROUND_PANEL};
    show-decoration-selected: 1;
}}

/* Qt private popup frame (Windows often draws a white PE_Frame here) */
QComboBoxPrivateContainer {{
    background-color: {BACKGROUND_PANEL};
    border: 1px solid {BORDER_DEFAULT};
    outline: none;
    padding: 0px;
    margin: 0px;
}}

QComboBoxPrivateContainer > QAbstractScrollArea {{
    background-color: {BACKGROUND_PANEL};
    border: none;
    outline: none;
    padding: 0px;
    margin: 0px;
}}

QComboBox QAbstractItemView::item,
QComboBox QListView::item {{
    background-color: {BACKGROUND_PANEL};
    color: {TEXT_PRIMARY};
    border: none;
    outline: none;
    min-height: 28px;
    padding: 6px 10px;
    margin: 0px;
}}

QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected,
QComboBox QListView::item:hover,
QComboBox QListView::item:selected {{
    background-color: {ACCENT_SELECTION};
    color: {TEXT_PRIMARY};
    border: none;
    outline: none;
}}

QPushButton#detailButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_STRONG};
    padding: 6px 10px;
}}

QPushButton#detailButton:hover {{
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}

QProgressBar {{
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    background-color: {BACKGROUND_INPUT};
    text-align: center;
    color: {TEXT_BODY};
    min-height: 18px;
}}

QProgressBar::chunk {{
    background-color: {ACCENT_PRIMARY};
    border-radius: 5px;
}}

QLabel#titleLabel {{
    font-size: 20px;
    font-weight: 600;
    color: {ACCENT_PRIMARY};
    letter-spacing: 0.5px;
}}

QLabel#pageTitle {{
    font-size: 16px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}

QLabel#fieldCaption {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}

QLabel#warningBanner {{
    color: {ACCENT_WARNING};
    font-size: 12px;
    padding: 4px 0;
}}

QLabel#subtitleLabel {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}

QLabel#pathHintLabel {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

QPushButton#libraryHeaderButton {{
    background-color: {BACKGROUND_BUTTON};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 4px 12px;
    min-width: 64px;
    min-height: 28px;
    color: {TEXT_BODY};
    font-size: 12px;
}}

QPushButton#libraryHeaderButton:hover {{
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}

QPushButton#libraryHeaderButton:disabled {{
    background-color: {BACKGROUND_BUTTON_DISABLED};
    color: {TEXT_MUTED};
    border-color: {BORDER_SUBTLE};
}}

QPushButton#libraryIconButton,
QPushButton#panelIconButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px;
    min-width: 32px;
    max-width: 36px;
    min-height: 28px;
    max-height: 32px;
    color: {TEXT_BODY};
}}

QPushButton#libraryIconButton:hover,
QPushButton#panelIconButton:hover {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}

QPushButton#libraryIconButton:disabled,
QPushButton#panelIconButton:disabled {{
    background-color: {BACKGROUND_BUTTON_DISABLED};
    color: {TEXT_MUTED};
    border-color: {BORDER_SUBTLE};
}}

QPushButton#panelIconDangerButton {{
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
    border-radius: 6px;
    padding: 4px;
    min-width: 32px;
    max-width: 36px;
    min-height: 28px;
    max-height: 32px;
    color: {ACCENT_ERROR};
}}

QPushButton#panelIconDangerButton:hover {{
    background-color: {ACCENT_ERROR_BG};
    border-color: {ACCENT_ERROR};
    color: {ACCENT_ERROR};
}}

QFrame#gameFilterPanel {{
    background-color: {BACKGROUND_SIDEBAR};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}

QWidget#gameFilterRow {{
    background: transparent;
}}

QWidget#GameTreeItem {{
    background: transparent;
}}

QWidget#CategoryTreeItem {{
    background: transparent;
}}

QLabel#gameListChevron {{
    color: {TEXT_MUTED};
    font-size: 11px;
    min-width: 12px;
}}

QLabel#gameTreeIcon {{
    color: {TEXT_BODY};
    font-size: 12px;
}}

QLabel#categoryTreeIcon {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

QLabel#gameTreeName,
QLabel#gameListName {{
    color: {TEXT_BODY};
    font-size: 13px;
    font-weight: 600;
}}

QLabel#categoryTreeName {{
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 400;
}}

QLabel#gameTreeCount,
QLabel#gameListCount {{
    color: {TEXT_MUTED};
    font-size: 12px;
    font-weight: 500;
}}

QLabel#categoryTreeCount {{
    color: {TEXT_MUTED};
    font-size: 10px;
    font-weight: 400;
}}

QLabel#gameListStatus {{
    color: {ACCENT_WARNING};
    font-size: 10px;
    font-weight: 500;
    padding-left: 2px;
}}

QWidget#GameTreeItem[overall="conflict"] QLabel#gameTreeIcon {{
    color: {ACCENT_ERROR};
}}

QWidget#libraryCenter {{
    background: transparent;
}}

QWidget#libraryHost {{
    background-color: {BACKGROUND_PRIMARY};
}}

QScrollArea#libraryScroll {{
    background: transparent;
    border: none;
}}

QLabel#statusLabel {{
    color: {TEXT_STATUS};
}}

QLabel#emptyLabel {{
    color: {TEXT_MUTED};
    font-size: 15px;
}}

QLabel#textSuccess {{
    color: {ACCENT_SUCCESS};
}}

QLabel#textWarning {{
    color: {ACCENT_WARNING};
}}

QLabel#textError {{
    color: {ACCENT_ERROR};
}}

QLabel#cardMetaLabel {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

QLabel#cardStatusSuccess {{
    color: {ACCENT_SUCCESS};
    font-size: 11px;
}}

QLabel#cardStatusWarning {{
    color: {ACCENT_WARNING};
    font-size: 11px;
}}

QFrame#controlPanel {{
    background-color: {BACKGROUND_SIDEBAR};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 12px;
}}

QFrame#modCard {{
    background-color: {BACKGROUND_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
}}

QFrame#modCard:hover {{
    border-color: {BORDER_STRONG};
}}

QFrame#modCard[selected="true"] {{
    background-color: {BACKGROUND_CARD_SELECTED};
    border: 2px solid {ACCENT_PRIMARY};
}}

QScrollBar:vertical {{
    background: {BACKGROUND_PRIMARY};
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {BORDER_DEFAULT};
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: {BORDER_STRONG};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QListWidget {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    color: {TEXT_BODY};
}}

QListWidget::item {{
    padding: 6px 8px;
    border-radius: 4px;
    color: {TEXT_BODY};
}}

QListWidget::item:selected {{
    background-color: {BACKGROUND_BUTTON};
    color: {ACCENT_PRIMARY};
}}

QListWidget::item:hover {{
    background-color: {BORDER_SUBTLE};
}}

QListWidget#gameList {{
    background-color: {BACKGROUND_SIDEBAR};
    border: none;
    border-radius: 6px;
    padding: 2px;
    outline: none;
}}

QListWidget#gameList::item {{
    padding: 2px 0;
    border-radius: 6px;
    color: {TEXT_BODY};
    border-left: 2px solid transparent;
}}

QListWidget#gameList::item:selected {{
    background-color: {BACKGROUND_CARD_SELECTED};
    color: {ACCENT_PRIMARY};
    border-left: 2px solid {ACCENT_PRIMARY};
}}

QListWidget#gameList::item:selected QLabel#gameListName {{
    color: {ACCENT_PRIMARY};
    font-weight: 600;
}}

QListWidget#gameList::item:selected QLabel#gameListCount {{
    color: {TEXT_SECONDARY};
}}

QListWidget#gameList::item:hover {{
    background-color: {BACKGROUND_SECTION};
}}

QListWidget#detailList {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    color: {TEXT_BODY};
}}

QLineEdit#librarySearchBox {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 6px 10px;
    color: {TEXT_BODY};
    selection-background-color: {BACKGROUND_BUTTON};
}}

QLineEdit#librarySearchBox:focus {{
    border-color: {BORDER_FOCUS};
}}

QLineEdit#librarySearchBox::placeholder {{
    color: {TEXT_MUTED};
}}

QPushButton#libraryFilterChip {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 12px;
    padding: 4px 12px;
    color: {TEXT_SECONDARY};
    font-size: 12px;
    min-height: 26px;
}}

QWidget#libraryFilterBar {{
    background: transparent;
}}

QPushButton#libraryFilterChip:hover {{
    border-color: {BORDER_STRONG};
    color: {TEXT_BODY};
}}

QPushButton#libraryFilterChip:checked {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}

QComboBox#librarySortCombo {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
    padding: 3px 8px;
    min-width: 96px;
    max-height: 28px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
}}

QFrame#libraryEmptyOverlay {{
    background-color: {BACKGROUND_OVERLAY};
    border: none;
}}

QLabel#libraryEmptyTitle {{
    color: {TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
}}

QPushButton#libraryEmptyAction {{
    background-color: {BACKGROUND_BUTTON};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 8px 16px;
    color: {ACCENT_PRIMARY};
    font-weight: 600;
}}

QPushButton#libraryEmptyAction:hover {{
    border-color: {ACCENT_PRIMARY};
}}

QFrame#libraryLoadingOverlay {{
    background-color: transparent;
    border: none;
}}

QLabel#libraryLoadingLabel {{
    color: {ACCENT_PRIMARY};
    font-size: 13px;
    font-weight: 600;
    background-color: transparent;
    border: none;
}}

QListWidget#navList {{
    background-color: {BACKGROUND_SIDEBAR};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
    padding: 4px;
    outline: none;
}}

QListWidget#navList::item {{
    padding: 10px 8px;
    border-radius: 8px;
    margin: 2px 0;
    color: {TEXT_BODY};
    font-weight: 600;
}}

QListWidget#navList::item:selected {{
    background-color: {BACKGROUND_BUTTON};
    color: {ACCENT_PRIMARY};
}}

QListWidget#navList::item:hover {{
    background-color: {BORDER_SUBTLE};
}}

QFrame#gamePreviewCard {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 10px;
}}

QLabel#gamePreviewTitle {{
    font-size: 16px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}

QLabel#gamePreviewMeta {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}

QLabel#gamePreviewDesc {{
    color: {TEXT_STATUS};
    font-size: 12px;
}}
""".strip()


def _build_panel_style() -> str:
    """Detail panel stylesheet — shares the same Design Tokens as APP_STYLE."""
    return f"""
QWidget#modDetailPanel {{
    background-color: {BACKGROUND_PANEL};
    border-left: 1px solid {BORDER_DEFAULT};
}}
QFrame#detailPanelInner {{
    background-color: {BACKGROUND_PANEL};
}}
QFrame#detailSection {{
    background-color: {BACKGROUND_SECTION};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}
QFrame#detailFooter {{
    background-color: {BACKGROUND_PRIMARY};
    border-top: 1px solid {BORDER_DEFAULT};
}}
QFrame#detailStatusBanner {{
    border-radius: 8px;
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
}}
QFrame#detailStatusBanner[tone="error"] {{
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
}}
QLabel#detailStatusBannerBody {{
    font-size: 13px;
    line-height: 1.4;
    color: {ACCENT_ERROR};
}}
QFrame#detailStatusBanner[tone="error"] QLabel#detailStatusBannerBody {{
    color: {ACCENT_ERROR};
}}
QFrame#detailActionArea {{
    background-color: {BACKGROUND_SECTION};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}
QLabel#detailEmptyHint {{
    color: {TEXT_MUTED};
    font-size: 14px;
    line-height: 1.5;
}}
QLabel#detailPanelTitle {{
    color: {TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
}}
QPushButton#detailRefreshButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px 10px;
    min-height: 26px;
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
QPushButton#detailRefreshButton:hover {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {ACCENT_PRIMARY};
    color: {TEXT_PRIMARY};
}}
QPushButton#detailRefreshButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER_SUBTLE};
}}
QLabel#detailMetaLine {{
    color: {TEXT_BODY};
    font-size: 13px;
    line-height: 1.55;
    padding: 1px 0;
}}
QPushButton#detailFlagChip {{
    background-color: {BACKGROUND_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 12px;
    padding: 4px 14px;
    color: {TEXT_SECONDARY};
    font-size: 12px;
    min-height: 26px;
}}
QPushButton#detailFlagChip:hover {{
    border-color: {BORDER_STRONG};
    color: {TEXT_BODY};
}}
QPushButton#detailFlagChip:checked {{
    background-color: {ACCENT_ERROR_BG};
    border-color: {ACCENT_ERROR_BORDER};
    color: {ACCENT_ERROR};
    font-weight: 600;
}}
QWidget#detailHeaderActions {{
    min-width: 140px;
}}
QLabel#detailPanelSection {{
    color: {ACCENT_PRIMARY};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QLabel#detailPanelField {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    margin-top: 4px;
}}
QLabel#detailPanelMeta {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
QLabel#detailPanelBody {{
    color: {TEXT_BODY};
    font-size: 13px;
}}
QPushButton#panelActionButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    font-size: 11px;
    padding: 4px 6px;
    min-height: 26px;
}}
QPushButton#panelActionButton:hover {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {ACCENT_PRIMARY};
}}
QPushButton#panelPrimaryButton {{
    background-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY_ON};
    font-weight: 600;
    font-size: 11px;
    border: none;
    border-radius: 6px;
    padding: 4px 6px;
    min-height: 26px;
}}
QPushButton#panelPrimaryButton:hover {{
    background-color: {ACCENT_PRIMARY_HOVER};
}}
QPushButton#dependencyPillButton {{
    background-color: rgba(77, 166, 255, 0.15);
    color: #4da6ff;
    border: 1px solid #4da6ff;
    border-radius: 12px;
    padding: 4px 12px;
    font-weight: bold;
    min-height: 22px;
}}
QPushButton#dependencyPillButton:hover {{
    background-color: rgba(77, 166, 255, 0.3);
}}
QPushButton#dependencyPillButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER_SUBTLE};
    background-color: transparent;
}}
QPushButton#panelDangerButton {{
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
    border-radius: 6px;
    padding: 8px 10px;
    color: {ACCENT_ERROR};
    min-height: 28px;
}}
QPushButton#panelDangerButton:hover {{
    background-color: {ACCENT_ERROR_BG};
    border-color: {ACCENT_ERROR};
}}
QPushButton#panelIconButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px;
    min-width: 32px;
    max-width: 36px;
    min-height: 28px;
    max-height: 32px;
    color: {TEXT_BODY};
}}
QPushButton#panelIconButton:hover {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}
QPushButton#panelIconButton:disabled {{
    background-color: {BACKGROUND_BUTTON_DISABLED};
    color: {TEXT_MUTED};
    border-color: {BORDER_SUBTLE};
}}
QPushButton#panelIconDangerButton {{
    background-color: {ACCENT_ERROR_BG};
    border: 1px solid {ACCENT_ERROR_BORDER};
    border-radius: 6px;
    padding: 4px;
    min-width: 32px;
    max-width: 36px;
    min-height: 28px;
    max-height: 32px;
    color: {ACCENT_ERROR};
}}
QPushButton#panelIconDangerButton:hover {{
    background-color: {ACCENT_ERROR_BG};
    border-color: {ACCENT_ERROR};
}}
QToolButton#collapsibleSection {{
    color: {ACCENT_PRIMARY};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
    border: none;
    background: transparent;
    padding: 2px 0;
    text-align: left;
}}
QLabel#detailPlatformBadge {{
    font-size: 11px;
    font-weight: 600;
}}
QLabel#detailFilesSummary {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
QLabel#detailFilesStatusReady {{
    color: {ACCENT_SUCCESS};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#detailFilesStatusEmpty {{
    color: {ACCENT_WARNING};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#detailFilesGroup {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    font-weight: 600;
    margin-top: 4px;
}}
QLabel#detailFilesPrimary {{
    color: {TEXT_BODY};
    font-size: 13px;
}}
QLabel#detailFilesSecondary {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#detailFilesLegacyHint {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#detailFileBadgeMain {{
    background-color: #3fb950;
    color: #ffffff;
    font-size: 9px;
    font-weight: 700;
    padding: 0px 3px;
    border-radius: 2px;
}}
QLabel#detailFileBadgeSource {{
    background-color: #b8860b;
    color: #ffffff;
    font-size: 9px;
    font-weight: 700;
    padding: 0px 3px;
    border-radius: 2px;
}}
/* Nexus category badges — colors via Qt property; NO default green inherit */
QLabel#detailFileCategoryBadge {{
    color: #ffffff;
    font-size: 8px;
    font-weight: 700;
    padding: 0px;
    margin: 0px;
    border-radius: 2px;
    background-color: #4a90d9;
}}
QLabel#detailFileCategoryBadge[category="Main"] {{
    background-color: #3fb950;
}}
QLabel#detailFileCategoryBadge[category="Optional"] {{
    background-color: #d4a017;
}}
QLabel#detailFileCategoryBadge[category="Miscellaneous"] {{
    background-color: #6e6e6e;
}}
QLabel#detailFileCategoryBadge[category="汉化"] {{
    background-color: #d97706;
}}
QLabel#detailFileCategoryBadge[category="Other"] {{
    background-color: #4a90d9;
}}
QLabel#detailFileDesc {{
    color: #888888;
    font-size: 11px;
}}
QPushButton#detailFilesEditButton {{
    background: transparent;
    border: none;
    color: {TEXT_SECONDARY};
    font-size: 14px;
    padding: 0;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
}}
QPushButton#detailFilesEditButton:hover {{
    color: {ACCENT_PRIMARY};
    background: transparent;
    border: none;
}}
QPushButton#detailFilesEditButton:pressed {{
    color: {ACCENT_PRIMARY_HOVER};
}}
QPushButton#detailFilesActionButton {{
    background-color: {BACKGROUND_BUTTON_PRESSED};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    color: {TEXT_BODY};
    min-height: 22px;
}}
QPushButton#detailFilesActionButton:hover {{
    background-color: {BACKGROUND_BUTTON};
    border-color: {BORDER_STRONG};
}}
QLabel#detailFileBadgeSpacer {{
    background: transparent;
    border: none;
}}
""".strip()


APP_STYLE = _build_app_style()
PANEL_STYLE = _build_panel_style()


def apply_dark_palette(app) -> None:
    """
    Dark QPalette so native popup remnants (combo list frames) stay dark
    even when a PE_Frame draws outside QSS coverage.
    """
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette(app.palette())
    window = QColor(BACKGROUND_PRIMARY)
    base = QColor(BACKGROUND_PANEL)
    alt = QColor(BACKGROUND_SECTION)
    text = QColor(TEXT_PRIMARY)
    muted = QColor(TEXT_MUTED)
    highlight = QColor(ACCENT_SELECTION)
    button = QColor(BACKGROUND_BUTTON)

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, button)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, text)
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BACKGROUND_SECTION))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, text)
    palette.setColor(QPalette.ColorRole.Light, base)
    palette.setColor(QPalette.ColorRole.Midlight, alt)
    palette.setColor(QPalette.ColorRole.Dark, QColor(BORDER_DEFAULT))
    palette.setColor(QPalette.ColorRole.Mid, QColor(BORDER_STRONG))
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#000000"))
    app.setPalette(palette)
