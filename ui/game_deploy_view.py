"""Game deploy settings — configure install / mod paths per AppID."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import (
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    SUPPORTED_DEPLOY_TYPES,
    DatabaseManager,
    get_db,
)


def validate_deploy_paths(install_path: str, mod_path: str) -> list[str]:
    """
    Check install_path / mod_path exist and are directories.

    Does not create directories. Returns a list of human-readable errors
    (empty list means OK).
    """
    errors: list[str] = []
    install = install_path.strip()
    mods = mod_path.strip()

    if not install:
        errors.append("游戏安装目录未填写。")
    else:
        p = Path(install)
        if not p.exists():
            errors.append(f"游戏目录不存在：\n{install}")
        elif not p.is_dir():
            errors.append(f"游戏目录不是文件夹：\n{install}")

    if not mods:
        errors.append("Mod 部署目录未填写。")
    else:
        p = Path(mods)
        if not p.exists():
            errors.append(f"Mod 部署目录不存在：\n{mods}")
        elif not p.is_dir():
            errors.append(f"Mod 部署目录不是文件夹：\n{mods}")

    return errors


class GameDeployView(QWidget):
    """View C: per-game deploy path configuration (no deploy execution)."""

    config_saved = Signal(int)  # app_id

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._loading = False
        self._build_ui()

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        panel = QFrame()
        panel.setObjectName("controlPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)

        heading = QLabel("游戏部署设置")
        heading.setObjectName("pageTitle")
        panel_layout.addWidget(heading)

        hint = QLabel(
            "为每个游戏配置安装目录、Mod 部署目录与 Steam 创意工坊目录。"
            "路径保存在数据库中，属于游戏级配置；不会修改 Mod 归档或 .info。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        panel_layout.addWidget(hint)

        # Game picker
        picker_col = QVBoxLayout()
        picker_col.setSpacing(4)
        picker_label = QLabel("选择游戏")
        picker_label.setObjectName("fieldCaption")
        picker_col.addWidget(picker_label)
        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        self.game_combo = QComboBox()
        self.game_combo.setObjectName("deployGameCombo")
        self.game_combo.currentIndexChanged.connect(self._on_game_selected)
        picker_row.addWidget(self.game_combo, stretch=1)
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.setObjectName("browseButton")
        self.refresh_btn.clicked.connect(self.refresh)
        picker_row.addWidget(self.refresh_btn)
        picker_col.addLayout(picker_row)
        panel_layout.addLayout(picker_col)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如 Palworld")
        panel_layout.addLayout(self._labeled_field("游戏名称", self.name_edit))

        self.app_id_edit = QLineEdit()
        self.app_id_edit.setPlaceholderText("例如 1623730")
        panel_layout.addLayout(self._labeled_field("Steam AppID", self.app_id_edit))

        self.install_edit = QLineEdit()
        self.install_edit.setPlaceholderText(
            r"例如 D:\SteamLibrary\steamapps\common\Palworld"
        )
        panel_layout.addLayout(
            self._path_row("游戏安装目录", self.install_edit, self._browse_install)
        )

        self.mod_path_edit = QLineEdit()
        self.mod_path_edit.setPlaceholderText(
            r"例如 D:\SteamLibrary\steamapps\common\Palworld\Pal\Binaries\Win64\Mods"
        )
        panel_layout.addLayout(
            self._path_row("Mod 部署目录", self.mod_path_edit, self._browse_mod_path)
        )

        self.workshop_edit = QLineEdit()
        self.workshop_edit.setPlaceholderText(
            r"例如 F:\SteamLibrary\steamapps\workshop\content\1623730（无工坊可留空）"
        )
        panel_layout.addLayout(
            self._path_row(
                "Steam 创意工坊目录",
                self.workshop_edit,
                self._browse_workshop,
            )
        )

        self.deploy_type_combo = QComboBox()
        self.deploy_type_combo.setObjectName("deployTypeCombo")
        self.deploy_type_combo.addItem("folder_copy（通用复制）", DEPLOY_TYPE_FOLDER_COPY)
        self.deploy_type_combo.addItem(
            "palworld_pak（Palworld PAK）", DEPLOY_TYPE_PALWORLD_PAK
        )
        type_col = QVBoxLayout()
        type_col.setSpacing(4)
        type_caption = QLabel("部署类型")
        type_caption.setObjectName("fieldCaption")
        type_col.addWidget(type_caption)
        type_col.addWidget(self.deploy_type_combo)
        panel_layout.addLayout(type_col)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.save_btn = QPushButton("保存配置")
        self.save_btn.setObjectName("syncButton")
        self.save_btn.clicked.connect(self._on_save)
        action_row.addWidget(self.save_btn, stretch=2)

        self.test_btn = QPushButton("测试路径")
        self.test_btn.clicked.connect(self._on_test_paths)
        action_row.addWidget(self.test_btn, stretch=1)
        panel_layout.addLayout(action_row)

        self.status_label = QLabel("选择游戏或填写 AppID 后保存。")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        panel_layout.addWidget(self.status_label)

        root.addWidget(panel)
        root.addStretch(1)

    def _labeled_field(self, label_text: str, edit: QLineEdit) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("fieldCaption")
        col.addWidget(label)
        col.addWidget(edit)
        return col

    def _path_row(self, label_text: str, edit: QLineEdit, browse_slot) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("fieldCaption")
        col.addWidget(label)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(edit, stretch=1)
        browse = QPushButton("浏览…")
        browse.setObjectName("browseButton")
        browse.clicked.connect(browse_slot)
        row.addWidget(browse)
        col.addLayout(row)
        return col

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload game list from SQLite and keep current AppID if possible."""
        previous = self.app_id_edit.text().strip()
        db = self._database()
        games = [g for g in db.list_games() if g.app_id]

        self._loading = True
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        self.game_combo.addItem("— 选择或新建 —", 0)
        for game in games:
            label = game.name.strip() or f"App_{game.app_id}"
            self.game_combo.addItem(f"{label}  ({game.app_id})", int(game.app_id))

        select_index = 0
        if previous.isdigit():
            want = int(previous)
            for i in range(self.game_combo.count()):
                if int(self.game_combo.itemData(i) or 0) == want:
                    select_index = i
                    break
        self.game_combo.setCurrentIndex(select_index)
        self.game_combo.blockSignals(False)
        self._loading = False

        if select_index > 0:
            self._load_game(int(self.game_combo.currentData()))
        elif not previous:
            self._clear_form()

        self.status_label.setText(f"已加载 {len(games)} 个游戏。")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_game_selected(self, index: int) -> None:
        if self._loading:
            return
        app_id = int(self.game_combo.itemData(index) or 0)
        if app_id <= 0:
            self._clear_form(keep_status=True)
            self.status_label.setText("填写 AppID 与路径后可新建配置。")
            return
        self._load_game(app_id)

    def _set_deploy_type(self, deploy_type: str) -> None:
        key = (deploy_type or DEPLOY_TYPE_FOLDER_COPY).strip()
        if key not in SUPPORTED_DEPLOY_TYPES:
            key = DEPLOY_TYPE_FOLDER_COPY
        idx = self.deploy_type_combo.findData(key)
        if idx < 0:
            idx = 0
        self.deploy_type_combo.setCurrentIndex(idx)

    def _clear_form(self, *, keep_status: bool = False) -> None:
        self.name_edit.clear()
        self.app_id_edit.clear()
        self.install_edit.clear()
        self.mod_path_edit.clear()
        self.workshop_edit.clear()
        self._set_deploy_type(DEPLOY_TYPE_FOLDER_COPY)
        if not keep_status:
            self.status_label.setText("选择游戏或填写 AppID 后保存。")

    def _load_game(self, app_id: int) -> None:
        cfg = self._database().get_game_deploy_config(app_id)
        if cfg is None:
            self.app_id_edit.setText(str(app_id))
            self.name_edit.clear()
            self.install_edit.clear()
            self.mod_path_edit.clear()
            self.workshop_edit.clear()
            self._set_deploy_type(DEPLOY_TYPE_FOLDER_COPY)
            self.status_label.setText(f"AppID {app_id} 尚无部署配置。")
            return
        self.app_id_edit.setText(str(cfg.app_id))
        self.name_edit.setText(cfg.name)
        self.install_edit.setText(cfg.install_path)
        self.mod_path_edit.setText(cfg.mod_path)
        self.workshop_edit.setText(cfg.workshop_path)
        self._set_deploy_type(cfg.deploy_type or DEPLOY_TYPE_FOLDER_COPY)
        self.status_label.setText(f"已加载 AppID {cfg.app_id} 的部署配置。")

    def _browse_install(self) -> None:
        start = self.install_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择游戏安装目录", start)
        if chosen:
            self.install_edit.setText(chosen)

    def _browse_mod_path(self) -> None:
        start = self.mod_path_edit.text().strip() or self.install_edit.text().strip()
        start = start or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择 Mod 部署目录", start)
        if chosen:
            self.mod_path_edit.setText(chosen)

    def _browse_workshop(self) -> None:
        start = self.workshop_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "选择 Steam 创意工坊目录", start
        )
        if chosen:
            self.workshop_edit.setText(chosen)

    def _parse_app_id(self) -> int | None:
        raw = self.app_id_edit.text().strip()
        if not raw.isdigit() or int(raw) <= 0:
            return None
        return int(raw)

    def _on_save(self) -> None:
        app_id = self._parse_app_id()
        if app_id is None:
            QMessageBox.warning(self, "无法保存", "请填写有效的 Steam AppID（正整数）。")
            return
        name = self.name_edit.text().strip()
        install = self.install_edit.text().strip()
        mod_path = self.mod_path_edit.text().strip()
        workshop_path = self.workshop_edit.text().strip()
        deploy_type = (
            str(self.deploy_type_combo.currentData() or DEPLOY_TYPE_FOLDER_COPY).strip()
            or DEPLOY_TYPE_FOLDER_COPY
        )

        try:
            saved = self._database().update_game_deploy_config(
                app_id,
                name=name,
                install_path=install,
                mod_path=mod_path,
                deploy_type=deploy_type,
                workshop_path=workshop_path,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"写入数据库失败：{exc}")
            return

        self.status_label.setText(f"已保存 AppID {saved.app_id} 的部署配置。")
        self.config_saved.emit(saved.app_id)
        # Refresh combo so new games appear; re-select saved id
        self.app_id_edit.setText(str(saved.app_id))
        self.refresh()
        for i in range(self.game_combo.count()):
            if int(self.game_combo.itemData(i) or 0) == saved.app_id:
                self.game_combo.setCurrentIndex(i)
                break

    def _on_test_paths(self) -> None:
        errors = validate_deploy_paths(
            self.install_edit.text(),
            self.mod_path_edit.text(),
        )
        if errors:
            QMessageBox.warning(self, "路径检测失败", "\n\n".join(errors))
            self.status_label.setText("路径检测未通过。")
            return
        QMessageBox.information(
            self,
            "路径检测通过",
            "游戏安装目录与 Mod 部署目录均存在且为文件夹。",
        )
        self.status_label.setText("路径检测通过。")
