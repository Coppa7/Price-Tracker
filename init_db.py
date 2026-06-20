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
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    verify_token TEXT,
    verify_token_expires DATETIME,
    reset_token TEXT,
    reset_token_expires DATETIME,
    oauth_provider TEXT,
    oauth_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')

# users table already existed before verification/reset/OAuth support was
# added; add the missing columns for databases created by an earlier version.
cursor.execute("PRAGMA table_info(users)")
existing_user_columns = [row[1] for row in cursor.fetchall()]
_user_columns_to_add = {
    "email_verified": "INTEGER NOT NULL DEFAULT 0",
    "verify_token": "TEXT",
    "verify_token_expires": "DATETIME",
    "reset_token": "TEXT",
    "reset_token_expires": "DATETIME",
    "oauth_provider": "TEXT",
    "oauth_id": "TEXT",
}
for column_name, column_def in _user_columns_to_add.items():
    if column_name not in existing_user_columns:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_def}")

cursor.execute('''
CREATE TABLE IF NOT EXISTS user_bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    user_id INTEGER,
    asin TEXT,
    created_at DATETIME,
    FOREIGN KEY (asin) REFERENCES products (asin),
    FOREIGN KEY (user_id) REFERENCES users (id)
)
''')

# user_bookmarks already existed before accounts were added; add the column
# for databases created by an earlier version of this script.
cursor.execute("PRAGMA table_info(user_bookmarks)")
existing_columns = [row[1] for row in cursor.fetchall()]
if "user_id" not in existing_columns:
    cursor.execute("ALTER TABLE user_bookmarks ADD COLUMN user_id INTEGER REFERENCES users(id)")

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