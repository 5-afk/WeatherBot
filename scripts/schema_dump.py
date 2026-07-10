import sqlite3
c = sqlite3.connect("data/positions.db")
for row in c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
    print(row[0])
    print()
