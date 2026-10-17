"""Production WebEngine composition with optional native read-only startup."""
import sys
import logging
import os
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from core.paths import project_root, DATA_DIR_NAME, DATABASE_FILENAME
from .library_adapter import LibraryAdapter
from .web_window import WebLibraryWindow


def _execute(db, *, resources=None, writable=False):
    """One real application loop; dispose WebEngine while QApplication is alive."""
    from shiboken6 import delete, isValid
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName('SteamModManager')
    app.setApplicationName('WorkshopLibrary')
    from ui.window_chrome import apply_application_icon
    apply_application_icon(app)
    window = None
    try:
        window = WebLibraryWindow(LibraryAdapter(db, writable=writable), resources=resources)
        window.setWindowTitle('SMM' if writable else 'SMM · 只读')
        window.show()
        return app.exec()
    finally:
        if window is not None and isValid(window):
            while not window.bridge.shutdown():
                app.processEvents()
                time.sleep(.02)
            app.processEvents()
            window.hide()
            window.page.setWebChannel(None)
            window.view.stop()
            if isValid(window):delete(window)
            app.processEvents()
        db.close()
        logging.getLogger(__name__).info('WebEngine window disposed before QApplication; database closed')


def run_validation(root):
    """The canonical entry can exercise approved writes without production access."""
    from services.business_resources import checked_sandbox_root, checked_resource_path, BusinessResources
    from services.mutation_context import activate_test_context, reset_mutation_context
    from services.app_instance import acquire_instance_lock, release_instance_lock
    root=checked_sandbox_root(root)
    database=checked_resource_path(root,root/'data/mod_manager.db')
    if not database.is_file():raise FileNotFoundError('发布验证需要已有的隔离 SQLite 和资源目录')
    if not acquire_instance_lock(path=root/'data/app_instance.lock'):return 1
    token=activate_test_context([root]);db=None;started=False
    try:
        db=DatabaseManager.open_existing(database,read_only=False)
        resources=BusinessResources(root,db,steam_api_url=os.environ.get('SMM_VALIDATION_STEAM_API',''))
        started=True
        return _execute(db,resources=resources,writable=True)
    finally:
        # _execute closes only after every business thread has stopped.
        if db is not None and not started:db.close()
        reset_mutation_context(token)
        release_instance_lock()


def run(*, read_only=False):
    from .admission import require_production_admission
    require_production_admission(read_only=read_only)
    if not read_only:
        from PySide6.QtCore import QSettings
        from services.production_resources import ProductionResources
        from services.production_recovery import require_recovery_point
        root=project_root()
        library=Path(QSettings('SteamModManager','WorkshopLibrary').value('paths/target_dir','',str) or root/'mod')
        require_recovery_point(root,library)
        QSettings('SteamModManager','WorkshopLibrary').setValue('paths/application_dir',str(root))
        db=DatabaseManager.open_existing(root/DATA_DIR_NAME/DATABASE_FILENAME,read_only=False)
        try:resources=ProductionResources(root,db,library)
        except Exception:
            db.close();raise
        return _execute(db,resources=resources,writable=True)
    from .readonly_runtime import install_readonly_runtime
    from services.mutation_context import suspend_mutation_context, reset_mutation_context
    database = project_root() / DATA_DIR_NAME / DATABASE_FILENAME
    install_readonly_runtime(database)
    token = suspend_mutation_context()
    try:
        db = DatabaseManager.open_existing(database)
        return _execute(db)
    finally:
        reset_mutation_context(token)
