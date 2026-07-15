import sqlite3

conn = sqlite3.connect('indexers/p_insurance_index.db')
cursor = conn.cursor()

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', [t[0] for t in tables])

# Check search_index structure
try:
    cursor.execute("PRAGMA table_info(search_index)")
    cols = cursor.fetchall()
    print('\nsearch_index columns:')
    for col in cols:
        print(f'  {col}')
    
    cursor.execute("SELECT COUNT(*) FROM search_index")
    print(f'\nRow count: {cursor.fetchone()[0]}')
    
    cursor.execute("SELECT * FROM search_index LIMIT 3")
    rows = cursor.fetchall()
    print('\nSample rows:')
    for row in rows:
        print(f'  {str(row)[:200]}')
except Exception as e:
    print(f'search_index error: {e}')

# Check if FTS table exists
try:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'")
    fts = cursor.fetchall()
    print(f'\nFTS tables: {fts}')
except Exception as e:
    print(f'FTS check error: {e}')

conn.close()