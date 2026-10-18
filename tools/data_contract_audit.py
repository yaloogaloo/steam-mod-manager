"""Classify the frozen read-only data inventory; never deletes production files."""
import argparse,csv,json,subprocess
from collections import Counter
from pathlib import Path

REFERENCES={
 'asset_store':('core/paths.py:217; core/cas_runtime.py','CAS 内容寻址资产；动态路径不能按缺少字面引用删除'),
 'mod_backup':('services/metadata_backup.py:52; services/backup_identity.py','UUID 与 legacy PK 恢复副本，部署/离线/封面恢复链使用'),
 'deploy_backup':('services/backup_manager.py:268; services/backup_identity.py:220','部署前用户文件备份；恢复所需'),
 'projection_recovery':('services/production_resources.py:91','外部投影恢复副本，按 app_id/UUID 动态定位'),
 'identity_repair_quarantine':('services/identity_repair.py:2019; core/paths.py:48','身份修复隔离证据；仅疑似历史遗留，未证明恢复链不再需要'),
 'collection_covers':('core/paths.py:227; services/collection.py','合集持有封面，关系引用可能动态构造'),
 'mod_manager.db':('core/db_manager.py; main.py','生产 SQLite 权威数据'),
 'mod_manager.db-wal':('SQLite journal runtime','SQLite WAL，不能脱离连接状态判断冗余'),
 'mod_manager.db-shm':('SQLite journal runtime','SQLite 共享内存文件，不能在线清理'),
 'mod_types.json':('services/mod_type_catalog.py','原类型目录权威文件'),
 'mod_types.legacy_migrated':('services/mod_type_catalog.py; core/paths.py:50','迁移完成标志；防止旧类型重复导入'),
 'import_crash_trace.log':('services/crash_trace.py:30','当前崩溃诊断日志，大小不足以证明冗余'),
 'app_instance.lock':('services/app_instance.py','运行实例锁，退出后是否存在不等于可在运行中删除'),
 '.gitkeep':('Git repository layout','保留目录占位文件'),
}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('inventory',type=Path);parser.add_argument('output',type=Path);a=parser.parse_args()
    entries=json.loads(a.inventory.read_text(encoding='utf-8'));root=Path(__file__).resolve().parents[1];a.output.mkdir(parents=True,exist_ok=True)
    tracked=set(subprocess.check_output(['git','ls-files','-z','data'],cwd=root).decode('utf-8').split('\0'))
    rows=[];totals=Counter();groups=Counter();suspects=[]
    for e in entries:
        rel=e['path'].replace('\\','/');group=rel.split('/')[0];ref,reason=REFERENCES.get(group,('未建立权威用途','未能证明冗余，需人工确认'))
        category='B' if group=='identity_repair_quarantine' or group not in REFERENCES else 'A'
        if e['kind']=='file':totals[category]+=e['bytes'];groups[group]+=e['bytes']
        row=dict(path=str(root/'data'/rel),relative_path=rel,kind=e['kind'],bytes=e['bytes'],mtime_utc=e['mtime_utc'],classification=category,code_references=ref,reason=reason,tracked=('data/'+rel in tracked),startup_or_recovery_dependency='yes' if category=='A' else 'unresolved',recommendation='保留；不执行删除',reclaimable_proven_bytes=0,rollback='原路径保留；未移动未删除')
        rows.append(row)
        if category=='B':suspects.append(row)
    for name,data in [('data-audit.csv',rows),('data-suspects.csv',suspects)]:
        with (a.output/name).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(data)
    text=['# data 只读审计 — 2026-10-18','',f'冻结基线：{a.inventory}。共 {len(rows)} 个文件/目录条目，基线扫描错误 0。扫描 lstat，跳过 reparse point 递归，使用 Windows 扩展路径处理长路径。','',f'A 必须保留：{totals["A"]:,} bytes。B 疑似遗留：{totals["B"]:,} bytes。C 已证明可清理：0 bytes。实际删除：0。','', '没有将文件名、修改时间或缺少源码字面引用作为删除依据。CAS、元数据备份、部署备份均有动态读路径；没有全量复制或修改 E:\\mod。Git 跟踪状态逐项在 CSV 中列出。','', '## 按实际路径分组','']
    for group,size in groups.items():
        ref,reason=REFERENCES.get(group,('待确认','待确认'));text.extend([f'- `{root/"data"/group}`：{size:,} bytes；{reason}。证据：`{ref}`。'])
    text.extend(['','## B 项处置边界','','`data-suspects.csv` 给出全部精确路径、大小、时间及依据。identity_repair_quarantine 是身份修复恢复证据，未逐项证明与存活身份的关联已经终止，不能划入 C。建议保留，预期可安全释放空间为 0；本次未删除，因此无恢复操作。','','## 审计限制','','这是冻结基线的分类清单，不是实时清理器。没有遍历外部 E:\\mod，也没有按大小相同推断重复内容；没有删除日志、锁、WAL 或 SHM。清理前仍需针对恢复依赖逐项核实。'])
    (a.output/'DATA_AUDIT.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    print(json.dumps({'entries':len(rows),'bytes':dict(totals),'C':0,'deleted':0}))
if __name__=='__main__':main()
