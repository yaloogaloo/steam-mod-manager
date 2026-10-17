"""P2.3 frozen palette translated to native Qt QSS; no resource file access."""
from types import MappingProxyType


COLORS = MappingProxyType({
    "background": "#171e22", "nav-background": "#11272b",
    "surface": "#202b30", "elevated": "#29363b", "border": "#42545b",
    "text-primary": "#dce7e5", "text-secondary": "#b9cbc9", "text-muted": "#a3b7b6",
    "accent": "#70c9b2", "accent-hover": "#8cdbc6", "on-accent": "#132d29",
    "selected": "#294c48", "hover": "#314349", "focus": "#9de7d1",
    "success": "#85d3b8", "success-surface": "#223e36",
    "warning": "#eac17b", "warning-surface": "#423829",
    "error": "#efa5a0", "error-surface": "#452f32",
    "info": "#a8cbee", "info-surface": "#283c50",
    "disabled-text": "#91a19f", "disabled-surface": "#263035",
})


def stylesheet() -> str:
    """Return scoped styling; never mutate QApplication or legacy windows."""
    template = """
    QMainWindow#WorkbenchWindow, QWidget#WorkbenchRoot {background: @background;}
    QWidget {
        color: @text-primary; font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 14px;
    }
    QFrame#Navigation {background: @nav-background; border: none;}
    QListView, QTextBrowser {background: @background; color: @text-primary; border: 1px solid @border; border-radius: 6px;}
    QDialog {background: @surface;}
    QFrame#AssetInspector {background: @surface; border: 1px solid @border; border-radius: 10px;}
    QListView#AssetGrid {border: none; background: @background; outline: none;}
    QTextBrowser#InspectorDescription {background: @surface; border: none; padding: 0; font-size: 14px;}
    QLabel[role='inspectorTitle'] {font-size: 16px; font-weight: 600;}
    QLabel[role='eyebrow'] {font-size: 11px; color: @text-muted; font-weight: 600;}
    QLabel[role='sectionTitle'] {font-size: 14px; font-weight: 600; color: @text-secondary;}
    QTabWidget::pane {border: 1px solid @border; background: @surface;}

    QSplitter::handle {background: @border; width: 1px;}
    QFrame#Topbar, QFrame[role='surface'] {background: @surface; border: 1px solid @border;}
    QLabel {background: transparent;}
    QLabel[role='brand'] {font-size: 23px; font-weight: 700;}
    QLabel[role='heading'] {font-size: 24px; font-weight: 600;}
    QLabel[role='subheading'] {font-size: 18px; font-weight: 600;}
    QLabel[role='muted'] {color: @text-muted; font-size: 12px;}
    QLabel[role='badge'] {color: @info; background: @info-surface; border-radius: 4px; padding: 4px 8px;}
    QLabel[tone='success'] {color: @success; background: @success-surface;}
    QLabel[tone='warning'] {color: @warning; background: @warning-surface;}
    QLabel[tone='error'] {color: @error; background: @error-surface;}
    QPushButton, QToolButton, QComboBox, QLineEdit {
        background: @surface; border: 1px solid @border; border-radius: 6px; padding: 7px 12px;
    }
    QPushButton:hover, QToolButton:hover, QComboBox:hover {background: @hover;}
    QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus {border: 2px solid @focus;}
    QPushButton[role='primary'] {background: @accent; color: @on-accent; border-color: @accent;}
    QPushButton[role='primary']:hover {background: @accent-hover;}
    QPushButton:disabled, QToolButton:disabled, QComboBox:disabled {
        background: @disabled-surface; color: @disabled-text; border-color: @border;
    }
    QPushButton[role='navigation'] {text-align: left; background: transparent; border-color: transparent; padding: 12px;}
    QPushButton[role='navigation']:checked {background: @selected; color: @accent;}
    QTabBar::tab {background: transparent; color: @text-muted; padding: 9px 16px; border-bottom: 2px solid transparent;}
    QTabBar::tab:selected {color: @accent; border-bottom-color: @accent;}
    QTabBar::tab:hover {background: @hover;}
    QTabBar::tab:focus {border: 1px solid @focus;}
    QComboBox QAbstractItemView, QMenu {background: @elevated; color: @text-primary; selection-background-color: @selected; border: 1px solid @border;}
    QMenu::item {padding: 8px 18px;}
    QMenu::item:selected {background: @selected;}
    QMenu::item:disabled {color: @disabled-text;}
    QToolTip {background: @elevated; color: @text-primary; border: 1px solid @border; padding: 6px;}
    QScrollBar:vertical {background: @background; width: 12px;}
    QScrollBar::handle:vertical {background: @border; min-height: 24px; border-radius: 5px;}
    """
    import re
    return re.sub(r"@([a-z-]+)", lambda match: COLORS[match[1]], template)
