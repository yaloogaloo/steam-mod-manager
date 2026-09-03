"""Dialog to edit Mod display metadata (never renames managed folders)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_STEAM,
    default_source_url_for_platform,
    get_available_sources,
    normalize_platform,
    platform_requires_source_url,
)
from core.witcher3_game_version import (
    WITCHER3_DEFAULT_VERSION,
    WITCHER3_GAME_VERSION_CHOICES,
    is_valid_witcher3_game_version,
    is_witcher3_game,
)
from services.deploy_status import resolve_game_install_path

_BATCH_PLACEHOLDER = "<批量模式下不可用>"


class EditModDialog(QDialog):
    """
    Edit display name / description / source platform / source URL.

    Persisting is the caller's job. Changing the display name must never
    rename the on-disk Mod folder.

    When ``mod_ids`` has more than one entry, only the source platform combo
    stays editable — name / description / URL are disabled and ignored on save.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        mod_id: str = "",
        mod_ids: Sequence[str] | None = None,
        display_name: str = "",
        steam_name: str = "",
        description: str = "",
        source_url: str = "",
        platform: str = PLATFORM_STEAM,
        game_name: str = "",
        game_id: int = 0,
        game_root: str = "",
        game_install_path: str = "",
        custom_deploy_path: str = "",
        game_version: str = "",
    ) -> None:
        super().__init__(parent)
        self._game_name = str(game_name or "").strip()
        self._game_id = int(game_id or 0)
        install = str(game_install_path or game_root or "").strip()
        self._game_install_path = install

        ids: list[str] = []
        for raw in mod_ids or ():
            text = str(raw or "").strip()
            if text and text not in ids:
                ids.append(text)
        single = str(mod_id or "").strip()
        if single and single not in ids:
            ids.insert(0, single)
        self._mod_ids = ids
        self._batch_mode = len(self._mod_ids) > 1

        # Prefer live mod→game→install_path resolution over a stale caller string.
        resolved = resolve_game_install_path(
            mod_id=(self._mod_ids[0] if self._mod_ids else ""),
            app_id=self._game_id,
        )
        if resolved:
            self._game_install_path = resolved
        elif not self._game_install_path and self._game_id:
            # Caller may have passed install_path already; otherwise leave empty
            # until browse-time re-resolve (allows tests to inject a path).
            pass

        if self._batch_mode:
            self.setWindowTitle(
                f"批量编辑信息 (已选 {len(self._mod_ids)} 个 Mod)"
            )
        else:
            self.setWindowTitle("编辑 Mod 信息")
        self.setMinimumWidth(480)
        self.resize(520, 520)
        self.setObjectName("editModDialog")

        root = QVBoxLayout(self)
        root.setSpacing(12)

        if self._batch_mode:
            hint = QLabel(
                f"已选 {len(self._mod_ids)} 个 Mod。\n"
                "批量模式下仅可统一修改「来源」；名称、介绍与源链接不会被改动。"
            )
        else:
            ws = "—"
            try:
                from core.db_manager import get_db

                mid0 = self._mod_ids[0] if self._mod_ids else ""
                row = get_db().get_mod_display_info(mid0) if mid0 else None
                if row is not None and str(row.workspace_id or "").strip():
                    ws = str(row.workspace_id).strip()
            except Exception:  # noqa: BLE001
                ws = str(self._mod_ids[0] if self._mod_ids else "—")
            hint = QLabel(
                f"Workspace ID：{ws}\n"
                "仅更新显示名称与元数据，不会重命名本地 Mod 目录。"
            )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self.display_name_edit = QLineEdit()
        self.display_name_edit.setText(str(display_name or ""))
        self.display_name_edit.setPlaceholderText(
            str(steam_name or "").strip() or "显示名称"
        )
        form.addRow("Mod 名称", self.display_name_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setAcceptRichText(False)
        self.description_edit.setPlainText(str(description or ""))
        self.description_edit.setPlaceholderText("Mod 介绍 / 功能说明")
        self.description_edit.setMinimumHeight(120)
        form.addRow("Mod 介绍", self.description_edit)

        self.platform_combo = QComboBox()
        self.platform_combo.setObjectName("editSourceCombo")
        current = normalize_platform(platform)
        sources = get_available_sources(self._game_name, self._game_id)
        # If the Mod already uses a platform not in the list (e.g. mod.io on a
        # non-Anno game after data migration), keep it selectable so edits don't
        # silently wipe identity — but never inject mod.io for other games.
        source_ids = {p for p, _ in sources}
        if current not in source_ids and current != PLATFORM_MODIO:
            from ui.platform_labels import format_platform_name

            sources = list(sources) + [(current, format_platform_name(current))]
        for plat_id, label in sources:
            self.platform_combo.addItem(label, plat_id)
        idx = self.platform_combo.findData(current)
        if idx < 0:
            idx = 0
        self.platform_combo.setCurrentIndex(idx)
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        form.addRow("来源", self.platform_combo)

        self.source_url_edit = QLineEdit()
        self.source_url_edit.setText(str(source_url or ""))
        form.addRow("源链接", self.source_url_edit)
        self._on_platform_changed()

        # Witcher 3 ONLY — never show this dimension for other games.
        self._game_version_combo: QComboBox | None = None
        if is_witcher3_game(self._game_name, self._game_id) and not self._batch_mode:
            self._game_version_combo = QComboBox()
            self._game_version_combo.setObjectName("witcher3GameVersionCombo")
            for token, label in WITCHER3_GAME_VERSION_CHOICES:
                self._game_version_combo.addItem(label, token)
            current_gv = str(game_version or "").strip()
            if not is_valid_witcher3_game_version(current_gv):
                current_gv = WITCHER3_DEFAULT_VERSION
            gv_idx = self._game_version_combo.findData(current_gv)
            if gv_idx < 0:
                gv_idx = self._game_version_combo.findData(WITCHER3_DEFAULT_VERSION)
            self._game_version_combo.setCurrentIndex(max(gv_idx, 0))
            form.addRow("版本", self._game_version_combo)

        deploy_row = QHBoxLayout()
        self.custom_deploy_edit = QLineEdit()
        self.custom_deploy_edit.setReadOnly(True)
        self.custom_deploy_edit.setPlaceholderText("留空则使用游戏默认部署规则")
        self.custom_deploy_edit.setText(str(custom_deploy_path or "").strip())
        self.btn_browse_deploy = QPushButton("浏览...")
        self.btn_browse_deploy.setObjectName("panelActionButton")
        self.btn_browse_deploy.clicked.connect(self._browse_custom_deploy)
        deploy_row.addWidget(self.custom_deploy_edit, stretch=1)
        deploy_row.addWidget(self.btn_browse_deploy)
        form.addRow("自定义部署目录 (可选)", deploy_row)

        if self._batch_mode:
            self._apply_batch_field_lock()

        root.addLayout(form, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn is not None:
            save_btn.setText("保存")
            save_btn.setObjectName("panelPrimaryButton")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("取消")
            cancel_btn.setObjectName("panelActionButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def is_batch_mode(self) -> bool:
        return self._batch_mode

    @property
    def mod_ids(self) -> list[str]:
        return list(self._mod_ids)

    def _apply_batch_field_lock(self) -> None:
        """Batch mode: only「来源」stays enabled."""
        for widget in (
            self.display_name_edit,
            self.description_edit,
            self.source_url_edit,
            self.custom_deploy_edit,
            self.btn_browse_deploy,
        ):
            if hasattr(widget, "clear") and widget is not self.btn_browse_deploy:
                widget.clear()
            widget.setEnabled(False)
            if hasattr(widget, "setReadOnly"):
                widget.setReadOnly(True)
        self.display_name_edit.setPlaceholderText(_BATCH_PLACEHOLDER)
        self.description_edit.setPlaceholderText(_BATCH_PLACEHOLDER)
        self.source_url_edit.setPlaceholderText(_BATCH_PLACEHOLDER)
        self.custom_deploy_edit.setPlaceholderText(_BATCH_PLACEHOLDER)
        self.platform_combo.setEnabled(True)

    def selected_platform(self) -> str:
        data = self.platform_combo.currentData()
        return normalize_platform(str(data or PLATFORM_STEAM))

    def _on_platform_changed(self, *_args) -> None:
        if self._batch_mode:
            return
        plat = self.selected_platform()
        if plat == PLATFORM_MODIO:
            self.source_url_edit.setPlaceholderText(
                default_source_url_for_platform(
                    PLATFORM_MODIO,
                    game_name=self._game_name,
                    game_id=self._game_id,
                )
            )
        elif not platform_requires_source_url(plat):
            self.source_url_edit.setPlaceholderText("可选，可留空")
        else:
            self.source_url_edit.setPlaceholderText(
                "https://www.nexusmods.com/... 或 https://github.com/..."
            )

    def browse_start_directory(self) -> str:
        """
        Directory used as QFileDialog initial path.

        Always resolves ``mod → game → game.install_path`` when possible.
        Falls back to the constructor-injected path only when the game is unknown.
        """
        mid = self._mod_ids[0] if self._mod_ids else ""
        resolved = resolve_game_install_path(mod_id=mid, app_id=self._game_id)
        if resolved:
            return resolved
        return str(self._game_install_path or "").strip()

    def _browse_custom_deploy(self) -> None:
        """Open a directory picker starting at the game's install path."""
        start = self.browse_start_directory()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择自定义部署目录",
            start,
        )
        if chosen:
            self.custom_deploy_edit.setText(str(chosen))

    def values(self) -> dict[str, str]:
        plat = self.selected_platform()
        if self._batch_mode:
            # Absolute red line: batch save carries platform only.
            return {"platform": plat}

        url = self.source_url_edit.text().strip()
        if not url and plat == PLATFORM_MODIO:
            url = default_source_url_for_platform(
                PLATFORM_MODIO,
                game_name=self._game_name,
                game_id=self._game_id,
            )
        # 「其它」and other URL-optional platforms keep empty string as-is.
        if not url and not platform_requires_source_url(plat):
            url = ""
        out = {
            "display_name": self.display_name_edit.text().strip(),
            "custom_description": self.description_edit.toPlainText(),
            "platform": plat,
            "source_url": url,
            "custom_deploy_path": self.custom_deploy_edit.text().strip(),
        }
        if self._game_version_combo is not None:
            out["game_version"] = str(self._game_version_combo.currentData() or "")
        return out

