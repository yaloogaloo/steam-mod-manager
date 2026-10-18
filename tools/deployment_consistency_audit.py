"""Read-only deployment evidence export. Never invokes deploy, repair or sync."""
import argparse,csv,json,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.db_manager import DatabaseManager
from services.deployment_inspection import inspect_deployment
from services.deploy_rules.manifest import manifest_path_for


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True,type=Path);parser.add_argument('--out',required=True,type=Path);args=parser.parse_args()
    output=args.out.resolve();repo=Path(__file__).resolve().parents[1]
    if output.is_relative_to(repo):raise PermissionError('证据输出必须独立于生产项目')
    db=DatabaseManager.open_existing(args.db.resolve(),read_only=True)
    try:
        protected={repo}
        for game in db.list_games():
            config=db.get_game_deploy_config(game.app_id)
            protected.update(Path(value).resolve() for value in (config.install_path,config.mod_path,config.workshop_path) if value)
        for row in db._conn.execute('SELECT DISTINCT last_known_path,custom_deploy_path FROM mods'):
            if row[0]:protected.add(Path(row[0]).parent.resolve())
            if row[1]:protected.add(Path(row[1]).resolve())
        if any(output.is_relative_to(path) or path.is_relative_to(output) for path in protected):
            raise PermissionError('输出目录不得与归档、生产项目或游戏目录重叠')
        rows=[]
        for game in db.list_games():
            for asset in db.list_mod_list_items(game_id=int(game.app_id)):
                if asset['game_id']!=int(game.app_id):continue
                if not asset['deployed'] and not manifest_path_for(Path(asset['managed_path'])).is_file():continue
                evidence=inspect_deployment(db,asset['mod_pk'])
                rows.append({'app_id':asset['game_id'],'game':game.name,'mod_pk':asset['mod_pk'],'internal_id':asset['internal_id'],'name':asset['name'],**evidence})
        output.mkdir(parents=True,exist_ok=True)
        (output/'deployment-evidence.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
        keys=['app_id','game','mod_pk','internal_id','name','recorded','backend','state','message']
        with (output/'deployment-evidence.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=keys,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        print(json.dumps({'objects':len(rows),'states':dict(Counter(r['state'] for r in rows)),'production_writes':False}))
    finally:db.close()
if __name__=='__main__':main()
