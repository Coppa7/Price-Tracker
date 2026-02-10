import sqlite3, os

folder = 'database_dir'
path = os.path.join(folder, 'bookmarks.db')

if not os.path.exists(folder):
    os.makedirs(folder)
    
connection = sqlite3.connect(path)
cursor = connection.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS products (
    asin TEXT PRIMARY KEY,
    name TEXT,
    price FLOAT,
    discount TEXT,
    img_src TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS user_bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    asin TEXT,
    created_at DATETIME,
    FOREIGN KEY (asin) REFERENCES products (asin)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS graph_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT,
    price FLOAT,
    date DATETIME,
    FOREIGN KEY (asin) REFERENCES products (asin)
)
''')

connection.commit()
connection.close()