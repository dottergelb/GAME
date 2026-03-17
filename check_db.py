import sqlite3

c = sqlite3.connect("bot/db.sqlite3")
cur = c.cursor()

tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
print("Tables:", tables)