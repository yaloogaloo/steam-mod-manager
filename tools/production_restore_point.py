"""Create a verified, independent recovery point while holding the application lock."""
import hashlib,json,shutil,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from services.app_instance import acquire_instance_lock,release_instance_lock

def create(destination):
 root=Path(__file__).resolve().parents[1];target=Path(destination).resolve()
 protected=root/'_tmp/protected_recovery'
 if not target.is_relative_to(protected):raise PermissionError('Agent recovery points must use project _tmp/protected_recovery')
 for candidate in (Path(destination).absolute(), *Path(destination).absolute().parents):
  if candidate.exists() and (candidate.is_symlink() or getattr(candidate.lstat(),'st_file_attributes',0)&0x400):raise PermissionError('Recovery path contains reparse point')
 if (target/'restore-point.json').exists():raise FileExistsError(target)
 if not acquire_instance_lock():raise RuntimeError('SMM is running; close it before backup')
 try:
  target.mkdir(parents=True,exist_ok=True);settings=QSettings('SteamModManager','WorkshopLibrary')
  library=Path(settings.value('paths/target_dir','',str) or root/'mod').resolve()
  sources={'config':root/'config'}
  manifest=[]
  for label,source in sources.items():
   for path in source.rglob('*'):
    if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400:raise RuntimeError('Reparse point: '+str(path))
    if not path.is_file() or path.name in ('app_instance.lock','mod_manager.db','mod_manager.db-wal','mod_manager.db-shm'):continue
    dest=target/label/path.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(path,dest)
    def digest(p):
     h=hashlib.sha256()
     with p.open('rb') as f:
      for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
     return h.hexdigest()
    before=digest(path)
    if digest(dest)!=before:raise RuntimeError('Backup hash mismatch: '+str(path))
    manifest.append({'source':str(path),'backup':str(dest.relative_to(target)),'bytes':path.stat().st_size,'sha256':before})
    if len(manifest)%1000==0:print('Verified',len(manifest),flush=True)
  db=target/'data/mod_manager.db';db.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect((root/'data/mod_manager.db').as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(db) as copy:
   source.backup(copy)
   if copy.execute('pragma integrity_check').fetchone()[0]!='ok':raise RuntimeError('SQLite backup integrity failure')
  settings_copy={k:settings.value(k) for k in settings.allKeys()}
  # QSettings serializes QByteArray/QVariant without disclosing credentials in logs.
  from PySide6.QtCore import QSettings as Settings
  exported=Settings(str(target/'settings.ini'),Settings.IniFormat)
  for k,v in settings_copy.items():exported.setValue(k,v)
  exported.sync()
  if exported.status()!=Settings.NoError:raise RuntimeError('Settings export failed')
  result={'scope':'Consistent SQLite, application configuration and settings only; original Mod library and CAS stay in place', 'complete':True,'root':str(root),'library':str(library),'database':str(db),'files':manifest}
  (target/'restore-point.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
  receipt=target/'restore-point.json'
  (root/'config/production_recovery.json').write_text(json.dumps({'receipt':str(receipt),'sha256':hashlib.sha256(receipt.read_bytes()).hexdigest()}),encoding='utf8')
  print('COMPLETE',len(manifest),flush=True)
  return result
 finally:release_instance_lock()
if __name__=='__main__':create(sys.argv[1])
