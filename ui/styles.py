"""Application-wide Qt stylesheets."""

APP_STYLE = """
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
    color: #e8eef5;
}

QMainWindow, QDialog {
    background-color: #121820;
}

QScrollArea {
    border: none;
    background: transparent;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

QLineEdit {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 6px;
    padding: 8px 10px;
    selection-background-color: #3d7ea6;
}

QLineEdit:focus {
    border-color: #66c0f4;
}

QPushButton {
    background-color: #2a475e;
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    color: #e8eef5;
}

QPushButton:hover {
    background-color: #3d6a8a;
}

QPushButton:pressed {
    background-color: #1b2838;
}

QPushButton:disabled {
    background-color: #243040;
    color: #6b7c8f;
}

QPushButton#syncButton {
    background-color: #66c0f4;
    color: #0b1520;
    font-weight: 600;
    font-size: 14px;
    padding: 12px 20px;
    border-radius: 8px;
}

QPushButton#syncButton:hover {
    background-color: #8ed0f8;
}

QPushButton#syncButton:disabled {
    background-color: #3a5568;
    color: #9bb0c0;
}

QPushButton#browseButton {
    min-width: 72px;
}

QComboBox {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 6px;
    padding: 8px 10px;
    color: #e8eef5;
    min-height: 20px;
}

QComboBox:hover {
    border-color: #3d5a73;
}

QComboBox::drop-down {
    border: none;
    width: 28px;
}

QComboBox QAbstractItemView {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    selection-background-color: #2a475e;
    selection-color: #66c0f4;
    outline: none;
}

QPushButton#detailButton {
    background-color: #1b2838;
    border: 1px solid #3d5a73;
    padding: 6px 10px;
}

QPushButton#detailButton:hover {
    border-color: #66c0f4;
    color: #66c0f4;
}

QProgressBar {
    border: 1px solid #2c3a4d;
    border-radius: 6px;
    background-color: #1a2330;
    text-align: center;
    color: #c7d5e0;
    min-height: 18px;
}

QProgressBar::chunk {
    background-color: #66c0f4;
    border-radius: 5px;
}

QLabel#titleLabel {
    font-size: 20px;
    font-weight: 600;
    color: #66c0f4;
    letter-spacing: 0.5px;
}

QLabel#subtitleLabel {
    color: #8b9bb0;
    font-size: 12px;
}

QLabel#statusLabel {
    color: #a8b8c8;
}

QLabel#emptyLabel {
    color: #6b7c8f;
    font-size: 15px;
}

QFrame#controlPanel {
    background-color: #171e28;
    border: 1px solid #243044;
    border-radius: 12px;
}

QFrame#modCard {
    background-color: #171e28;
    border: 1px solid #243044;
    border-radius: 10px;
}

QFrame#modCard:hover {
    border-color: #3d5a73;
}

QFrame#modCard[selected="true"] {
    background-color: #1e2a3a;
    border: 2px solid #66c0f4;
}

QScrollBar:vertical {
    background: #121820;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #2c3a4d;
    border-radius: 5px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: #3d5a73;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QListWidget#gameList {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 8px;
    padding: 4px;
    outline: none;
}

QListWidget#gameList::item {
    padding: 8px 10px;
    border-radius: 6px;
    color: #c7d5e0;
}

QListWidget#gameList::item:selected {
    background-color: #2a475e;
    color: #66c0f4;
}

QListWidget#gameList::item:hover {
    background-color: #243044;
}

QLineEdit#librarySearchBox {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 8px;
    padding: 8px 12px;
    color: #c7d5e0;
    selection-background-color: #2a475e;
}

QLineEdit#librarySearchBox:focus {
    border-color: #66c0f4;
}

QLineEdit#librarySearchBox::placeholder {
    color: #6b7c8f;
}

QPushButton#libraryFilterChip {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 14px;
    padding: 6px 12px;
    color: #8b9bb0;
    font-size: 12px;
}

QPushButton#libraryFilterChip:hover {
    border-color: #3d5a73;
    color: #c7d5e0;
}

QPushButton#libraryFilterChip:checked {
    background-color: #2a475e;
    border-color: #66c0f4;
    color: #66c0f4;
}

QComboBox#librarySortCombo {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 110px;
    color: #e8eef5;
}

QFrame#libraryEmptyOverlay {
    background-color: rgba(18, 24, 32, 220);
}

QLabel#libraryEmptyTitle {
    color: #e8eef5;
    font-size: 16px;
    font-weight: 600;
}

QPushButton#libraryEmptyAction {
    background-color: #2a475e;
    border: 1px solid #3d5a73;
    border-radius: 8px;
    padding: 8px 16px;
    color: #66c0f4;
    font-weight: 600;
}

QPushButton#libraryEmptyAction:hover {
    border-color: #66c0f4;
}

QFrame#libraryLoadingOverlay {
    background-color: rgba(18, 24, 32, 180);
}

QLabel#libraryLoadingLabel {
    color: #66c0f4;
    font-size: 14px;
    font-weight: 600;
}

QListWidget#navList {
    background-color: #171e28;
    border: 1px solid #243044;
    border-radius: 10px;
    padding: 6px;
    outline: none;
}

QListWidget#navList::item {
    padding: 12px 14px;
    border-radius: 8px;
    margin: 2px 0;
    color: #c7d5e0;
    font-weight: 600;
}

QListWidget#navList::item:selected {
    background-color: #2a475e;
    color: #66c0f4;
}

QListWidget#navList::item:hover {
    background-color: #243044;
}

QFrame#gamePreviewCard {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 10px;
}

QLabel#gamePreviewTitle {
    font-size: 16px;
    font-weight: 600;
    color: #e8eef5;
}

QLabel#gamePreviewMeta {
    color: #8b9bb0;
    font-size: 12px;
}

QLabel#gamePreviewDesc {
    color: #a8b8c8;
    font-size: 12px;
}
"""
