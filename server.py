from functools import lru_cache
import json, os, sqlite3
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()
# Check this filename against your sidebar!
# If sidebar says p_insurance_index.db, use that.
DB_PATH = "p_insurance_index.db"

mcp = FastMCP("Insurance-Secure-RAG")
# 1. GLOBAL CACHE FOR BENEFIT EXTRACTION (to speed up repeated queries)


@mcp.tool()
def get_available_plans() -> str:
    """
    DISCOVERY TOOL: Returns a unique list of all Plan Types, Tiers, and Years.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT year, plan_type, plan_tier FROM master_index ORDER BY year DESC"
            )
            rows = cursor.fetchall()

        if not rows:
            return "DATABASE INFO: The index is currently empty."

        return f"DATA SOURCE SCHEMA (Year, Type, Tier): {str(rows)}"
    except Exception as e:
        return f"DISCOVERY ERROR: {str(e)}"


# ---(LRU: Least Recently Used) --- python in built memory caching decorator.
# This keeps the last 128 unique plan lookups in RAM across the entire session.
# It is 100% thread-safe and prevents the 'Triple Fetch' bug.
# in production grade system we need to move this to distributed cache like redis or memcached
@lru_cache(maxsize=128)
def get_plan_data_from_disk(year, plan_type, plan_tier, topic):
    """
    INTERNAL HELPER: This is the only part that actually touches the DB and JSON.
    """
    print(f"[*] SURGICAL FETCH (DISK READ): {year} {plan_tier} {plan_type} - {topic}")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = "SELECT year, plan_type, plan_tier, sub_index_path FROM master_index WHERE 1=1"
            params = []
            if year:
                query += " AND year = ?"
                params.append(year)
            if plan_type:
                query += " AND plan_type LIKE ?"
                params.append(f"%{plan_type}%")
            if plan_tier:
                query += " AND plan_tier LIKE ?"
                params.append(f"%{plan_tier}%")
            cursor.execute(query, params)
            rows = cursor.fetchall()

        if not rows:
            return f"ERROR: Plan for {year} {plan_tier} {plan_type} not found."

        combined_results = ""
        for r_year, r_type, r_tier, sub_index_file in rows:
            if not os.path.exists(sub_index_file):
                continue
            with open(sub_index_file, "r", encoding="utf-8") as f:
                sub_index = json.load(f)

            # --- SEARCH LOGIC (WITH FALLBACK) ---
            best_chunks = [
                p
                for p in sub_index
                if any(topic.lower() in str(k).lower() for k in p.get("keywords", []))
                or topic.lower() in str(p.get("topic", "")).lower()
                or topic.lower() in str(p.get("content", "")).lower()
            ]

            # CRITICAL FIX: If no specific 'specialist' or 'emergency' chunk is found,
            # fall back to the first 2 pages (the summary/deductible page).
            # This ensures the LLM has something to read for your 2024/2025 test docs.
            if not best_chunks and len(sub_index) > 0:
                print(
                    f"[*] SEARCH FALLBACK: Topic '{topic}' not found in {r_year}. Using summary chunks."
                )
                best_chunks = sub_index[:2]

            page_context = ""
            for chunk in best_chunks[:4]:
                page_context += f"\n--- {r_year} {r_tier} {r_type} ---\n[SECTION: {chunk.get('topic', 'Detail')}]\n{chunk.get('content', '')}\n"
            combined_results += page_context

        return combined_results
    except Exception as e:
        return f"ERROR: {str(e)}"


@mcp.tool()
def query_insurance_benefits(
    year=None, plan_type=None, plan_tier=None, topic="deductible"
):
    """
    RETRIEVAL TOOL: Wrapper that calls the cached disk-reader.
    """
    # Simply call the cached helper. Python handles the 'Triple Fetch' logic for you.
    return get_plan_data_from_disk(year, plan_type, plan_tier, topic.lower())


if __name__ == "__main__":
    mcp.run(transport="stdio")

# @mcp.tool()
# def query_insurance_benefits(
#     year: int | None = None,
#     plan_type: str | None = None,
#     plan_tier: str | None = None,
#     topic: str = "deductible",
# ) -> str:
#     """
#     RETRIEVAL TOOL: Extracts structured Markdown benefit data from the index.
#     """

#     try:
#         with sqlite3.connect(DB_PATH) as conn:
#             cursor = conn.cursor()
#             query = "SELECT year, plan_type, plan_tier, sub_index_path FROM master_index WHERE 1=1"
#             params = []

#             if year:
#                 query += " AND year = ?"
#                 params.append(year)
#             if plan_type:
#                 query += " AND plan_type LIKE ?"
#                 params.append(f"%{plan_type}%")
#             if plan_tier:
#                 query += " AND plan_tier LIKE ?"
#                 params.append(f"%{plan_tier}%")

#             cursor.execute(query, params)
#             rows = cursor.fetchall()

#         if not rows:
#             return "ERROR: No matching plans found. Call 'get_available_plans' to see valid options."

#     except Exception as e:
#         return f"DATABASE ERROR: {str(e)}"

#     combined_results = ""
#     for r_year, r_type, r_tier, sub_index_file in rows:
#         if not os.path.exists(sub_index_file):
#             continue

#         with open(sub_index_file, "r", encoding="utf-8") as f:
#             sub_index = json.load(f)

#         # SEARCH logic
#         best_chunks = [
#             p
#             for p in sub_index
#             if any(topic.lower() in k.lower() for k in p.get("keywords", []))
#             or topic.lower() in p.get("topic", "").lower()
#         ]

#         # FALLBACK: If no specific match, take the first chunk
#         if not best_chunks and len(sub_index) > 0:
#             best_chunks = [sub_index[0]]

#         page_context = ""
#         for chunk in best_chunks[:2]:
#             page_context += f"\n[SECTION: {chunk.get('topic', 'Detail')}]\n{chunk.get('content', '')}\n"

#         combined_results += f"\n--- {r_year} {r_tier} {r_type} ---\n{page_context}\n"

#     return combined_results
