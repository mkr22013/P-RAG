import json
import os
import re
import sqlite3
import logging
from functools import lru_cache
from fastmcp import FastMCP
from dotenv import load_dotenv

# ============================================================
# INIT
# ============================================================

load_dotenv()
DB_PATH = "p_insurance_index.db"

mcp = FastMCP("Insurance-Secure-RAG")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# MEMBER CONTEXT (HARDCODED FOR NOW)
# ============================================================


# This function should be revisit to ensure it will pick up the right metadata for the document
def get_member_plan_info_sbc():
    return {
        "year": "2026",
        "plan_category": "sbc",
        "plan_type": "PPO",
        "plan_tier": None,
        "product_line": "Your Future HSA Qualified Agg NGF - SF",
        "variant": "Standard",
        "network": "Unknown Network",
    }


def get_member_plan_info_medical():
    return {
        "year": "2026",
        "plan_category": "medical",
        "plan_type": "PPO",
        "plan_tier": None,
        "product_line": "Premera Employees Health Plan - Standard PPO Retiree Plan (Non-Grandfathered)",
        "variant": "Retiree",
        "network": "Unknown Network",
    }


def get_member_plan_info_dental():
    return {
        "year": "2026",
        "plan_category": "dental",
        "plan_type": "",
        "plan_tier": None,
        "product_line": "Willamette Dental Plan",
        "variant": "Standard",
        "network": "Unknown Network",
    }


# ============================================================
# QUERY PROCESSING
# ============================================================

STOPWORDS = {"the", "me", "about", "tell", "what", "is", "a", "an", "to", "of"}


def clean_content(text):
    if not text:
        return ""

    # remove useless labels
    text = text.replace("Category:", "")
    text = text.replace("Question:", "")
    text = text.replace("Answer:", "")

    # normalize spacing
    text = re.sub(r"\n\s*\n", "\n", text)

    return text.strip()


