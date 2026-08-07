"""Dialog to edit user-owned Mod metadata (SQLite only)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import ModDisplayInfo, get_db


class ModEditDialog(QDialog):
    """Edit display name / custom description / notes / favorite → SQLite."""

    def __init__(
        self,
        mod_id: str,
        *,
        steam_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mod_id = str(mod_id)
        self.steam_name = (steam_name or "").strip()
        self._saved: ModDisplayInfo | None = None

        self.setWindowTitle("编辑 Mod 信息")
        self.setMinimumWidth(480)
        self.resize(520, 420)

        info = get_db().get_mod_display_info(self.mod_id)
        if info is not None and not self.steam_name:
            self.steam_name = info.steam_name

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            f"Mod ID: {self.mod_id}"
            + (f"\nSteam 原名: {self.steam_name}" if self.steam_name else "")
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self.display_name_edit = QLineEdit()
        self.display_name_edit.setPlaceholderText(
            self.steam_name or "留空则使用 Steam 原名"
        )
        if info and info.user_display_name:
            self.display_name_edit.setText(info.user_display_name)
        form.addRow("显示名称", self.display_name_edit)

        self.custom_desc_edit = QTextEdit()
        self.custom_desc_edit.setPlaceholderText("自定义介绍（可选）")
        self.custom_desc_edit.setAcceptRichText(False)
        if info:
            self.custom_desc_edit.setPlainText(info.custom_description)
        form.addRow("自定义介绍", self.custom_desc_edit)

        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText("个人备注…")
        self.notes_edit.setAcceptRichText(False)
        if info:
            self.notes_edit.setPlainText(info.user_notes)
        form.addRow("备注", self.notes_edit)

        self.favorite_check = QCheckBox("收藏")
        if info:
            self.favorite_check.setChecked(info.favorite)
        form.addRow("", self.favorite_check)

        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def saved_info(self) -> ModDisplayInfo | None:
        return self._saved

    def _save(self) -> None:
        try:
            self._saved = get_db().update_mod_user_metadata(
                self.mod_id,
                {
                    "display_name": self.display_name_edit.text(),
                    "custom_description": self.custom_desc_edit.toPlainText(),
                    "user_notes": self.notes_edit.toPlainText(),
                    "favorite": self.favorite_check.isChecked(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.accept()
