"""Production read-only composition for the complete WebEngine library slice."""
import sys
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from core.paths import project_root, DATA_DIR_NAME, DATABASE_FILENAME
from .library_adapter import LibraryAdapter
from .web_window import WebLibraryWindow


def run(*, read_only=False):
    from .admission import require_production_admission
    require_production_admission(read_only=read_only)
    if not read_only:
        raise PermissionError('Web frontend is read-only')
    from .readonly_runtime import install_readonly_runtime
    from services.mutation_context import suspend_mutation_context, reset_mutation_context
    database = project_root() / DATA_DIR_NAME / DATABASE_FILENAME
    install_readonly_runtime(database)
    token = suspend_mutation_context()
    db = None
    window = None
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        db = DatabaseManager.open_existing(database)
        window = WebLibraryWindow(LibraryAdapter(db))
        window.show()
        return app.exec()
    finally:
        idle = window is None or window.bridge.shutdown()
        if db is not None and idle:
            db.close()
        reset_mutation_context(token)
        if not idle:
            raise RuntimeError('Cover readers have not stopped')
