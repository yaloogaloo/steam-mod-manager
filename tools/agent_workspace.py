"""Paths for Agent diagnostics only; never configures the production application."""
import os
import tempfile
from pathlib import Path


def require_workspace(value):
    from services.business_resources import checked_sandbox_root

    path = Path(value).absolute()
    workspace = Path(__file__).resolve().parents[1] / '_tmp'
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError('Agent output must be inside project _tmp')
    return checked_sandbox_root(path)


def configure_temporary_files(output):
    """Called explicitly by diagnostic processes, not application startup."""
    temporary = require_workspace(Path(output) / 'temp')
    temporary.mkdir(parents=True, exist_ok=True)
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[name] = str(temporary)
    tempfile.tempdir = str(temporary)
    return temporary
