"""
scripts/migrate_sqlite_to_pg.py
────────────────────────────────────────────────────────────────────────────
One-time migration script: SQLite p_insurance_index.db → Azure PostgreSQL.

Run once when setting up the production PostgreSQL instance.
Safe to re-run — uses UPSERT so existing rows are updated not duplicated.

Usage:
    POSTGRES_DSN=postgresql://user:pass@host:5432/dbname python scripts/migrate_sqlite_to_pg.py

Steps:
    1. Creates the master_index table in PostgreSQL if it doesn't exist
    2. Reads all rows from local SQLite
    3. Derives blob_path and redis_key from existing sub_index_path
    4. Upserts all rows into PostgreSQL
"""

import os
import sys
import sqlite3
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")
SQLITE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "indexers", "p_insurance_index.db"
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS master_index (
    id              SERIAL PRIMARY KEY,
    year            VARCHAR(4)    NOT NULL,
    plan_category   VARCHAR(50)   NOT NULL,
    plan            VARCHAR(255)  NOT NULL,
    group_number    VARCHAR(50)   NOT NULL,
    group_name      VARCHAR(255),
    plan_type       VARCHAR(50),
    plan_tier       VARCHAR(50),
    product_line    VARCHAR(255),
    variant         VARCHAR(100),
    network         VARCHAR(100),
    blob_path       VARCHAR(500)  NOT NULL,
    redis_key       VARCHAR(500)  NOT NULL,
    blob_etag       VARCHAR(100),
    last_indexed    TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_lookup
ON master_index(year, plan_category, group_number,
                COALESCE(plan_type, ''), COALESCE(plan_tier, ''),
                COALESCE(product_line, ''), COALESCE(variant, ''),
                COALESCE(network, ''));

CREATE INDEX IF NOT EXISTS idx_group_category
ON master_index(group_number, plan_category);
"""

UPSERT_SQL = """
INSERT INTO master_index (
    year, plan_category, plan, group_number, group_name,
    plan_type, plan_tier, product_line, variant, network,
    blob_path, redis_key, blob_etag, last_indexed
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW())
ON CONFLICT (year, plan_category, group_number,
             COALESCE(plan_type, ''), COALESCE(plan_tier, ''),
             COALESCE(product_line, ''), COALESCE(variant, ''),
             COALESCE(network, ''))
DO UPDATE SET
    plan         = EXCLUDED.plan,
    blob_path    = EXCLUDED.blob_path,
    redis_key    = EXCLUDED.redis_key,
    last_indexed = NOW()
"""


def derive_blob_path(sub_index_path: str) -> str:
    """
    Derives the blob path from the local sub_index_path.
    Local: C:\\...\\indices\\2026_medical_ppo_1000016_...json
    Blob:  indices/2026_medical_ppo_1000016_...json
    """
    filename = os.path.basename(sub_index_path)
    return f"indices/{filename}"


def derive_redis_key(row: dict) -> str:
    """
    Derives Redis key from plan attributes.
    Format: index:{year}:{plan_category}:{group_number}:{variant}
    """
    return ":".join(
        [
            "index",
            (row.get("year") or "").strip(),
            (row.get("plan_category") or "").strip().lower(),
            (row.get("group_number") or "").strip(),
            (row.get("variant") or "standard").strip().lower(),
        ]
    )


def read_sqlite_rows() -> list:
    if not os.path.exists(SQLITE_PATH):
        logger.error("SQLite DB not found at: %s", SQLITE_PATH)
        sys.exit(1)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM master_index").fetchall()

    logger.info("Read %d rows from SQLite", len(rows))
    return [dict(r) for r in rows]


async def migrate():
    if not POSTGRES_DSN:
        logger.error("POSTGRES_DSN environment variable not set.")
        sys.exit(1)

    import asyncpg

    logger.info("Connecting to PostgreSQL...")
    pool = await asyncpg.create_pool(dsn=POSTGRES_DSN, min_size=1, max_size=5)

    # Create table and indexes
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)
    logger.info("Table and indexes created (or already exist).")

    # Read from SQLite
    rows = read_sqlite_rows()

    # Upsert into PostgreSQL
    success = 0
    failed = 0
    async with pool.acquire() as conn:
        for row in rows:
            try:
                blob_path = derive_blob_path(row.get("sub_index_path", ""))
                redis_key = derive_redis_key(row)
                await conn.execute(
                    UPSERT_SQL,
                    row.get("year", ""),
                    row.get("plan_category", ""),
                    row.get("plan", ""),
                    row.get("group_number", ""),
                    row.get("group_name", ""),
                    row.get("plan_type") or None,
                    row.get("plan_tier") or None,
                    row.get("product_line") or None,
                    row.get("variant") or None,
                    row.get("network") or None,
                    blob_path,
                    redis_key,
                    "",
                )
                success += 1
            except Exception as exc:
                logger.error("Failed to upsert row %s: %s", row, exc)
                failed += 1

    await pool.close()
    logger.info("Migration complete: %d succeeded, %d failed", success, failed)


if __name__ == "__main__":
    asyncio.run(migrate())
