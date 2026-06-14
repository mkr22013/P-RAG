"""
infrastructure/db.py
────────────────────────────────────────────────────────────────────────────
PostgreSQL client for the master plan index.

Replaces the SQLite p_insurance_index.db used in local development.
Uses asyncpg for async connection pooling — production grade for FastAPI.

Environment variables:
    POSTGRES_DSN  — full connection string, e.g.:
                    postgresql://user:password@host:5432/dbname

Schema (run migration script once):
    See scripts/migrate_sqlite_to_pg.py

Local dev fallback:
    When POSTGRES_DSN is not set, falls back to SQLite exactly as today.
    Zero impact on local development.
"""

import os
from config import settings
import logging
from typing import Optional

logger = logging.getLogger(__name__)

POSTGRES_DSN = settings.POSTGRES_DSN

# ── Async PostgreSQL pool (production) ────────────────────────────────────────
_pg_pool = None


async def get_pg_pool():
    """Returns the shared asyncpg connection pool, creating it on first call."""
    global _pg_pool
    if _pg_pool is None:
        try:
            import asyncpg

            _pg_pool = await asyncpg.create_pool(
                dsn=POSTGRES_DSN,
                min_size=2,
                max_size=10,
                command_timeout=10,
                statement_cache_size=0,  # Required for Azure PostgreSQL with PgBouncer
            )
            logger.info("[db] PostgreSQL connection pool created.")
        except Exception as exc:
            logger.error("[db] Failed to create PostgreSQL pool: %s", exc)
            raise
    return _pg_pool


async def close_pg_pool():
    """Call on application shutdown to cleanly close the connection pool."""
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        _pg_pool = None
        logger.info("[db] PostgreSQL connection pool closed.")


# ── SQLite fallback (local dev) ───────────────────────────────────────────────
def _sqlite_query(
    year: str,
    plan_category: str,
    plan: str,
    group_number: str,
    plan_type: str,
    plan_tier: str,
    product_line: str,
    variant: str,
    network: str,
) -> Optional[str]:
    """
    Falls back to the existing SQLite database for local development.
    Returns the sub_index_path (local file path) or None if not found.
    """
    import sqlite3
    import os

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "indexers", "p_insurance_index.db"
    )

    if not os.path.exists(db_path):
        logger.warning("[db] SQLite DB not found at %s", db_path)
        return None

    def _like(val):
        return f"%{val}%" if val else "%"

    def _null_clause(col, val):
        if not val or val.upper() in ("NULL", "NONE", ""):
            return f"({col} IS NULL OR {col} = '' OR {col} = 'NULL')"
        return f"{col} LIKE ?"

    conditions = [
        "year = ?",
        "plan_category = ?",
        f"plan LIKE ?",
        f"group_number LIKE ?",
        _null_clause("plan_type", plan_type),
        _null_clause("plan_tier", plan_tier),
        _null_clause("product_line", product_line),
        f"variant LIKE ?",
        _null_clause("network", network),
    ]

    params = [year, plan_category, _like(plan), _like(group_number)]
    if plan_type and plan_type.upper() not in ("NULL", "NONE", ""):
        params.append(_like(plan_type))
    if plan_tier and plan_tier.upper() not in ("NULL", "NONE", ""):
        params.append(_like(plan_tier))
    if product_line and product_line.upper() not in ("NULL", "NONE", ""):
        params.append(_like(product_line))
    params.append(_like(variant))
    if network and network.upper() not in ("NULL", "NONE", ""):
        params.append(_like(network))

    query = f"SELECT sub_index_path FROM master_index WHERE {' AND '.join(conditions)}"

    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        if rows:
            return rows[0][0]
        return None
    except Exception as exc:
        logger.error("[db] SQLite query failed: %s", exc)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────


