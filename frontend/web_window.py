"""Offline WebEngine view: off-record profile, no network/file navigation/downloads."""
from pathlib import Path
from PySide6.QtCore import QUrl, Qt, QTimer, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMainWindow, QApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from .web_bridge import LibraryBridge


class OfflineRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = url.scheme() == 'data' or url.toString() == 'qrc:///qtwebchannel/qwebchannel.js'
        info.block(not allowed)


class OfflinePage(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.scheme() in ('data', 'about') and navigation_type != QWebEnginePage.NavigationType.NavigationTypeLinkClicked

    def createWindow(self, window_type):
        return None


class WebLibraryWindow(QMainWindow):
    def __init__(self, adapter, *, resources=None):
        super().__init__()
        self.setWindowIcon(QApplication.instance().windowIcon())
        self.setWindowTitle('SMM · 资产库 · ' + ('隔离可写验收' if adapter.writable else '只读'))
        ratio = QApplication.primaryScreen().devicePixelRatio()
        self.resize(round(1600 / ratio), round(900 / ratio))
        self._production_session=bool(getattr(resources,'production',False))
        if self._production_session:
            geometry=QSettings('SteamModManager','WorkshopLibrary').value('ui/geometry')
            if geometry is not None:self.restoreGeometry(geometry)
        # Qt destroys sibling children in creation order: Page/View must die before Profile.
        self.view = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)
        self.interceptor = OfflineRequests(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.page = OfflinePage(self.profile, self.view)
        self.page.setBackgroundColor(QColor('#171e22'))
        settings = self.page.settings()
        for attribute in (QWebEngineSettings.LocalContentCanAccessRemoteUrls,
                          QWebEngineSettings.LocalContentCanAccessFileUrls,
                          QWebEngineSettings.JavascriptCanOpenWindows,
                          QWebEngineSettings.PluginsEnabled,
                          QWebEngineSettings.FullScreenSupportEnabled):
            settings.setAttribute(attribute, False)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)
        self.channel = QWebChannel(self.page)
        self.bridge = LibraryBridge(adapter, self.channel, resources=resources)
        self.channel.registerObject('library', self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.featurePermissionRequested.connect(lambda origin, feature:
            self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionDeniedByUser))
        root = Path(__file__).parent / 'web'
        html = (root / 'index.html').read_text('utf-8')
        html = html.replace('/*STYLE*/', (root / 'style.css').read_text('utf-8'))
        html = html.replace('/*SCRIPT*/', '\n'.join((root / name).read_text('utf-8') for name in ('app.js', 'organization.js', 'acquisition.js', 'deployment.js', 'snapshots.js', 'registration.js', 'workshop.js', 'offline_registration.js', 'game_configuration.js', 'mod_operations.js', 'app_settings.js', 'type_management.js', 'asset_management.js', 'advanced_editor.js', 'batch_management.js')))
        self.view.setHtml(html, QUrl('about:blank'))

    def closeEvent(self, event):
        if not self.bridge.shutdown():
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        if self._production_session:
            QSettings('SteamModManager','WorkshopLibrary').setValue('ui/geometry',self.saveGeometry())
        super().closeEvent(event)
