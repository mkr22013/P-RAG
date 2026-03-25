import sqlite3
import os
import json

def verify_system_integrity(db_path="insurance_index.db"):
    print(f"[*] Starting Integrity Audit for: {db_path}...")
    
    if not os.path.exists(db_path):
        print(f"❌ CRITICAL ERROR: Database file '{db_path}' not found.")
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 1. Physical Database Health Check
        cursor.execute("PRAGMA integrity_check;")
        db_health = cursor.fetchone()[0]
        if db_health == "ok":
            print("✅ SQLite File Health: OK")
        else:
            print(f"❌ SQLite File Health: CORRUPT ({db_health})")

        # 2. Schema Verification
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='master_index'")
        if not cursor.fetchone():
            print("❌ ERROR: Table 'master_index' is missing.")
            return

        # 3. Data & Linkage Audit
        cursor.execute("SELECT id, year, plan_type, plan_tier, pdf_path, sub_index_path FROM master_index")
        rows = cursor.fetchall()

        if not rows:
            print("⚠️ WARNING: Database is empty. Please run indexer.py first.")
        else:
            print(f"✅ FOUND: {len(rows)} documents in Master Index.\n")
            print(f"{'ID':<4} | {'Plan Details':<25} | {'PDF Status':<10} | {'Sub-Index Status'}")
            print("-" * 80)

            for row in rows:
                doc_id, year, p_type, p_tier, pdf_path, sub_idx_path = row
                plan_label = f"{year} {p_tier} {p_type}"
                
                # Check PDF Linkage
                pdf_status = "🟢 VALID" if os.path.exists(pdf_path) else "🔴 MISSING"
                
                # Check Sub-Index Linkage & Format
                if not os.path.exists(sub_idx_path):
                    idx_status = "🔴 MISSING"
                else:
                    try:
                        with open(sub_idx_path, 'r') as f:
                            pages = json.load(f)
                        idx_status = f"🟢 VALID ({len(pages)} Pages)"
                    except Exception:
                        idx_status = "🔴 CORRUPT"

                print(f"{doc_id:<4} | {plan_label:<25} | {pdf_status:<10} | {idx_status}")

        conn.close()
    except Exception as e:
        print(f"❌ AUDIT FAILED: {e}")

if __name__ == "__main__":
    verify_system_integrity()
