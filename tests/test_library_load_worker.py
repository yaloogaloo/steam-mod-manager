"""LibraryLoadWorker must not construct widgets."""

from __future__ import annotations

import inspect

from ui.library_load_thread import LibraryLoadWorker


def test_worker_source_has_no_qwidget() -> None:
    src = inspect.getsource(LibraryLoadWorker)
    assert "QWidget" not in src
    assert "ModCardWidget" not in src
