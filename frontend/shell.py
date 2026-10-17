"""Native product Shell with explicit presentation inputs and no business binding."""
from collections.abc import Sequence

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLineEdit, QMainWindow,
    QMenu, QPushButton, QSizePolicy, QStackedWidget, QTabBar, QVBoxLayout, QWidget,
)

from .components import PageHeader, StatusBadge, label
from .models import GameContext, WORKSPACES, Workspace
from .theme import stylesheet


class WorkspaceFrame(QWidget):
    """Stable mount point for later real pages; selection lives in this instance."""
    def __init__(self, spec: Workspace, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setObjectName("Workspace_" + spec.key)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 16)
        root.setSpacing(8)
        self.header = PageHeader(spec.title, "展示框架 · 业务未接入", spec.action_label, self)
        root.addWidget(self.header)
        self.tabs = QTabBar(self)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for title in spec.tabs:
            self.tabs.addTab(title)
        root.addWidget(self.tabs)
        self.help_text = label(spec.help_text, "muted", self)
        self.help_text.setWordWrap(True)
        self.help_text.hide()
        self.header.help_button.clicked.connect(self._toggle_help)
        root.addWidget(self.help_text)
        self.content = QFrame(self)
        self.content.setProperty("role", "surface")
        self.content.setObjectName("Content_" + spec.key)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(24, 24, 24, 24)
        self.content_layout.setSpacing(16)
        self.section_title = label(spec.tabs[0], "subheading", self.content)
        self.content_layout.addWidget(self.section_title)
        self.content_layout.addWidget(StatusBadge("业务页面将在此逐页接入", parent=self.content),
                                      alignment=Qt.AlignmentFlag.AlignLeft)
        hint = label("当前仅实现真实 Qt 导航、上下文与展示框架。没有加载用户资产或启动业务任务。",
                     "muted", self.content)
        hint.setWordWrap(True)
        self.content_layout.addWidget(hint)
        if spec.key == "library":
            tools = QHBoxLayout()
            self.search = QLineEdit(self.content)
            self.search.setPlaceholderText("搜索 Mod · 数据尚未接入")
            self.search.setAccessibleName("Mod 搜索展示输入")
            self.search.setClearButtonEnabled(True)
            tools.addWidget(self.search, 1)
            self.density = QComboBox(self.content)
            self.density.addItems(["标准宫格", "紧凑宫格"])
            self.density.setAccessibleName("浏览密度展示偏好")
            self.density.setToolTip("仅保留展示偏好；本轮未实现 Mod 宫格")
            tools.addWidget(self.density)
            self.content_layout.addLayout(tools)
        self.content_layout.addStretch()
        root.addWidget(self.content, 1)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, index: int):
        self.section_title.setText(self.spec.tabs[index])

    def _toggle_help(self):
        self.help_text.setVisible(self.help_text.isHidden())


