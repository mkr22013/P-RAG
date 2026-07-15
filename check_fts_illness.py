import sqlite3
import json

conn = sqlite3.connect('indexers/p_insurance_index.db')
cursor = conn.cursor()

terms = ['asthma', 'diabetes', 'depression', 'migraine', 'hypertension']

for term in terms:
    try:
        cursor.execute(
            "SELECT topic, category, benefit_category, keywords FROM search_index WHERE search_index MATCH ?",
            (term,)
        )
        rows = cursor.fetchall()
        print(f'\n{term}: {len(rows)} matches')
        for r in rows[:3]:
            print(f'  topic={str(r[0])[:60]} cat={r[1]} benefit={r[2]}')
    except Exception as e:
        print(f'{term}: ERROR - {e}')

# Also check what categories exist in search_index
cursor.execute("SELECT DISTINCT category, benefit_category, COUNT(*) FROM search_index GROUP BY category, benefit_category")
rows = cursor.fetchall()
print('\nCategories in search_index:')
for r in rows:
    print(f'  cat={r[0]} benefit={r[1]} count={r[2]}')

conn.close()