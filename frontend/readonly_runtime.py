"""Process-scoped readonly capabilities for the opt-in preview.

Python audit events supplement SQLite's native readonly open + authorizer.
They are not an OS sandbox. Qt has no reachable settings/network/file writer
in this preview; native SQLite may create its WAL/SHM coordination files.
"""
import os
from pathlib import Path
import sys


def install_readonly_runtime(database: Path):
    allowed_uri = database.resolve(strict=True).as_uri() + '?mode=ro'
    sys.dont_write_bytecode = True

    def audit(event, args):
        if event == 'sqlite3.connect' and str(args[0]) != allowed_uri:
            raise PermissionError('Readonly preview refuses other database connections')
        if event.startswith(('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                             'os.startfile', 'os.system', 'winreg.SetValue', 'winreg.Delete',
                             'winreg.CreateKey', 'winreg.LoadKey', 'winreg.SaveKey')):
            raise PermissionError('Readonly preview refuses external or persistent operations: ' + event)
        if event in ('os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.truncate',
                     'os.chmod', 'os.utime', 'os.link', 'os.symlink'):
            raise PermissionError('Readonly preview refuses filesystem mutation: ' + event)
        if event == 'open' and isinstance(args[0], (str, bytes)):
            if (args[2] or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise PermissionError('Readonly preview refuses file write')
    sys.addaudithook(audit)