class WorkbenchWindow(QMainWindow):
    """Opt-in presentation root, never a replacement production entry point.

    Construction starts no worker or persistence. Signals describe user intent;
    this module deliberately does not connect them to services.
    """
    workspace_changed = Signal(str)
    game_changed = Signal(str)

    def __init__(self, games: Sequence[GameContext], parent=None, *, library_adapter=None):
        super().__init__(parent)
        if len({game.key for game in games}) != len(games):
            raise ValueError("Game context keys must be unique")
        self.games = tuple(games)
        self.setObjectName("WorkbenchWindow")
        self.setWindowTitle("SMM · Mod 工作台 · 新前端预览")
        self.setMinimumSize(1200, 720)
        self.resize(1520, 900)
        self.setStyleSheet(stylesheet())
        central = QWidget(self)
        central.setObjectName("WorkbenchRoot")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        navigation = QFrame(central)
        navigation.setObjectName("Navigation")
        navigation.setFixedWidth(216)
        nav = QVBoxLayout(navigation)
        nav.setContentsMargins(14, 24, 14, 16)
        nav.setSpacing(8)
        nav.addWidget(label("SMM", "brand", navigation))
        nav.addWidget(label("MOD WORKSPACE", "muted", navigation))
        nav.addSpacing(24)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}
        for index, spec in enumerate(WORKSPACES):
            button = QPushButton(spec.title, navigation)
            button.setObjectName("Nav_" + spec.key)
            button.setProperty("role", "navigation")
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            self.nav_buttons[spec.key] = button
            nav.addWidget(button)
        nav.addStretch()
        nav.addWidget(label("新版资产库\n其他工作区待迁移" if library_adapter else
                            "新前端预览\n不连接生产服务", "muted", navigation))
        root.addWidget(navigation)
        main = QWidget(central)
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        topbar = QFrame(main)
        topbar.setObjectName("Topbar")
        topbar.setFixedHeight(60)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(24, 8, 24, 8)
        top.setSpacing(16)
        self.game_selector = QComboBox(topbar)
        self.game_selector.setObjectName("GameContext")
        self.game_selector.setAccessibleName("当前游戏上下文")
        self.game_selector.setMinimumWidth(260)
        self.game_selector.setMaximumWidth(420)
        self.game_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for game in self.games:
            self.game_selector.addItem(game.name, game.key)
        self.game_selector.setEnabled(bool(self.games))
        if not self.games:
            self.game_selector.addItem("尚未提供游戏上下文")
        top.addWidget(self.game_selector)
        self.context_info = label("", "muted", topbar)
        top.addWidget(self.context_info)
        top.addStretch()
        top.addWidget(StatusBadge(("真实数据库 · 可整理" if library_adapter.writable else
                                   "真实数据库 · 只读") if library_adapter else
                                  "展示层 · 无业务连接", parent=topbar))
        menu_button = QPushButton("预览说明", topbar)
        menu = QMenu(menu_button)
        menu.addAction("P2.3 设计基线 / 原生 Qt Shell").setEnabled(False)
        menu.addAction("部署 / 导入等操作待迁移" if library_adapter else "业务操作尚未接入").setEnabled(False)
        menu_button.setMenu(menu)
        top.addWidget(menu_button)
        column.addWidget(topbar)
        self.stack = QStackedWidget(main)
        self.stack.setObjectName("WorkspaceStack")
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.workspaces = {}
        for spec in WORKSPACES:
            if spec.key == "library" and library_adapter is not None:
                from .library_page import LibraryPage
                page = LibraryPage(library_adapter, self.stack)
                self.library_view = page
            else:
                page = WorkspaceFrame(spec, self.stack)
            self.workspaces[spec.key] = page
            self.stack.addWidget(page)
        column.addWidget(self.stack, 1)
        root.addWidget(main, 1)
        self.nav_group.idClicked.connect(self._navigate_index)
        self.game_selector.currentIndexChanged.connect(self._game_selected)
        if library_adapter is not None:
            self.game_changed.connect(self.library_view.set_game)
            self.setWindowTitle("SMM · 新版资产库")
        self._game_selected(self.game_selector.currentIndex())
        self.navigate("library")

    def closeEvent(self, event):
        page = getattr(self, "library_view", None)
        if page is not None and not page.shutdown_workers():
            page.feedback.setText("封面读取尚未退出，请稍后再次关闭窗口")
            event.ignore()
            return
        super().closeEvent(event)

    def navigate(self, key: str):
        page = self.workspaces[key]
        self.stack.setCurrentWidget(page)
        self.nav_buttons[key].setChecked(True)
        self.workspace_changed.emit(key)

    def _navigate_index(self, index: int):
        self.navigate(WORKSPACES[index].key)

    def _game_selected(self, index: int):
        if not self.games or index < 0:
            self.context_info.setText("未接入游戏数据")
            return
        game = self.games[index]
        self.context_info.setText("AppID " + game.app_id)
        self.game_selector.setToolTip(game.name)
        # Inputs are game-scoped; avoid carrying a query into another game.
        for page in self.workspaces.values():
            if hasattr(page, "search"):
                page.search.clear()
        self.game_changed.emit(game.key)
