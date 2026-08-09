import sqlite3
con = sqlite3.connect('smartreco.db')
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('tables:', sorted([r[0] for r in cur.fetchall()]))
try:
    cur.execute('SELECT count(*) FROM reviews')
    print('reviews_count:', cur.fetchone()[0])
except Exception as e:
    print('reviews_count: error', e)
con.close()
