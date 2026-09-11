from core.db_manager import get_db

db = get_db()
print("db", db.db_path)
c = db._conn
print("tables", [r[0] for r in c.execute("select name from sqlite_master where type='table' order by 1").fetchall()[:40]])
print("mod_count", c.execute("select count(*) from mods").fetchone()[0])
print("mods_cols", [r[1] for r in c.execute("pragma table_info(mods)").fetchall()])
print("by_app", [tuple(r) for r in c.execute("select app_id, count(*) c from mods group by app_id order by c desc limit 12").fetchall()])
print("games", [tuple(r) for r in c.execute("select app_id, name from games limit 20").fetchall()])