async def get_index_path(
    year: str,
    plan_category: str,
    plan: str,
    group_number: str,
    plan_type: str = "",
    plan_tier: str = "",
    product_line: str = "",
    variant: str = "",
    network: str = "",
) -> Optional[dict]:
    """
    Looks up the index location for a given plan.

    Returns dict with keys:
        - local: production → None, dev → local file path
        - blob_path: production → Azure Blob path, dev → None
        - redis_key: production → Redis cache key, dev → None

    Local dev (no POSTGRES_DSN):
        Returns { "local": "/path/to/index.json", "blob_path": None, "redis_key": None }

    Production (POSTGRES_DSN set):
        Returns { "local": None, "blob_path": "indices/...", "redis_key": "index:..." }
    """
    if not POSTGRES_DSN:
        # Local dev — use SQLite
        local_path = _sqlite_query(
            year,
            plan_category,
            plan,
            group_number,
            plan_type,
            plan_tier,
            product_line,
            variant,
            network,
        )
        if local_path:
            return {"local": local_path, "blob_path": None, "redis_key": None}
        return None

    # Production — use PostgreSQL
    try:
        pool = await get_pg_pool()

        def _null_cond(col, val):
            if not val or val.upper() in ("NULL", "NONE", ""):
                return f"({col} IS NULL OR {col} = '' OR {col} = 'NULL')"
            return f"{col} ILIKE ${{}}"

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT blob_path, redis_key
                FROM master_index
                WHERE year = $1
                  AND plan_category = $2
                  AND plan ILIKE $3
                  AND group_number ILIKE $4
                  AND (plan_type IS NULL OR plan_type = '' OR plan_type ILIKE $5)
                  AND (plan_tier IS NULL OR plan_tier = '' OR plan_tier ILIKE $6)
                  AND (product_line IS NULL OR product_line = '' OR product_line ILIKE $7)
                  AND variant ILIKE $8
                  AND (network IS NULL OR network = '' OR network ILIKE $9)
                LIMIT 1
                """,
                year,
                plan_category,
                f"%{plan}%",
                f"%{group_number}%",
                f"%{plan_type}%" if plan_type else "%",
                f"%{plan_tier}%" if plan_tier else "%",
                f"%{product_line}%" if product_line else "%",
                f"%{variant}%",
                f"%{network}%" if network else "%",
            )

        if row:
            return {
                "local": None,
                "blob_path": row["blob_path"],
                "redis_key": row["redis_key"],
            }
        return None

    except Exception as exc:
        logger.error("[db] PostgreSQL query failed: %s", exc)
        return None


async def upsert_index_entry(
    year: str,
    plan_category: str,
    plan: str,
    group_number: str,
    group_name: str,
    plan_type: str,
    plan_tier: str,
    product_line: str,
    variant: str,
    network: str,
    blob_path: str,
    redis_key: str,
    blob_etag: str = "",
) -> bool:
    """
    Inserts or updates a master index entry after successful indexing.
    Called by the indexer after uploading a new JSON index to blob.
    Returns True on success, False on failure.
    """
    if not POSTGRES_DSN:
        logger.warning(
            "[db] upsert_index_entry called but POSTGRES_DSN not set — skipping"
        )
        return False

    try:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO master_index (
                    year, plan_category, plan, group_number, group_name,
                    plan_type, plan_tier, product_line, variant, network,
                    blob_path, redis_key, blob_etag, last_indexed
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW())
                ON CONFLICT (year, plan_category, group_number, plan_type,
                             plan_tier, product_line, variant, network)
                DO UPDATE SET
                    blob_path    = EXCLUDED.blob_path,
                    redis_key    = EXCLUDED.redis_key,
                    blob_etag    = EXCLUDED.blob_etag,
                    last_indexed = NOW()
                """,
                year,
                plan_category,
                plan,
                group_number,
                group_name,
                plan_type or "",
                plan_tier or "",
                product_line or "",
                variant or "",
                network or "",
                blob_path,
                redis_key,
                blob_etag or "",
            )
        logger.info(
            "[db] Upserted index entry: %s / %s / %s", year, plan_category, group_number
        )
        return True
    except Exception as exc:
        logger.error("[db] upsert_index_entry failed: %s", exc)
        return False


async def get_last_indexed(
    year: str,
    plan_category: str,
    group_number: str,
    variant: str = "",
) -> Optional[str]:
    """
    Returns the last_indexed timestamp for a plan as ISO string.
    Used by indexer to check if a PDF has changed since last indexing.
    Returns None if plan not found or not yet indexed.
    """
    if not POSTGRES_DSN:
        # Local dev — check SQLite
        try:
            import sqlite3 as _sqlite3

            db_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "indexers",
                "p_insurance_index.db",
            )
            if not os.path.exists(db_path):
                return None
            with _sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT last_indexed FROM master_index WHERE year=? AND plan_category=? AND group_number=? AND variant LIKE ?",
                    (year, plan_category, group_number, f"%{variant}%"),
                ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    try:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_indexed FROM master_index
                WHERE year=$1 AND plan_category=$2
                  AND group_number=$3 AND variant ILIKE $4
                LIMIT 1
                """,
                year,
                plan_category,
                group_number,
                f"%{variant}%",
            )
        return row["last_indexed"].isoformat() if row else None
    except Exception as exc:
        logger.error("[db] get_last_indexed failed: %s", exc)
        return None