@lru_cache(maxsize=128)
def get_plan_data_from_disk(query, topics, category, keywords):
    """
    INTERNAL HELPER: Reads JSON index and returns best matching chunks.
    Uses topic (primary) + query (secondary) with scoring.
    """

    print(
        f"[*] SURGICAL FETCH: topics={topics} | query={query} | category={category} | keywords={keywords}"
    )

    try:

        if category == "medical":
            member_plan_info = get_member_plan_info_medical()
            print("[*] USING MEDICAL BOOKLET TO SEARCH CONTENT")
        elif category == "dental":
            member_plan_info = get_member_plan_info_dental()
            print("[*] USING DENTAL BOOKLET TO SEARCH CONTENT")
        else:
            member_plan_info = get_member_plan_info_sbc()
            print("[*] USING SBC BOOKLET TO SEARCH CONTENT")

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()

            sql = """
                SELECT sub_index_path
                FROM master_index
                WHERE 1=1
            """

            params = []

            sql += " AND year = ?"
            params.append(member_plan_info["year"])

            sql += " AND plan_category LIKE ?"
            params.append(f"%{member_plan_info["plan_category"]}%")

            if member_plan_info["plan_type"] != "":
                sql += " AND plan_type LIKE ?"
                params.append(f"%{member_plan_info["plan_type"]}%")

            sql += " AND plan_tier LIKE ?"
            params.append(f"%{member_plan_info["plan_tier"]}%")

            sql += " AND product_line LIKE ?"
            params.append(f"%{member_plan_info["product_line"]}%")

            sql += " AND variant LIKE ?"
            params.append(f"%{member_plan_info["variant"]}%")

            sql += " AND network LIKE ?"
            params.append(f"%{member_plan_info["network"]}%")

            print(f"[*] FINAL QUERY TO BE EXECUTED : {sql} ")
            print(f"[*] PARAM PASSED TO QUERY : {params}")
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            print(f"[*] ROWS RETURNED : {rows} ")

        if not rows:
            return f"ERROR: Plan for {member_plan_info["year"]} {member_plan_info["plan_tier"]} {member_plan_info["plan_type"]} not found."

        # Normalize inputs

        query = (query or "").lower()
        if isinstance(topics, list):
            print("topics are type of list")
        else:
            print("topics are not type of list")

        # # ============================================================
        # # 🔥 NORMALIZE TOPICS (ROBUST)
        # # ============================================================
        print(f"[*] RAW TOPICS: {topics}")

        sectioned_context = {}  # 🔥 group by type
        
        # ------------------------------------------------------------
        # 🔧 🔁 MAIN LOOP HELPERS
        # ------------------------------------------------------------
        def normalize_text(text):
            return re.sub(r"[^\w\s]", " ", str(text).lower())

        def tokenize(text):
            return [w for w in normalize_text(text).split() if len(w) > 2]

        def soft_match(word, text):
            """Loose match: handles singular/plural"""
            if word in text:
                return True
            if word.endswith("s") and word[:-1] in text:
                return True
            if (word + "s") in text:
                return True
            return False

        WEAK_WORDS = {
            "treatment",
            "service",
            "services",
            "care",
            "visit",
            "cost",
            "procedure",
            "therapy",
            "exam",
            "test",
        }

        # ============================================================
        # 🔁 MAIN LOOP
        # ============================================================
        for (sub_index_path,) in rows:
            print(f"[*] SEARCHING IN FILE : {sub_index_path}")

            if not os.path.exists(sub_index_path):
                continue

            with open(sub_index_path, "r", encoding="utf-8") as f:
                sub_index = json.load(f)

            print(f"[*] FILE SUCCESSFULLY FOUND AND PARSED : {sub_index_path}")

            # ------------------------------------------------------------
            # 🔥 NORMALIZATION
            # ------------------------------------------------------------
            if isinstance(topics, str):
                topics = [topics]
            elif isinstance(topics, tuple):
                topics = list(topics)

            if not topics:
                print("[*] NO TOPIC FOUND → USING GLOBAL SEARCH")
                topics = ["__all__"]

            query_lower = query.lower()
            query_clean = normalize_text(query)
            query_words = tokenize(query_clean)
            keywords = [k.lower().strip() for k in (keywords or []) if k]

            print(f"[*] QUERY WORDS: {query_words}")
            print(f"[*] KEYWORDS: {keywords}")
            print(f"[*] TOPICS: {topics}")

            # ------------------------------------------------------------
            # 🔥 SPLIT KEYWORDS
            # ------------------------------------------------------------
            strong_keywords = []
            weak_keywords = []

            for kw in keywords:
                parts = kw.split()
                if any(p in WEAK_WORDS for p in parts):
                    weak_keywords.append(kw)
                else:
                    strong_keywords.append(kw)

            # 🔥 NEW: split query words (SAFE)
            strong_query_words = [w for w in query_words if w not in WEAK_WORDS]

            # 🔥 NEW: phrase for exact match
            query_phrase = " ".join(query_words)

            # ============================================================
            # 🔍 PROCESS EACH TOPIC
            # ============================================================
            for topic in topics:
                print(f"\n[*] PROCESSING TOPIC: {topic}")

                topic_clean = normalize_text(topic)
                scored_chunks = []

                for p in sub_index:
                    chunk_topic = normalize_text(p.get("topic", ""))
                    chunk_keywords = " ".join(
                        [normalize_text(k) for k in p.get("keywords", [])]
                    )
                    content = normalize_text(p.get("content", ""))

                    full_text = f"{chunk_topic} {chunk_keywords} {content}"

                    score = 0
                    match_score = 0

                    # ====================================================
                    # 🔥 PHRASE MATCH (fixes "urgent care")
                    # ====================================================
                    if query_phrase and query_phrase in full_text:
                        score += 150
                        match_score += 2

                    # ====================================================
                    # 🔥 UNIFIED MATCH (UPDATED)
                    # ====================================================

                    # STRONG KEYWORDS → highest signal
                    for kw in strong_keywords:
                        if soft_match(kw, full_text):
                            match_score += 3

                    # ALL KEYWORDS
                    for kw in keywords:
                        if soft_match(kw, full_text):
                            match_score += 2

                    # 🔥 ONLY STRONG QUERY WORDS (FIX)
                    for w in strong_query_words:
                        if soft_match(w, full_text):
                            match_score += 1

                    # ❌ ONLY skip if NOTHING matches
                    if match_score == 0:
                        continue

                    # ====================================================
                    # 🔥 SCORING
                    # ====================================================

                    # 🧠 TOPIC BOOST (never filter)
                    if topic != "__all__":
                        if topic_clean in chunk_topic:
                            score += 60
                        elif topic_clean in full_text:
                            score += 30

                    # 🔥 STRONG KEYWORDS
                    for kw in strong_keywords:
                        if kw in full_text:
                            score += 120
                        elif soft_match(kw, chunk_topic):
                            score += 100
                        elif soft_match(kw, chunk_keywords):
                            score += 80
                        elif soft_match(kw, content):
                            score += 60

                    # 🔹 NORMAL KEYWORDS
                    for kw in keywords:
                        if soft_match(kw, full_text):
                            score += 40

                    # 🔥 ONLY STRONG QUERY WORDS (FIX)
                    for w in strong_query_words:
                        if soft_match(w, chunk_topic):
                            score += 20
                        elif soft_match(w, content):
                            score += 10

                    if score > 0:
                        scored_chunks.append((score, p))

                # ============================================================
                # 🎯 SORT
                # ============================================================
                scored_chunks.sort(key=lambda x: x[0], reverse=True)
                prioritized = [p for _, p in scored_chunks]

                print(f"[*] TOTAL MATCHED: {len(prioritized)}")

                # ============================================================
                # 🔥 LIST QUERY DETECTION
                # ============================================================
                is_list_query = (
                    any(w in query_lower for w in ["all", "list", "which"])
                    or "show me" in query_lower
                    or "what are" in query_lower
                    or "give me" in query_lower
                    or "cost" in query_lower
                )

                # ============================================================
                # 🔥 SAME EVENT GROUPING
                # ============================================================
                def extract_event(p):
                    c = p.get("content", {})
                    if isinstance(c, dict):
                        return normalize_text(c.get("event", p.get("topic", "")))
                    return normalize_text(p.get("topic", ""))

                event_set = set(extract_event(p) for p in prioritized)

                # ============================================================
                # 🔥 FINAL SELECTION
                # ============================================================
                if len(prioritized) == 1:
                    selected_chunks = prioritized[:1]

                elif len(event_set) == 1:
                    selected_chunks = prioritized

                elif is_list_query:
                    selected_chunks = prioritized[:10]

                else:
                    selected_chunks = prioritized[:3]

                if not selected_chunks and prioritized:
                    selected_chunks = prioritized[:1]

                print(f"[*] FINAL SELECTED COUNT: {len(selected_chunks)}")

                # ============================================================
                # 🔥 DEDUP
                # ============================================================
                seen = set()
                deduped_selected = []

                for c in selected_chunks:
                    key = (
                        c.get("category"),
                        c.get("topic"),
                        json.dumps(c.get("content", {}), sort_keys=True),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped_selected.append(c)

                selected_chunks = deduped_selected

                # ============================================================
                # 🧾 GROUP BY SECTION
                # ============================================================
                if "_seen_keys" not in sectioned_context:
                    sectioned_context["_seen_keys"] = set()

                for selected in selected_chunks:
                    if not isinstance(selected, dict):
                        continue

                    chunk_type = selected.get("category", "").lower().strip()

                    if chunk_type not in {"qa", "cost", "excluded"}:
                        continue

                    if chunk_type not in sectioned_context:
                        sectioned_context[chunk_type] = []

                    content = selected.get("content", {})

                    if not isinstance(content, dict):
                        continue

                    if chunk_type == "cost":
                        structured = {
                            "event": content.get("event", selected.get("topic", "")),
                            "service": content.get("service", selected.get("topic", "")),
                            "in_network": content.get("in_network") or "Data Not Found",
                            "out_of_network": content.get("out_of_network") or "Data Not Found",
                            "notes": content.get("limitations") or "Data Not Found",
                        }

                    elif chunk_type == "qa":
                        if not content.get("answer") and not content.get("explanation"):
                            continue

                        structured = {
                            "question": content.get("question", ""),
                            "answer": content.get("answer") or "Data Not Found",
                            "explanation": content.get("explanation") or "Data Not Found",
                        }

                    else:
                        structured = content

                    dedup_key = json.dumps(structured, sort_keys=True)

                    if dedup_key in sectioned_context["_seen_keys"]:
                        continue

                    sectioned_context["_seen_keys"].add(dedup_key)
                    sectioned_context[chunk_type].append(structured)

        # # ------------------------------------------------------------
        # # 🔧 🔁 MAIN LOOP HELPERS
        # # ------------------------------------------------------------
        # def normalize_text(text):
        #     return re.sub(r"[^\w\s]", " ", str(text).lower())

        # def tokenize(text):
        #     return [w for w in normalize_text(text).split() if len(w) > 2]

        # def soft_match(word, text):
        #     """Loose match: handles singular/plural"""
        #     if word in text:
        #         return True
        #     if word.endswith("s") and word[:-1] in text:
        #         return True
        #     if (word + "s") in text:
        #         return True
        #     return False

        # WEAK_WORDS = {
        #     "treatment",
        #     "service",
        #     "services",
        #     "care",
        #     "visit",
        #     "cost",
        #     "procedure",
        #     "therapy",
        #     "exam",
        #     "test",
        # }

        # # ============================================================
        # # 🔁 MAIN LOOP
        # # ============================================================
        # for (sub_index_path,) in rows:
        #     print(f"[*] SEARCHING IN FILE : {sub_index_path}")

        #     if not os.path.exists(sub_index_path):
        #         continue

        #     with open(sub_index_path, "r", encoding="utf-8") as f:
        #         sub_index = json.load(f)

        #     print(f"[*] FILE SUCCESSFULLY FOUND AND PARSED : {sub_index_path}")

        #     # ------------------------------------------------------------
        #     # 🔥 NORMALIZATION
        #     # ------------------------------------------------------------
        #     if isinstance(topics, str):
        #         topics = [topics]
        #     elif isinstance(topics, tuple):
        #         topics = list(topics)

        #     if not topics:
        #         print("[*] NO TOPIC FOUND → USING GLOBAL SEARCH")
        #         topics = ["__all__"]

        #     query_lower = query.lower()
        #     query_clean = normalize_text(query)
        #     query_words = tokenize(query_clean)
        #     keywords = [k.lower().strip() for k in (keywords or []) if k]

        #     print(f"[*] QUERY WORDS: {query_words}")
        #     print(f"[*] KEYWORDS: {keywords}")
        #     print(f"[*] TOPICS: {topics}")

        #     # ------------------------------------------------------------
        #     # 🔥 SPLIT KEYWORDS
        #     # ------------------------------------------------------------
        #     strong_keywords = []
        #     weak_keywords = []

        #     for kw in keywords:
        #         parts = kw.split()
        #         if any(p in WEAK_WORDS for p in parts):
        #             weak_keywords.append(kw)
        #         else:
        #             strong_keywords.append(kw)

        #     # ============================================================
        #     # 🔍 PROCESS EACH TOPIC
        #     # ============================================================
        #     for topic in topics:
        #         print(f"\n[*] PROCESSING TOPIC: {topic}")

        #         topic_clean = normalize_text(topic)
        #         scored_chunks = []

        #         for p in sub_index:
        #             chunk_topic = normalize_text(p.get("topic", ""))
        #             chunk_keywords = " ".join(
        #                 [normalize_text(k) for k in p.get("keywords", [])]
        #             )
        #             content = normalize_text(p.get("content", ""))

        #             full_text = f"{chunk_topic} {chunk_keywords} {content}"

        #             score = 0
        #             match_score = 0

        #             # ====================================================
        #             # 🔥 UNIFIED MATCH (NO HARD FILTER)
        #             # ====================================================

        #             # STRONG KEYWORDS → highest signal
        #             for kw in strong_keywords:
        #                 if soft_match(kw, full_text):
        #                     match_score += 3

        #             # ALL KEYWORDS
        #             for kw in keywords:
        #                 if soft_match(kw, full_text):
        #                     match_score += 2

        #             # QUERY WORDS fallback
        #             for w in query_words:
        #                 if soft_match(w, full_text):
        #                     match_score += 1

        #             # ❌ ONLY skip if NOTHING matches
        #             if match_score == 0:
        #                 continue

        #             # ====================================================
        #             # 🔥 SCORING
        #             # ====================================================

        #             # 🧠 TOPIC BOOST (never filter)
        #             if topic != "__all__":
        #                 if topic_clean in chunk_topic:
        #                     score += 60
        #                 elif topic_clean in full_text:
        #                     score += 30

        #             # 🔥 STRONG KEYWORDS
        #             for kw in strong_keywords:
        #                 if kw in full_text:
        #                     score += 120
        #                 elif soft_match(kw, chunk_topic):
        #                     score += 100
        #                 elif soft_match(kw, chunk_keywords):
        #                     score += 80
        #                 elif soft_match(kw, content):
        #                     score += 60

        #             # 🔹 NORMAL KEYWORDS
        #             for kw in keywords:
        #                 if soft_match(kw, full_text):
        #                     score += 40

        #             # 🔹 QUERY WORDS
        #             for w in query_words:
        #                 if soft_match(w, chunk_topic):
        #                     score += 20
        #                 elif soft_match(w, content):
        #                     score += 10

        #             if score > 0:
        #                 scored_chunks.append((score, p))

        #         # ============================================================
        #         # 🎯 SORT
        #         # ============================================================
        #         scored_chunks.sort(key=lambda x: x[0], reverse=True)
        #         prioritized = [p for _, p in scored_chunks]

        #         print(f"[*] TOTAL MATCHED: {len(prioritized)}")

        #         # ============================================================
        #         # 🔥 LIST QUERY DETECTION
        #         # ============================================================
        #         is_list_query = (
        #             any(w in query_lower for w in ["all", "list", "which"])
        #             or "show me" in query_lower
        #             or "what are" in query_lower
        #             or "give me" in query_lower
        #             or "cost" in query_lower
        #         )

        #         # ============================================================
        #         # 🔥 SAME EVENT GROUPING
        #         # ============================================================
        #         def extract_event(p):
        #             c = p.get("content", {})
        #             if isinstance(c, dict):
        #                 return normalize_text(c.get("event", p.get("topic", "")))
        #             return normalize_text(p.get("topic", ""))

        #         event_set = set(extract_event(p) for p in prioritized)

        #         # ============================================================
        #         # 🔥 FINAL SELECTION
        #         # ============================================================
        #         if len(prioritized) == 1:
        #             selected_chunks = prioritized[:1]

        #         elif len(event_set) == 1:
        #             selected_chunks = prioritized  # SAME BENEFIT → return all

        #         elif is_list_query:
        #             selected_chunks = prioritized[:10]

        #         else:
        #             selected_chunks = prioritized[:3]

        #         if not selected_chunks and prioritized:
        #             selected_chunks = prioritized[:1]

        #         print(f"[*] FINAL SELECTED COUNT: {len(selected_chunks)}")

        #         # ============================================================
        #         # 🔥 DEDUP
        #         # ============================================================
        #         seen = set()
        #         deduped_selected = []

        #         for c in selected_chunks:
        #             key = (
        #                 c.get("category"),
        #                 c.get("topic"),
        #                 json.dumps(c.get("content", {}), sort_keys=True),
        #             )
        #             if key in seen:
        #                 continue
        #             seen.add(key)
        #             deduped_selected.append(c)

        #         selected_chunks = deduped_selected

        #         # ============================================================
        #         # 🧾 GROUP BY SECTION
        #         # ============================================================
        #         if "_seen_keys" not in sectioned_context:
        #             sectioned_context["_seen_keys"] = set()

        #         for selected in selected_chunks:
        #             if not isinstance(selected, dict):
        #                 continue

        #             chunk_type = selected.get("category", "").lower().strip()

        #             if chunk_type not in {"qa", "cost", "excluded"}:
        #                 continue

        #             if chunk_type not in sectioned_context:
        #                 sectioned_context[chunk_type] = []

        #             content = selected.get("content", {})

        #             if not isinstance(content, dict):
        #                 continue

        #             if chunk_type == "cost":
        #                 structured = {
        #                     "event": content.get("event", selected.get("topic", "")),
        #                     "service": content.get("service", selected.get("topic", "")),
        #                     "in_network": content.get("in_network") or "Data Not Found",
        #                     "out_of_network": content.get("out_of_network") or "Data Not Found",
        #                     "notes": content.get("limitations") or "Data Not Found",
        #                 }

        #             elif chunk_type == "qa":
        #                 if not content.get("answer") and not content.get("explanation"):
        #                     continue

        #                 structured = {
        #                     "question": content.get("question", ""),
        #                     "answer": content.get("answer") or "Data Not Found",
        #                     "explanation": content.get("explanation") or "Data Not Found",
        #                 }

        #             else:
        #                 structured = content

        #             dedup_key = json.dumps(structured, sort_keys=True)

        #             if dedup_key in sectioned_context["_seen_keys"]:
        #                 continue

        #             sectioned_context["_seen_keys"].add(dedup_key)
        #             sectioned_context[chunk_type].append(structured)

        # ============================================================
        # 🧠 BUILD FINAL CONTEXT (UNCHANGED ✅)
        # ============================================================
        final_context = ""

        for section, chunks in sectioned_context.items():
            if section.startswith("_"):
                continue

            print(f"[*] BUILDING SECTION: {section}")

            final_context += f"\n\n### SECTION: {section.upper()}\n\n"

            for i, c in enumerate(chunks, 1):
                final_context += f"Item {i}:\n"
                final_context += json.dumps(c, indent=2)
                final_context += "\n\n"

        return final_context.strip()

    except Exception as e:
        return f"ERROR: {str(e)}"


# ============================================================
# MCP TOOL
# ============================================================


@mcp.tool()
def query_insurance_benefits(query, topics, category, keywords):
    """
    RETRIEVAL TOOL
    """
    logger.info(f"[TOOL CALL] query_insurance_benefits: {query}")
    topics_tuple = tuple(topics) if isinstance(topics, list) else topics
    keywords_tuple = tuple(keywords) if isinstance(keywords, list) else keywords
    result = get_plan_data_from_disk(query, topics_tuple, category, keywords_tuple)
    print(f"[*] RESULT RETURNED FROM SERVER : {result}")

    if result is None:
        logger.warning("[!] No FTS result, using fallback")
        # return fallback_to_summary(query, "medical")

    return str(result)  # 🔥 ALWAYS STRING


# ============================================================
# SERVER START
# ============================================================

if __name__ == "__main__":
    logger.info("Starting FastMCP server...")
    mcp.run(transport="stdio")


# def debug_fts():
#     import sqlite3

#     conn = sqlite3.connect(DB_PATH)

#     queries = [
#         "primary",
#         "pcp",
#         "visit",
#         "care",
#         "copay",
#         "primary care",
#     ]

#     for q in queries:
#         try:
#             print(f"\n=== QUERY: {q} ===")

#             sql = """
#                 SELECT rowid, content
#                 FROM search_index
#                 WHERE search_index MATCH ?
#                 LIMIT 3
#             """

#             rows = conn.execute(sql, (q,)).fetchall()

#             print(f"Rows: {len(rows)}")

#             for r in rows:
#                 print(r[1][:200])

#         except Exception as e:
#             print("ERROR:", e)

#     conn.close()

# # ============================================================
# # FALLBACK (UNCHANGED)
# # ============================================================


# def fallback_to_summary(topic, benefit_category):
#     try:
#         member_plan_info = get_member_plan_info()

#         with sqlite3.connect(DB_PATH) as conn:
#             cursor = conn.execute(
#                 """
#                 SELECT sub_index_path
#                 FROM master_index
#                 WHERE year = ? AND plan_tier = ? AND plan_type = ?
#                 AND product_line = ? AND variant = ?
#                 """,
#                 (
#                     int(member_plan_info["year"]),
#                     member_plan_info["plan_tier"],
#                     member_plan_info["plan_type"],
#                     member_plan_info["product_line"],
#                     member_plan_info["variant"],
#                 ),
#             )

#             res = cursor.fetchone()

#             if res and os.path.exists(res[0]):
#                 with open(res[0], "r", encoding="utf-8") as f:
#                     sub_index = json.load(f)

#                 return "\n\n".join([item.get("content", "") for item in sub_index[:4]])

#         return "ERROR: No fallback data found."

#     except Exception as e:
#         return f"ERROR: {str(e)}"
