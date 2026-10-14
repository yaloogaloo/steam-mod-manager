"""Opt-in real application composition; never invoked by the legacy default."""
import sys
from PySide6.QtWidgets import QApplication, QMessageBox
from core.db_manager import DatabaseManager
from core.paths import project_root, DATA_DIR_NAME, DATABASE_FILENAME
from .library_adapter import LibraryAdapter
from .shell import WorkbenchWindow


def run(*, read_only=False):
    from .admission import require_production_admission
    require_production_admission(read_only=read_only)
    if not read_only:
        raise PermissionError('新版可写业务尚未准入')
    from .readonly_runtime import install_readonly_runtime
    database = project_root() / DATA_DIR_NAME / DATABASE_FILENAME
    install_readonly_runtime(database)
    from services.mutation_context import suspend_mutation_context, reset_mutation_context
    mutation_token = suspend_mutation_context()
    app = QApplication(sys.argv)
    app.setOrganizationName('SteamModManagerNext')
    app.setApplicationName('AssetLibrary')
    app.setStyle('Fusion')
    db = None
    window = None
    try:
        # Existing schema only: no get_db(), no migrations, no directory discovery.
        db = DatabaseManager.open_existing(database, read_only=True)
        adapter = LibraryAdapter(db, writable=False)
        window = WorkbenchWindow(adapter.games(), library_adapter=adapter)
        window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(None, '新版启动失败', str(exc) + '\n没有执行数据库迁移。可继续使用旧版入口。')
        return 1
    finally:
        # app.quit() may bypass closeEvent. Do not close persistence before readers.
        readers_idle = window is None or window.library_view.shutdown_workers()
        # Preview owns only its cover readers and explicit DAL connection.
        # Do not import shutdown handlers for business workers never started here.
        if db is not None and readers_idle:
            db.close()
        elif db is not None:
            raise RuntimeError('后台任务未停止，数据库未强制关闭')
        reset_mutation_context(mutation_token)

