import sqlite3

c = sqlite3.connect(r"E:\project\steam-mod-manager\data\mod_manager.db")
print("=== TABLES ===")
for n, s in c.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
):
    print(f"--- {n} ---")
    print(s)
print("=== INDEXES ===")
for n, s in c.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
):
    print(f"{n}: {s}")
print("=== PRAGMA mods ===")
for r in c.execute("PRAGMA table_info(mods)"):
    print(tuple(r))
print("=== PRAGMA games ===")
for r in c.execute("PRAGMA table_info(games)"):
    print(tuple(r))
print("=== FK mods ===")
for r in c.execute("PRAGMA foreign_key_list(mods)"):
    print(tuple(r))
print("=== FK mod_relationships ===")
for r in c.execute("PRAGMA foreign_key_list(mod_relationships)"):
    print(tuple(r))
