"""Offline WebEngine view: off-record profile, no network/file navigation/downloads."""
from pathlib import Path
from PySide6.QtCore import QUrl, Qt
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
    def __init__(self, adapter):
        super().__init__()
        self.setWindowTitle('SMM · 资产库 · 只读')
        ratio = QApplication.primaryScreen().devicePixelRatio()
        self.resize(round(1600 / ratio), round(900 / ratio))
        self.profile = QWebEngineProfile(self)
        self.interceptor = OfflineRequests(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.view = QWebEngineView(self)
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
        self.bridge = LibraryBridge(adapter, self.channel)
        self.channel.registerObject('library', self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.featurePermissionRequested.connect(lambda origin, feature:
            self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionDeniedByUser))
        root = Path(__file__).parent / 'web'
        html = (root / 'index.html').read_text('utf-8')
        html = html.replace('/*STYLE*/', (root / 'style.css').read_text('utf-8'))
        html = html.replace('/*SCRIPT*/', (root / 'app.js').read_text('utf-8'))
        self.view.setHtml(html, QUrl('about:blank'))

    def closeEvent(self, event):
        if not self.bridge.shutdown():
            event.ignore()
            return
        super().closeEvent(event)
