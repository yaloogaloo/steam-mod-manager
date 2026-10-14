"""Real Library workspace. Unmigrated commands are visible and disabled."""
from pathlib import Path
from urllib.parse import urlparse
from PySide6.QtCore import Qt, QTimer, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
    QInputDialog, QLineEdit, QListView, QMenu, QMessageBox, QPushButton,
    QSplitter, QTextBrowser, QVBoxLayout, QWidget, QFrame, QTabWidget)
from .asset_model import AssetDelegate, AssetModel
from .description_view import SafeDescriptionBrowser
from .asset_grid import AssetGrid
from .inspector_title import InspectorTitle
from .components import PageHeader, label


class LibraryPage(QWidget):
    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter, self.game_id, self.all_assets = adapter, 0, []
        self.current_asset = None
        self._load_generation = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 16)
        root.setSpacing(8)
        self.setObjectName('LibraryPage')
        self.header = PageHeader('资产库', '等待游戏上下文', '添加 Mod', self)
        root.addWidget(self.header)
        self.header.help_button.clicked.connect(self.help)
        tools = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('搜索名称、Workspace ID')
        self.search.setClearButtonEnabled(True)
        tools.addWidget(self.search, 1)
        self.status = QComboBox(self)
        self.status.addItems(['全部状态', '已部署', '未部署', '异常 / 缺失', '收藏'])
        tools.addWidget(self.status)
        self.category = QComboBox(self)
        tools.addWidget(self.category)
        self.collection = QComboBox(self)
        tools.addWidget(self.collection)
        self.density = QComboBox(self)
        self.density.addItems(['标准宫格', '紧凑宫格'])
        for combo in (self.status, self.category, self.collection, self.density):
            combo.setMinimumWidth(112)
        tools.addWidget(self.density)
        self.refresh_button = QPushButton('刷新', self)
        tools.addWidget(self.refresh_button)
        self.inspector_button = QPushButton('收起详情', self)
        tools.addWidget(self.inspector_button)
        root.addLayout(tools)
        self.feedback = label('正在读取资产…', 'muted', self)
        self.feedback.setAccessibleName('资产库结果与反馈')
        bulk = QHBoxLayout()
        bulk.addWidget(self.feedback, 1)
        self.selection_info = label('未勾选', 'muted', self)
        bulk.addWidget(self.selection_info)
        for text, slot in [('全选', self.select_all), ('清除选择', self.clear_selection),
                           ('新建合集', self.create_collection), ('加入合集', self.add_to_collection)]:
            button = QPushButton(text, self)
            button.clicked.connect(slot)
            if text in ('新建合集', '加入合集'):
                button.setEnabled(adapter.writable)
                button.setToolTip('组织操作仅修改合集成员；不会部署 Mod' if adapter.writable else '只读模式')
            bulk.addWidget(button)
        bulk.addStretch()
        root.addLayout(bulk)
        self.splitter = QSplitter(Qt.Horizontal, self)
        root.addWidget(self.splitter, 1)
        self.grid = AssetGrid(self.splitter)
        self.grid.setObjectName('AssetGrid')
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setWrapping(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model = AssetModel(adapter, self)
        self.grid.setModel(self.model)
        self.delegate = AssetDelegate(self.grid)
        self.grid.setItemDelegate(self.delegate)
        self.model.dataChanged.connect(self._update_preview)
        self.inspector = QFrame(self.splitter)
        self.inspector.setObjectName('AssetInspector')
        self.inspector.setMinimumWidth(300)
        self.inspector.setMaximumWidth(380)
        details = QVBoxLayout(self.inspector)
        details.setContentsMargins(12, 12, 12, 12)
        details.setSpacing(6)
        overview = QHBoxLayout()
        self.preview = label('SMM', 'badge', self.inspector)
        self.preview.setFixedSize(80, 56)
        self.preview.setAlignment(Qt.AlignCenter)
        overview.addWidget(self.preview)
        self.name = InspectorTitle('选择一个 Mod', self.inspector)
        self.name.setWordWrap(True)
        overview.addWidget(self.name, 1)
        details.addLayout(overview)
        self.meta = label('', 'muted', self.inspector)
        self.meta.setWordWrap(True)
        details.addWidget(self.meta)
        self.description = SafeDescriptionBrowser(self.inspector)
        self.description.setObjectName("InspectorDescription")
        self.description.setFrameShape(QFrame.NoFrame)
        self.description.setOpenLinks(False)
        self.description.setOpenExternalLinks(False)
        details.addWidget(self.description, 1)
        self.full_button = QPushButton('阅读完整详情', self.inspector)
        self.full_button.setProperty('role', 'primary')
        self.full_button.clicked.connect(self.full_detail)
        actions = QHBoxLayout()
        actions.addWidget(self.full_button, 1)
        self.favorite_button = QPushButton('收藏', self.inspector)
        self.favorite_button.clicked.connect(self.favorite)
        self.favorite_button.hide()
        self.folder_button = QPushButton('打开目录', self.inspector)
        self.source_button = QPushButton('打开来源', self.inspector)
        self.folder_button.clicked.connect(self.open_folder)
        self.source_button.clicked.connect(self.open_source)
        self.folder_button.hide()
        self.source_button.hide()
        self.more_button = QPushButton('更多操作', self.inspector)
        self.more_menu = QMenu(self.more_button)
        for title in ['收藏 / 取消收藏', '打开目录', '打开来源', '编辑元数据', '部署', '重新部署', '取消部署', '打开离线页面']:
            action = self.more_menu.addAction(title + ' · 只读预览不可用')
            action.setEnabled(False)
            action.setToolTip('只读预览禁止写入及外部执行；请退出新版后使用旧版入口')
        self.more_button.setMenu(self.more_menu)
        actions.addWidget(self.more_button)
        details.addLayout(actions)
        self.splitter.setSizes([1000, 360])
        self.splitter.setChildrenCollapsible(False)
        self.search.textChanged.connect(self.apply_filter)
        self.status.currentIndexChanged.connect(self.apply_filter)
        self.category.currentIndexChanged.connect(self.apply_filter)
        self.collection.currentIndexChanged.connect(self.apply_filter)
        self.density.currentIndexChanged.connect(self.set_density)
        self.refresh_button.clicked.connect(self.reload)
        self.inspector_button.clicked.connect(self.toggle_inspector)
        self.grid.selectionModel().currentChanged.connect(self.show_current)
        self.grid.selectionModel().selectionChanged.connect(self.update_selection)
        self.grid.doubleClicked.connect(self.full_detail)
        self.show_current()

    def help(self):
        QMessageBox.information(self, '资产库',
            '单击查看；勾选框或空格勾选，Ctrl / Shift + 单击多选；Ctrl+A 全选。\n'
            '筛选变化会清除不可见选择。收藏和合集仅整理资产，不执行批量部署。\n'
            '描述支持安全 HTML / BBCode 阅读；图片及外部资源不会自动加载。')

    def set_game(self, key):
        self.game_id = int(key)
        for control in (self.search, self.status, self.category, self.collection):
            control.blockSignals(True)
        self.search.clear()
        self.status.setCurrentIndex(0)
        self.category.clear()
        self.collection.clear()
        for control in (self.search, self.status, self.category, self.collection):
            control.blockSignals(False)
        self.reload()

    def reload(self):
        self._load_generation += 1
        generation = self._load_generation
        self.feedback.setText('正在读取数据库快照…')
        self.refresh_button.setEnabled(False)
        def load():
            if generation != self._load_generation:
                return
            try:
                self.all_assets = self.adapter.assets(self.game_id)
                previous_category, previous_collection = self.category.currentText(), self.collection.currentData()
                self.category.blockSignals(True)
                self.category.clear()
                self.category.addItems(['全部分类'] + sorted({a.category for a in self.all_assets if a.category}))
                self.category.setCurrentIndex(max(0, self.category.findText(previous_category)))
                self.category.blockSignals(False)
                self.collection.blockSignals(True)
                self.collection.clear()
                self.collection.addItem('全部合集', None)
                for item in self.adapter.collections(self.game_id):
                    self.collection.addItem(item.name, item.collection_id)
                self.collection.setCurrentIndex(max(0, self.collection.findData(previous_collection)))
                self.collection.blockSignals(False)
                self.header.summary.setText(f'{len(self.all_assets)} 个 Mod · {sum(a.deployed for a in self.all_assets)} 已部署')
                self.apply_filter()
            except Exception as exc:
                self.all_assets = []
                self.model.replace([])
                self.show_current()
                self.feedback.setText('加载失败：' + str(exc))
            finally:
                self.refresh_button.setEnabled(True)
        QTimer.singleShot(0, load)

    def apply_filter(self, *_):
        selected = {self.model.rows[i.row()].internal_id for i in self.grid.selectedIndexes()}
        current = self.current_asset.internal_id if self.current_asset else None
        scroll = self.grid.verticalScrollBar().value()
        query = self.search.text().casefold().strip()
        status = self.status.currentIndex()
        category = self.category.currentText()
        try:
            members = self.adapter.members(self.collection.currentData()) if self.collection.currentData() else None
            rows = [a for a in self.all_assets if
                (not query or query in (a.name + ' ' + a.workspace_id).casefold()) and
                (status == 0 or status == 1 and a.deployed or status == 2 and not a.deployed or
                 status == 3 and a.status or status == 4 and a.favorite) and
                (not category or category == '全部分类' or category == a.category) and
                (members is None or str(a.mod_pk) in members)]
            self.model.replace(rows)
            from PySide6.QtCore import QItemSelectionModel
            for r, asset in enumerate(rows):
                index = self.model.index(r)
                if asset.internal_id in selected:
                    self.grid.selectionModel().select(index, QItemSelectionModel.Select)
                if asset.internal_id == current:
                    self.grid.selectionModel().setCurrentIndex(index, QItemSelectionModel.NoUpdate)
            self.grid.verticalScrollBar().setValue(scroll)
            self.show_current()
            self.update_selection()
            self.feedback.setText(f'{len(rows)} 个结果' if rows else '没有匹配的 Mod · 可清除筛选或切换游戏')
        except Exception as exc:
            self.model.replace([])
            self.show_current()
            self.feedback.setText('筛选失败：' + str(exc))

    def set_density(self, index):
        self.grid.set_density(index == 1)

    def update_selection(self, *_):
        self.selection_info.setText(f'{len(self.grid.selectedIndexes())} 项已勾选' if self.grid.selectedIndexes() else '未勾选')
        self.grid.viewport().update()

    def select_all(self):
        self.grid.selectAll()

    def clear_selection(self):
        self.grid.clearSelection()

    def show_current(self, *_):
        index = self.grid.currentIndex()
        self.current_asset = self.model.rows[index.row()] if index.isValid() else None
        asset = self.current_asset
        for button in (self.full_button, self.folder_button, self.source_button, self.more_button):
            button.setEnabled(asset is not None)
        self.favorite_button.setEnabled(asset is not None and asset.status != '身份异常' and self.adapter.writable)
        if asset is None:
            self.name.setText('选择一个 Mod')
            self.meta.clear()
            self.description.clear()
            self.preview.clear()
            self.preview.setText("SMM")
            return
        try:
            info = self.adapter.detail(asset)
            self.name.setText(asset.name)
            self.name.setToolTip(asset.name)
            self.meta.setText(f'{asset.platform} · {"已部署" if asset.deployed else "未部署"} · {asset.status}\n'
                              f'{asset.category or "未分类"} · Workspace ID {asset.workspace_id}')
            raw = getattr(info, 'description', '') or info.custom_description or info.steam_description
            self.description.set_description(raw or '暂无描述。现有本地来源尚未提供内容。')
            plain = self.description.toPlainText()
            if len(plain) > 750:
                self.description.setPlainText(plain[:750].rstrip() + '…\n\n完整内容请打开“阅读完整详情”。')
            self._update_preview()
            self.favorite_button.setText('取消收藏' if asset.favorite else '收藏')
            self.folder_button.setEnabled(self.adapter.writable and bool(asset.managed_path) and Path(asset.managed_path).is_dir())
            self.source_button.setEnabled(self.adapter.writable and urlparse(info.source_url).scheme in ('http', 'https'))
        except Exception as exc:
            self.description.setPlainText('详情读取失败：' + str(exc))

    def _update_preview(self, *_):
        if self.current_asset is None:
            return
        image = self.model.image(self.current_asset)
        if image is not None and not image.isNull():
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            state = self.model.cover_states.get(self.current_asset.internal_id, 'loading')
            self.preview.setText({'invalid':'图片无效', 'error':'读取失败', 'loading':'读取中'}.get(state, 'SMM\n默认预览'))

    def toggle_inspector(self):
        visible = self.inspector.isHidden()
        self.inspector.setVisible(visible)
        self.inspector_button.setText('收起详情' if visible else '展开详情')

    def full_detail(self, *_):
        if not self.current_asset:
            return
        try:
            asset = self.current_asset
            info = self.adapter.detail(asset)
            dialog = QDialog(self)
            dialog.setWindowTitle(asset.name)
            available = self.screen().availableGeometry()
            dialog.resize(min(1200, available.width()-48), min(850, available.height()-64))
            layout = QVBoxLayout(dialog)
            heading = label(asset.name, 'heading', dialog)
            heading.setWordWrap(True)
            layout.addWidget(heading)
            layout.addWidget(label(f'{asset.platform} · Workspace ID {asset.workspace_id}', 'muted', dialog))
            text = SafeDescriptionBrowser(dialog)
            text.setFrameShape(QFrame.NoFrame)
            text.document().setDocumentMargin(24)
            text.set_description((getattr(info, 'description', '') or info.custom_description or info.steam_description or '暂无描述'))
            tabs = QTabWidget(dialog)
            tabs.addTab(text, "描述")
            layout.addWidget(tabs, 1)
            resources = QTextBrowser(dialog)
            resources.setOpenLinks(False)
            resources.setOpenExternalLinks(False)
            resources.setPlainText(
                '\n\n—— 用户备注 ——\n' + info.user_notes + '\n\n—— 文件与资源 ——\n' +
                asset.managed_path + '\n' + info.mod_files_json + '\n\n来源：' + info.source_url +
                '\n\nInternal UUID: ' + asset.internal_id + '\nSQLite PK: ' + str(asset.mod_pk))
            tabs.addTab(resources, "文件与来源")
            close = QPushButton('返回资产库', dialog)
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec()
            dialog.deleteLater()
        except Exception as exc:
            self.error(exc)

    def error(self, exc):
        self.feedback.setText('操作失败：' + str(exc))
        QMessageBox.warning(self, '操作未完成', str(exc))

    def favorite(self):
        if not self.adapter.writable:
            return
        if self.current_asset:
            try:
                self.adapter.favorite(self.current_asset)
                self.reload()
            except Exception as exc:
                self.error(exc)

    def create_collection(self):
        if not self.adapter.writable:
            return
        name, ok = QInputDialog.getText(self, '新建合集', '合集名称（不驱动部署）')
        if ok and name.strip():
            try:
                self.adapter.create_collection(self.game_id, name.strip())
                self.reload()
            except Exception as exc:
                self.error(exc)

    def add_to_collection(self):
        if not self.adapter.writable:
            return
        assets = [self.model.rows[i.row()] for i in self.grid.selectedIndexes()]
        if not assets:
            self.feedback.setText('请先选择需要整理的 Mod')
            return
        try:
            collections = self.adapter.collections(self.game_id)
            if not collections:
                self.feedback.setText('请先新建合集')
                return
            names = [c.name for c in collections]
            name, ok = QInputDialog.getItem(self, '加入合集', '选择目标合集', names, 0, False)
            if ok:
                count = self.adapter.add_members(collections[names.index(name)].collection_id, assets)
                self.feedback.setText(f'合集已更新 · 新增 {count} 项成员')
        except Exception as exc:
            self.error(exc)

    def open_folder(self):
        if not self.adapter.writable:
            return
        try:
            asset = self.current_asset
            if not asset:
                return
            path = self.adapter.folder(asset)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
                raise RuntimeError('系统未能打开目录')
        except Exception as exc:
            self.error(exc)

    def open_source(self):
        if not self.adapter.writable:
            return
        try:
            if not self.current_asset:
                return
            url = self.adapter.verify(self.current_asset).source_url
            if urlparse(url).scheme not in ('http', 'https'):
                raise ValueError('来源链接必须是 HTTP 或 HTTPS')
            if not QDesktopServices.openUrl(QUrl(url)):
                raise RuntimeError('系统未能打开来源链接')
        except Exception as exc:
            self.error(exc)

    def shutdown_workers(self):
        self._load_generation += 1
        return self.model.shutdown()

    def library_load_is_running(self):
        return self.model.pool.activeThreadCount() > 0
