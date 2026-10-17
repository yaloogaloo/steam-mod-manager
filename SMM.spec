# Windows onedir application; production data stays at the existing configured root.
from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path
import PySide6
qt_runtime=Path(PySide6.__file__).parent
runtimes=[(str(qt_runtime/name),".") for name in ("VCRUNTIME140.dll","VCRUNTIME140_1.dll")]
hidden=[]
for package in ('core','services','frontend','ui'):hidden+=collect_submodules(package)
a=Analysis(['main.py'],pathex=[SPECPATH],binaries=runtimes,datas=[('frontend/web','frontend/web'),('resources/app.ico','resources')],hiddenimports=hidden,excludes=['tkinter','matplotlib','pandas','scipy','pytest','IPython'],noarchive=False)
# Qt uses the Windows ICU API; unrelated PATH tools may expose incompatible ICU builds.
a.binaries=[entry for entry in a.binaries if entry[0].lower() not in ('icuuc.dll','icuin.dll','icudt.dll')]
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='SMM',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,icon='resources/app.ico',contents_directory='.')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='SMM')
