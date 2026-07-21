import json
import os
import re
import sqlite3
import logging
import asyncio
from functools import lru_cache
from fastmcp import FastMCP
from dotenv import load_dotenv

from utility.utils import (
    expand_query_keywords,
    find_kb_gaps,
    _log_kb_gaps_async,
    RX_NOISE_WORDS,
)

# ============================================================
# INIT
# ============================================================

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "indexers", "p_insurance_index.db")

mcp = FastMCP("Insurance-Secure-RAG")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
def get_plan_data_from_disk(
    query,
    topics,
    category,
    keywords,
    member_info: str = "{}",
    include_requirements_text: bool = False,
    include_limitations_text: bool = False,
):
    """
    INTERNAL HELPER: Reads JSON index and returns best matching chunks.
    Uses topic (primary) + query (secondary) with scoring.
    member_info: JSON string from /member-info endpoint.
    Must contain 'plans' dict with category keys and 'year' at top level.

    include_requirements_text: Rx only. See earlier docstring section —
        enables condition/category-style Rx queries to search requirements_text
        (e.g. "Preventive No Cost") without reopening false-positive risk for
        specific-drug-name queries.

    include_limitations_text: Medical/dental/vision INFO chunks only. Some
        INFO chunks (e.g. "Out-Of-Area Care", travel coverage explanations)
        have event/service fields that are just generic labels — the actual
        substantive explanation lives entirely in 'limitations'. Without this
        flag, such chunks are unfindable via topic/keyword search, since
        searchable content defaults to event+service only (to prevent dollar
        amounts and cost-share details in limitations from causing false-
        positive matches on COST queries). Only set True for genuinely
        conceptual/info-seeking queries — same opt-in safety pattern as Rx's
        include_requirements_text.
    """

    print(
        f"[*] SURGICAL FETCH: topics={topics} | query={query} | category={category} | keywords={keywords}"
    )

    try:
        # Use member_info from request if provided
        parsed_member_info = {}
        plan_for_category = {}
        try:
            parsed_member_info = (
                json.loads(member_info) if member_info and member_info != "{}" else {}
            )
            plans = parsed_member_info.get("plans", {})
            plan_for_category = plans.get(category, {})
        except (json.JSONDecodeError, AttributeError):
            plan_for_category = {}

        if not plan_for_category:
            return f"ERROR: No member plan info available for category: {category}"

        member_plan_info = {
            **plan_for_category,
            "plan_category": category,
            "year": parsed_member_info.get("year", "2026"),
        }
        print(f"[*] USING MEMBER INFO FROM REQUEST for {category.upper()}")

        print(f"[*] DB PATH : {DB_PATH} ")

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()

            # We use a base query and build filters carefully
            sql = "SELECT sub_index_path FROM master_index WHERE year = ? AND plan_category = ?"
            params = [member_plan_info["year"], member_plan_info["plan_category"]]

            # List of fields to filter on
            # Format: (DB_COLUMN_NAME, DICTIONARY_KEY)
            filters = [
                ("plan", "plan"),
                ("group_number", "group_number"),
                ("plan_type", "plan_type"),
                ("plan_tier", "plan_tier"),
                ("product_line", "product_line"),
                ("variant", "variant"),
                ("network", "network"),
            ]

            for db_col, dict_key in filters:
                val = str(member_plan_info.get(dict_key, "")).strip()

                # If value is empty or the string "NULL", we check for empty OR null in DB
                if val == "" or val.upper() == "NULL":
                    sql += (
                        f" AND ({db_col} IS NULL OR {db_col} = '' OR {db_col} = 'NULL')"
                    )
                else:
                    # Use exact match for stability, or LIKE if you prefer partial
                    sql += f" AND {db_col} LIKE ?"
                    params.append(f"%{val}%")

            print(f"[*] FINAL QUERY: {sql}")
            print(f"[*] PARAMS: {params}")

            cursor.execute(sql, params)
            rows = cursor.fetchall()
            print(f"[*] ROWS RETURNED: {rows}")

        if not rows:
            return f"ERROR: Plan not found for {member_plan_info['plan_category']} - {member_plan_info['variant']}"

        # Normalize inputs

        query = (query or "").lower()
        if isinstance(topics, list):
            print("topics are type of list")
        else:
            print("topics are not type of list")

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
            if re.search(r"\b" + re.escape(word) + r"\b", text):
                return True
            if word.endswith("s"):
                if re.search(r"\b" + re.escape(word[:-1]) + r"\b", text):
                    return True
            if re.search(r"\b" + re.escape(word + "s") + r"\b", text):
                return True
            return False

        WEAK_WORDS = {
            "treatment",
            "service",
            "services",
            "care",
            "visit",
            "visits",
            "cost",
            "procedure",
            "therapy",
            "exam",
            "test",
            # Location/setting words — describe WHERE a service is rendered,
            # not WHAT the benefit is.  Must stay in sync with client.py WEAK_WORDS.
            "office",
            "clinic",
            "clinics",
            "setting",
            "settings",
            "facility",
            "facilities",
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
            # Expand query keywords via knowledge base before chunk scoring.
            # Bridges gap between member language and index terms:
            #   "cleaning"         → adds "prophylaxis"
            #   "glasses"          → adds "vision hardware"
            #   "physical therapy" → adds "rehabilitation"
            #   "panoramic"        → adds "full mouth xray", "panoramic xray"
            # Pure dict lookup — 0 tokens, 0 LLM calls, microseconds.
            # If word not in KB → used as-is, normal scoring (graceful degradation).

            expanded_keywords = expand_query_keywords(keywords, category)
            # Fire-and-forget gap detection — find terms with no KB match.
            # Logged to kb_gap_log.jsonl for offline batch review by build_kb.py.
            # Never awaited, never blocks response — zero latency impact.
            gaps = find_kb_gaps(keywords, expanded_keywords, category)
            if gaps:
                try:
                    asyncio.create_task(_log_kb_gaps_async(gaps, category, query))
                except RuntimeError:
                    # No running event loop (test/dev context) — skip silently
                    pass

            keywords = expanded_keywords

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

                    # EXPERIMENT: only search IDENTITY fields, not the entire
                    # content dict. See RX_INDEXER_FLOW.md discussion — testing
                    # in isolation now that the phonetic category-detection bug
                    # (the actual root cause of the earlier "regression") has
                    # been confirmed and removed separately.
                    content_dict = p.get("content", {})
                    if isinstance(content_dict, dict):
                        chunk_type_for_fields = p.get("category", "")
                        if chunk_type_for_fields == "rx":
                            searchable_content = (
                                f"{content_dict.get('drug_name', '')} "
                                f"{content_dict.get('tier_label', '')} "
                                f"{content_dict.get('drug_category', '')} "
                                f"{content_dict.get('drug_subcategory', '')}"
                            )
                            # Only include requirements_text when explicitly
                            # requested for condition/category-style queries
                            # (no specific drug name in the query) — see
                            # function docstring for why this must stay
                            # opt-in, not default-on.
                            if include_requirements_text:
                                searchable_content += (
                                    f" {content_dict.get('requirements_text', '')}"
                                )
                        else:
                            searchable_content = (
                                f"{content_dict.get('event', '')} "
                                f"{content_dict.get('service', '')}"
                            )
                            # For INFO chunks specifically, the actual
                            # explanatory content often lives ENTIRELY in
                            # 'limitations' (e.g. "Out-Of-Area Care" — the
                            # event/service fields are just labels; the real
                            # travel-coverage explanation is in limitations).
                            # Same opt-in pattern as Rx's requirements_text:
                            # only included when explicitly requested for
                            # conceptual/info-seeking queries, never for
                            # specific cost-lookup queries, to avoid
                            # reopening the dollar-amount leakage bug this
                            # scoping fix was built to prevent.
                            if (
                                include_limitations_text
                                and chunk_type_for_fields == "info"
                            ):
                                searchable_content += (
                                    f" {content_dict.get('limitations', '')}"
                                )
                    else:
                        searchable_content = str(content_dict)

                    content = normalize_text(searchable_content)

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

                    # ── INFO ENTRIES: score on event name only ─────────────
                    chunk_category = p.get("category", "")
                    if chunk_category == "info":
                        content_dict = p.get("content", {})
                        event_name = (
                            content_dict.get("event", "")
                            if isinstance(content_dict, dict)
                            else ""
                        )
                        event_lower = event_name.lower()
                        info_score = 0

                        # Direct phrase match — MULTI-WORD keywords only.
                        # "vision hardware" → matches "Vision Hardware" event directly.
                        # Single words ("medical", "contact") use word-count logic
                        # below to avoid false positives across many events.
                        #
                        # GUARD: skip keywords whose every word is a weak/setting term.
                        # Example: "office visits" is all-weak → must NOT score 400 against
                        # "Split Copay For Office Visits" for a "foot care" query.
                        # "vision hardware" has "vision"+"hardware" (both meaningful) → allowed.
                        _PHRASE_STOP = {
                            "and",
                            "or",
                            "in",
                            "an",
                            "the",
                            "a",
                            "for",
                            "of",
                        }
                        for kw in keywords:
                            kw_lower = kw.lower().strip()
                            if " " not in kw_lower:
                                continue
                            kw_parts = [
                                p
                                for p in re.split(r"\W+", kw_lower)
                                if p and len(p) > 1 and p not in _PHRASE_STOP
                            ]
                            if kw_parts and all(p in WEAK_WORDS for p in kw_parts):
                                continue  # all-weak keyword — skip phrase match
                            if kw_lower in event_lower or event_lower in kw_lower:
                                # Penalise "non-X" events when query is about X
                                if event_lower.startswith(
                                    "non-"
                                ) or event_lower.startswith("non "):
                                    info_score += 100
                                # Boost exact event name match
                                elif event_lower == kw_lower or kw_lower == event_lower:
                                    info_score += 600
                                else:
                                    info_score += 400
                                break

                        # Word-by-word match (single words or when no phrase matched)
                        if info_score == 0:
                            INFO_STOP = {
                                "what",
                                "which",
                                "does",
                                "how",
                                "and",
                                "for",
                                "under",
                                "about",
                                "when",
                                "where",
                                "covered",
                                "covers",
                                "tell",
                                "show",
                                "give",
                                "know",
                                "get",
                                "can",
                                "will",
                                "has",
                                "also",
                                "any",
                                "all",
                                "are",
                                "the",
                                "its",
                                "is",
                                "it",
                                "my",
                                "me",
                                "do",
                                "affect",
                                "affects",
                                "impact",
                                "impacts",
                                "happen",
                                "happens",
                                "work",
                                "works",
                                "related",
                                "regarding",
                            }
                            info_words = [
                                w
                                for w in strong_query_words
                                if w not in INFO_STOP and len(w) > 3
                            ]
                            if not info_words:
                                info_words = strong_query_words

                            matched = [
                                w for w in info_words if soft_match(w, event_lower)
                            ]
                            required = (
                                min(2, len(info_words)) if len(info_words) > 1 else 1
                            )

                            # Single-word keyword matches against event name only.
                            # Deliberately NOT matching against chunk_keywords to avoid
                            # false positives where a keyword appears in content text
                            # but not in the event name.
                            # e.g. "emergency" appears in E-Visit Exclusions chunk keywords
                            # because the content mentions "Emergency consultations" —
                            # but the event is NOT about emergency room.
                            # Exception: short terms like "tmj" (len<=3) that are filtered
                            # from info_words still need chunk_keywords matching.
                            for kw in keywords:
                                kw_lower = kw.lower().strip()
                                if " " not in kw_lower:
                                    if soft_match(kw_lower, event_lower):
                                        matched.append(kw_lower)
                                    elif len(kw_lower) <= 3 and soft_match(
                                        kw_lower, chunk_keywords
                                    ):
                                        matched.append(kw_lower)

                            if len(matched) >= required:
                                info_score = 200 * len(set(matched))

                        if info_score > 0:
                            scored_chunks.append((info_score, p))
                        continue  # skip general scoring for info entries

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
                    # For Rx chunks specifically, filter out RX_NOISE_WORDS —
                    # same shared stoplist client.py uses for rx_keywords
                    # extraction. Without this, generic words like "drugs"
                    # soft-match against EVERY Rx chunk's drug_category/
                    # drug_subcategory field (e.g. "...Immunosuppressant
                    # DRUGS"), causing near-universal false-positive scoring
                    # for queries like "I want to know about my preventive
                    # drugs?" — every chunk gets the same baseline score from
                    # "drugs" alone, regardless of actual relevance.
                    chunk_category_for_noise = p.get("category", "")
                    effective_strong_query_words = (
                        [w for w in strong_query_words if w not in RX_NOISE_WORDS]
                        if chunk_category_for_noise == "rx"
                        else strong_query_words
                    )
                    for w in effective_strong_query_words:
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

                # ── SEPARATE COST AND INFO — they never compete ────────────────
                # Cost entries have their own top-K, info entries have their own.
                # This prevents long prose info entries from crowding out cost entries.
                cost_chunks = [p for p in prioritized if p.get("category") == "cost"]
                info_chunks = [p for p in prioritized if p.get("category") == "info"]
                rx_chunks = [p for p in prioritized if p.get("category") == "rx"]
                other_chunks = [
                    p
                    for p in prioritized
                    if p.get("category") not in ("cost", "info", "rx")
                ]

                # Cost: take top 10 independently
                cost_selected = cost_chunks[:10]
                # Info: take top 3 independently (prose is verbose, keep focused)
                info_selected = info_chunks[:3]
                # Rx: take top 15 — drug formularies have many dosage variants
                # of the same drug name (e.g. metformin has 10+ forms/combinations)
                # A low limit here would hide the plain/common form of a drug
                rx_selected = rx_chunks[:15]
                # Other (qa, excluded): keep as before
                other_selected = other_chunks[:5]

                # Recombine — cost always first so it's never trimmed out
                prioritized = (
                    cost_selected + other_selected + rx_selected + info_selected
                )

                # ============================================================
                # 🔥 LIST QUERY DETECTION
                # ============================================================
                # is_list_query: only true when member explicitly asks for a list
                # "cost" alone should NOT trigger returning 10 items —
                # that caused vague cost queries to return too many mixed results
                # "show me X" means "show me info about X" — NOT a list query.
                # A list query requires an explicit enumeration signal ("all", "list",
                # "every") alongside "show me" / "give me", e.g. "show me all benefits".
                is_list_query = (
                    any(w in query_lower for w in ["all", "list", "which"])
                    or (
                        "show me" in query_lower
                        and any(w in query_lower for w in ["all", "list", "every"])
                    )
                    or "what are" in query_lower
                    or (
                        "give me" in query_lower
                        and any(w in query_lower for w in ["all", "list", "every"])
                    )
                )

                # ============================================================
                # 🔥 SAME EVENT GROUPING
                # ============================================================
                # def extract_event(p):
                #     c = p.get("content", {})
                #     if isinstance(c, dict):
                #         return normalize_text(c.get("event", p.get("topic", "")))
                #     return normalize_text(p.get("topic", ""))

                # event_set = set(extract_event(p) for p in prioritized)
                def extract_event(p):
                    c = p.get("content", {})

                    if isinstance(c, dict):
                        event = normalize_text(c.get("event", ""))
                        service = normalize_text(c.get("service", ""))

                        # 🔥 combine event + service
                        return f"{event}_{service}"

                    return normalize_text(p.get("topic", ""))

                event_set = set(extract_event(p) for p in prioritized)

                # ============================================================
                # 🔥 FINAL SELECTION
                # ============================================================
                MULTI_ROW_TOPICS = {
                    "deductible",
                    "out-of-pocket",
                    "oop",
                    "imaging",
                    "diagnostic",
                    "hospital",
                    "rehabilitation",
                    "prescription drug",  # Rx dual-index needs ALL tier rows
                    # (generic, brand, specialty, non-preferred)
                    # not just the top 3
                }

                if topic in MULTI_ROW_TOPICS:
                    selected_chunks = prioritized[:10]

                elif len(prioritized) == 1:
                    selected_chunks = prioritized[:1]

                elif len(event_set) == 1:
                    selected_chunks = prioritized

                elif is_list_query:
                    selected_chunks = prioritized[:10]

                else:
                    selected_chunks = prioritized[:3]

                if not selected_chunks and prioritized:
                    selected_chunks = prioritized[:1]

                # Always re-add info entries even if sliced off by [:3] or [:10].
                # Cost entries fill the slice first (they're at the start of prioritized),
                # so info entries at the end get cut. This ensures they're always included.
                selected_ids = set(id(p) for p in selected_chunks)
                for p in info_selected:
                    if id(p) not in selected_ids:
                        selected_chunks.append(p)

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

                    # rx chunks are handled like cost chunks but with drug-specific fields
                    if chunk_type not in {"qa", "cost", "excluded", "info", "rx"}:
                        continue

                    if chunk_type not in sectioned_context:
                        sectioned_context[chunk_type] = []

                    content = selected.get("content", {})

                    if not isinstance(content, dict):
                        continue

                    if chunk_type == "rx":
                        structured = {
                            "drug_name": content.get(
                                "drug_name", selected.get("topic", "")
                            ),
                            "tier": content.get("tier", ""),
                            "tier_label": content.get("tier_label", ""),
                            "requirements": content.get("requirements", ""),
                            "requirements_text": content.get("requirements_text", ""),
                            "drug_category": content.get("drug_category", ""),
                            "drug_subcategory": content.get("drug_subcategory", ""),
                            "page_number": selected.get("page_number", 0),
                        }

                    elif chunk_type == "cost":
                        structured = {
                            "event": content.get("event", selected.get("topic", "")),
                            "service": content.get(
                                "service", selected.get("topic", "")
                            ),
                            "in_network": content.get("in_network") or "Data Not Found",
                            "out_of_network": content.get("out_of_network")
                            or "Data Not Found",
                            "notes": content.get("limitations") or "Data Not Found",
                            "page_number": selected.get("page_number", 0),
                        }

                    elif chunk_type == "qa":
                        if not content.get("answer") and not content.get("explanation"):
                            continue

                        structured = {
                            "question": content.get("question", ""),
                            "answer": content.get("answer") or "Data Not Found",
                            "explanation": content.get("explanation")
                            or "Data Not Found",
                        }

                    elif chunk_type == "info":
                        # Info entries carry prose coverage details.
                        # Truncate to 2500 chars to avoid filling context window
                        # while still showing full coverage for most real
                        # explanations (median=577, 90th percentile=2215 chars).
                        # Original 1000-char limit was too aggressive — it was
                        # truncating important coverage details like TMJ limits
                        # ($1,000/$5,000 max) and exclusion lists mid-sentence.
                        raw_info = (
                            content.get("information")
                            or content.get("limitations")
                            or "Data Not Found"
                        )
                        if len(raw_info) > 2500:
                            raw_info = raw_info[:2500].rsplit(" ", 1)[0] + "..."
                        structured = {
                            "event": content.get("event", selected.get("topic", "")),
                            "service": content.get("service", "Coverage Information"),
                            "information": raw_info,
                            "page_number": selected.get("page_number", 0),
                        }

                    else:
                        structured = content

                    dedup_key = json.dumps(structured, sort_keys=True)

                    if dedup_key in sectioned_context["_seen_keys"]:
                        continue

                    sectioned_context["_seen_keys"].add(dedup_key)
                    sectioned_context[chunk_type].append(structured)

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
def query_insurance_benefits(
    query,
    topics,
    category,
    keywords,
    member_info: str = "{}",
    include_requirements_text: bool = False,
    include_limitations_text: bool = False,
):
    """
    RETRIEVAL TOOL
    """
    logger.info(f"[TOOL CALL] query_insurance_benefits: {query}")
    topics_tuple = tuple(topics) if isinstance(topics, list) else topics
    keywords_tuple = tuple(keywords) if isinstance(keywords, list) else keywords
    result = get_plan_data_from_disk(
        query,
        topics_tuple,
        category,
        keywords_tuple,
        member_info,
        include_requirements_text=include_requirements_text,
        include_limitations_text=include_limitations_text,
    )
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

# # ============================================================Previously working code==============================================
# # import json
# # import os
# # import re
# # import sqlite3
# # import logging
# # from functools import lru_cache
# # from fastmcp import FastMCP
# # from dotenv import load_dotenv

# # # ============================================================
# # # INIT
# # # ============================================================

# # load_dotenv()

# # BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# # DB_PATH = os.path.join(BASE_DIR, "indexers", "p_insurance_index.db")

# # mcp = FastMCP("Insurance-Secure-RAG")

# # logging.basicConfig(
# #     format="%(asctime)s - %(levelname)s - %(message)s",
# #     level=logging.INFO,
# # )
# # logger = logging.getLogger(__name__)

# # # ============================================================
# # # QUERY PROCESSING
# # # ============================================================

# # STOPWORDS = {"the", "me", "about", "tell", "what", "is", "a", "an", "to", "of"}


# # def clean_content(text):
# #     if not text:
# #         return ""

# #     # remove useless labels
# #     text = text.replace("Category:", "")
# #     text = text.replace("Question:", "")
# #     text = text.replace("Answer:", "")

# #     # normalize spacing
# #     text = re.sub(r"\n\s*\n", "\n", text)

# #     return text.strip()


# # @lru_cache(maxsize=128)
# # def get_plan_data_from_disk(query, topics, category, keywords, member_info: str = "{}"):
# #     """
# #     INTERNAL HELPER: Reads JSON index and returns best matching chunks.
# #     Uses topic (primary) + query (secondary) with scoring.
# #     member_info: JSON string from /member-info endpoint.
# #     Must contain 'plans' dict with category keys and 'year' at top level.
# #     """

# #     print(
# #         f"[*] SURGICAL FETCH: topics={topics} | query={query} | category={category} | keywords={keywords}"
# #     )

# #     try:
# #         # Use member_info from request if provided
# #         parsed_member_info = {}
# #         plan_for_category = {}
# #         try:
# #             parsed_member_info = (
# #                 json.loads(member_info) if member_info and member_info != "{}" else {}
# #             )
# #             plans = parsed_member_info.get("plans", {})
# #             plan_for_category = plans.get(category, {})
# #         except (json.JSONDecodeError, AttributeError):
# #             plan_for_category = {}

# #         if not plan_for_category:
# #             return f"ERROR: No member plan info available for category: {category}"

# #         member_plan_info = {
# #             **plan_for_category,
# #             "plan_category": category,
# #             "year": parsed_member_info.get("year", "2026"),
# #         }
# #         print(f"[*] USING MEMBER INFO FROM REQUEST for {category.upper()}")

# #         print(f"[*] DB PATH : {DB_PATH} ")

# #         with sqlite3.connect(DB_PATH) as conn:
# #             cursor = conn.cursor()

# #             # We use a base query and build filters carefully
# #             sql = "SELECT sub_index_path FROM master_index WHERE year = ? AND plan_category = ?"
# #             params = [member_plan_info["year"], member_plan_info["plan_category"]]

# #             # List of fields to filter on
# #             # Format: (DB_COLUMN_NAME, DICTIONARY_KEY)
# #             filters = [
# #                 ("plan", "plan"),
# #                 ("group_number", "group_number"),
# #                 ("plan_type", "plan_type"),
# #                 ("plan_tier", "plan_tier"),
# #                 ("product_line", "product_line"),
# #                 ("variant", "variant"),
# #                 ("network", "network"),
# #             ]

# #             for db_col, dict_key in filters:
# #                 val = str(member_plan_info.get(dict_key, "")).strip()

# #                 # If value is empty or the string "NULL", we check for empty OR null in DB
# #                 if val == "" or val.upper() == "NULL":
# #                     sql += (
# #                         f" AND ({db_col} IS NULL OR {db_col} = '' OR {db_col} = 'NULL')"
# #                     )
# #                 else:
# #                     # Use exact match for stability, or LIKE if you prefer partial
# #                     sql += f" AND {db_col} LIKE ?"
# #                     params.append(f"%{val}%")

# #             print(f"[*] FINAL QUERY: {sql}")
# #             print(f"[*] PARAMS: {params}")

# #             cursor.execute(sql, params)
# #             rows = cursor.fetchall()
# #             print(f"[*] ROWS RETURNED: {rows}")

# #         if not rows:
# #             return f"ERROR: Plan not found for {member_plan_info['plan_category']} - {member_plan_info['variant']}"

# #         # Normalize inputs

# #         query = (query or "").lower()
# #         if isinstance(topics, list):
# #             print("topics are type of list")
# #         else:
# #             print("topics are not type of list")

# #         print(f"[*] RAW TOPICS: {topics}")

# #         sectioned_context = {}  # 🔥 group by type

# #         # ------------------------------------------------------------
# #         # 🔧 🔁 MAIN LOOP HELPERS
# #         # ------------------------------------------------------------
# #         def normalize_text(text):
# #             return re.sub(r"[^\w\s]", " ", str(text).lower())

# #         def tokenize(text):
# #             return [w for w in normalize_text(text).split() if len(w) > 2]

# #         def soft_match(word, text):
# #             if re.search(r"\b" + re.escape(word) + r"\b", text):
# #                 return True
# #             if word.endswith("s"):
# #                 if re.search(r"\b" + re.escape(word[:-1]) + r"\b", text):
# #                     return True
# #             if re.search(r"\b" + re.escape(word + "s") + r"\b", text):
# #                 return True
# #             return False

# #         WEAK_WORDS = {
# #             "treatment",
# #             "service",
# #             "services",
# #             "care",
# #             "visit",
# #             "visits",
# #             "cost",
# #             "procedure",
# #             "therapy",
# #             "exam",
# #             "test",
# #             # Location/setting words — describe WHERE a service is rendered,
# #             # not WHAT the benefit is.  Must stay in sync with client.py WEAK_WORDS.
# #             "office",
# #             "clinic",
# #             "clinics",
# #             "setting",
# #             "settings",
# #             "facility",
# #             "facilities",
# #         }

# #         # ============================================================
# #         # 🔁 MAIN LOOP
# #         # ============================================================
# #         for (sub_index_path,) in rows:
# #             print(f"[*] SEARCHING IN FILE : {sub_index_path}")

# #             if not os.path.exists(sub_index_path):
# #                 continue

# #             with open(sub_index_path, "r", encoding="utf-8") as f:
# #                 sub_index = json.load(f)

# #             print(f"[*] FILE SUCCESSFULLY FOUND AND PARSED : {sub_index_path}")

# #             # ------------------------------------------------------------
# #             # 🔥 NORMALIZATION
# #             # ------------------------------------------------------------
# #             if isinstance(topics, str):
# #                 topics = [topics]
# #             elif isinstance(topics, tuple):
# #                 topics = list(topics)

# #             if not topics:
# #                 print("[*] NO TOPIC FOUND → USING GLOBAL SEARCH")
# #                 topics = ["__all__"]

# #             query_lower = query.lower()
# #             query_clean = normalize_text(query)
# #             query_words = tokenize(query_clean)
# #             keywords = [k.lower().strip() for k in (keywords or []) if k]

# #             print(f"[*] QUERY WORDS: {query_words}")
# #             print(f"[*] KEYWORDS: {keywords}")
# #             print(f"[*] TOPICS: {topics}")

# #             # ------------------------------------------------------------
# #             # 🔥 SPLIT KEYWORDS
# #             # ------------------------------------------------------------
# #             strong_keywords = []
# #             weak_keywords = []

# #             for kw in keywords:
# #                 parts = kw.split()
# #                 if any(p in WEAK_WORDS for p in parts):
# #                     weak_keywords.append(kw)
# #                 else:
# #                     strong_keywords.append(kw)

# #             # 🔥 NEW: split query words (SAFE)
# #             strong_query_words = [w for w in query_words if w not in WEAK_WORDS]

# #             # 🔥 NEW: phrase for exact match
# #             query_phrase = " ".join(query_words)

# #             # ============================================================
# #             # 🔍 PROCESS EACH TOPIC
# #             # ============================================================
# #             for topic in topics:
# #                 print(f"\n[*] PROCESSING TOPIC: {topic}")

# #                 topic_clean = normalize_text(topic)
# #                 scored_chunks = []

# #                 for p in sub_index:
# #                     chunk_topic = normalize_text(p.get("topic", ""))
# #                     chunk_keywords = " ".join(
# #                         [normalize_text(k) for k in p.get("keywords", [])]
# #                     )
# #                     content = normalize_text(p.get("content", ""))

# #                     full_text = f"{chunk_topic} {chunk_keywords} {content}"

# #                     score = 0
# #                     match_score = 0

# #                     # ====================================================
# #                     # 🔥 PHRASE MATCH (fixes "urgent care")
# #                     # ====================================================
# #                     if query_phrase and query_phrase in full_text:
# #                         score += 150
# #                         match_score += 2

# #                     # ====================================================
# #                     # 🔥 UNIFIED MATCH (UPDATED)
# #                     # ====================================================

# #                     # STRONG KEYWORDS → highest signal
# #                     for kw in strong_keywords:
# #                         if soft_match(kw, full_text):
# #                             match_score += 3

# #                     # ALL KEYWORDS
# #                     for kw in keywords:
# #                         if soft_match(kw, full_text):
# #                             match_score += 2

# #                     # 🔥 ONLY STRONG QUERY WORDS (FIX)
# #                     for w in strong_query_words:
# #                         if soft_match(w, full_text):
# #                             match_score += 1

# #                     # ❌ ONLY skip if NOTHING matches
# #                     if match_score == 0:
# #                         continue

# #                     # ====================================================
# #                     # 🔥 SCORING
# #                     # ====================================================

# #                     # ── INFO ENTRIES: score on event name only ─────────────
# #                     chunk_category = p.get("category", "")
# #                     if chunk_category == "info":
# #                         content_dict = p.get("content", {})
# #                         event_name = (
# #                             content_dict.get("event", "")
# #                             if isinstance(content_dict, dict)
# #                             else ""
# #                         )
# #                         event_lower = event_name.lower()
# #                         info_score = 0

# #                         # Direct phrase match — MULTI-WORD keywords only.
# #                         # "vision hardware" → matches "Vision Hardware" event directly.
# #                         # Single words ("medical", "contact") use word-count logic
# #                         # below to avoid false positives across many events.
# #                         #
# #                         # GUARD: skip keywords whose every word is a weak/setting term.
# #                         # Example: "office visits" is all-weak → must NOT score 400 against
# #                         # "Split Copay For Office Visits" for a "foot care" query.
# #                         # "vision hardware" has "vision"+"hardware" (both meaningful) → allowed.
# #                         _PHRASE_STOP = {
# #                             "and",
# #                             "or",
# #                             "in",
# #                             "an",
# #                             "the",
# #                             "a",
# #                             "for",
# #                             "of",
# #                         }
# #                         for kw in keywords:
# #                             kw_lower = kw.lower().strip()
# #                             if " " not in kw_lower:
# #                                 continue
# #                             kw_parts = [
# #                                 p
# #                                 for p in re.split(r"\W+", kw_lower)
# #                                 if p and len(p) > 1 and p not in _PHRASE_STOP
# #                             ]
# #                             if kw_parts and all(p in WEAK_WORDS for p in kw_parts):
# #                                 continue  # all-weak keyword — skip phrase match
# #                             if kw_lower in event_lower or event_lower in kw_lower:
# #                                 # Penalise "non-X" events when query is about X
# #                                 if event_lower.startswith(
# #                                     "non-"
# #                                 ) or event_lower.startswith("non "):
# #                                     info_score += 100
# #                                 # Boost exact event name match
# #                                 elif event_lower == kw_lower or kw_lower == event_lower:
# #                                     info_score += 600
# #                                 else:
# #                                     info_score += 400
# #                                 break

# #                         # Word-by-word match (single words or when no phrase matched)
# #                         if info_score == 0:
# #                             INFO_STOP = {
# #                                 "what",
# #                                 "which",
# #                                 "does",
# #                                 "how",
# #                                 "and",
# #                                 "for",
# #                                 "under",
# #                                 "about",
# #                                 "when",
# #                                 "where",
# #                                 "covered",
# #                                 "covers",
# #                                 "tell",
# #                                 "show",
# #                                 "give",
# #                                 "know",
# #                                 "get",
# #                                 "can",
# #                                 "will",
# #                                 "has",
# #                                 "also",
# #                                 "any",
# #                                 "all",
# #                                 "are",
# #                                 "the",
# #                                 "its",
# #                                 "is",
# #                                 "it",
# #                                 "my",
# #                                 "me",
# #                                 "do",
# #                                 "affect",
# #                                 "affects",
# #                                 "impact",
# #                                 "impacts",
# #                                 "happen",
# #                                 "happens",
# #                                 "work",
# #                                 "works",
# #                                 "related",
# #                                 "regarding",
# #                             }
# #                             info_words = [
# #                                 w
# #                                 for w in strong_query_words
# #                                 if w not in INFO_STOP and len(w) > 3
# #                             ]
# #                             if not info_words:
# #                                 info_words = strong_query_words

# #                             matched = [
# #                                 w for w in info_words if soft_match(w, event_lower)
# #                             ]
# #                             required = (
# #                                 min(2, len(info_words)) if len(info_words) > 1 else 1
# #                             )

# #                             # Single-word keyword matches against event name only.
# #                             # Deliberately NOT matching against chunk_keywords to avoid
# #                             # false positives where a keyword appears in content text
# #                             # but not in the event name.
# #                             # e.g. "emergency" appears in E-Visit Exclusions chunk keywords
# #                             # because the content mentions "Emergency consultations" —
# #                             # but the event is NOT about emergency room.
# #                             # Exception: short terms like "tmj" (len<=3) that are filtered
# #                             # from info_words still need chunk_keywords matching.
# #                             for kw in keywords:
# #                                 kw_lower = kw.lower().strip()
# #                                 if " " not in kw_lower:
# #                                     if soft_match(kw_lower, event_lower):
# #                                         matched.append(kw_lower)
# #                                     elif len(kw_lower) <= 3 and soft_match(
# #                                         kw_lower, chunk_keywords
# #                                     ):
# #                                         matched.append(kw_lower)

# #                             if len(matched) >= required:
# #                                 info_score = 200 * len(set(matched))

# #                         if info_score > 0:
# #                             scored_chunks.append((info_score, p))
# #                         continue  # skip general scoring for info entries

# #                     # 🧠 TOPIC BOOST (never filter)
# #                     if topic != "__all__":
# #                         if topic_clean in chunk_topic:
# #                             score += 60
# #                         elif topic_clean in full_text:
# #                             score += 30

# #                     # 🔥 STRONG KEYWORDS
# #                     for kw in strong_keywords:
# #                         if kw in full_text:
# #                             score += 120
# #                         elif soft_match(kw, chunk_topic):
# #                             score += 100
# #                         elif soft_match(kw, chunk_keywords):
# #                             score += 80
# #                         elif soft_match(kw, content):
# #                             score += 60

# #                     # 🔹 NORMAL KEYWORDS
# #                     for kw in keywords:
# #                         if soft_match(kw, full_text):
# #                             score += 40

# #                     # 🔥 ONLY STRONG QUERY WORDS (FIX)
# #                     for w in strong_query_words:
# #                         if soft_match(w, chunk_topic):
# #                             score += 20
# #                         elif soft_match(w, content):
# #                             score += 10

# #                     if score > 0:
# #                         scored_chunks.append((score, p))

# #                 # ============================================================
# #                 # 🎯 SORT
# #                 # ============================================================
# #                 scored_chunks.sort(key=lambda x: x[0], reverse=True)
# #                 prioritized = [p for _, p in scored_chunks]

# #                 print(f"[*] TOTAL MATCHED: {len(prioritized)}")

# #                 # ── SEPARATE COST AND INFO — they never compete ────────────────
# #                 # Cost entries have their own top-K, info entries have their own.
# #                 # This prevents long prose info entries from crowding out cost entries.
# #                 cost_chunks = [p for p in prioritized if p.get("category") == "cost"]
# #                 info_chunks = [p for p in prioritized if p.get("category") == "info"]
# #                 rx_chunks = [p for p in prioritized if p.get("category") == "rx"]
# #                 other_chunks = [
# #                     p
# #                     for p in prioritized
# #                     if p.get("category") not in ("cost", "info", "rx")
# #                 ]

# #                 # Cost: take top 10 independently
# #                 cost_selected = cost_chunks[:10]
# #                 # Info: take top 3 independently (prose is verbose, keep focused)
# #                 info_selected = info_chunks[:3]
# #                 # Rx: take top 15 — drug formularies have many dosage variants
# #                 # of the same drug name (e.g. metformin has 10+ forms/combinations)
# #                 # A low limit here would hide the plain/common form of a drug
# #                 rx_selected = rx_chunks[:15]
# #                 # Other (qa, excluded): keep as before
# #                 other_selected = other_chunks[:5]

# #                 # Recombine — cost always first so it's never trimmed out
# #                 prioritized = (
# #                     cost_selected + other_selected + rx_selected + info_selected
# #                 )

# #                 # ============================================================
# #                 # 🔥 LIST QUERY DETECTION
# #                 # ============================================================
# #                 # is_list_query: only true when member explicitly asks for a list
# #                 # "cost" alone should NOT trigger returning 10 items —
# #                 # that caused vague cost queries to return too many mixed results
# #                 # "show me X" means "show me info about X" — NOT a list query.
# #                 # A list query requires an explicit enumeration signal ("all", "list",
# #                 # "every") alongside "show me" / "give me", e.g. "show me all benefits".
# #                 is_list_query = (
# #                     any(w in query_lower for w in ["all", "list", "which"])
# #                     or (
# #                         "show me" in query_lower
# #                         and any(w in query_lower for w in ["all", "list", "every"])
# #                     )
# #                     or "what are" in query_lower
# #                     or (
# #                         "give me" in query_lower
# #                         and any(w in query_lower for w in ["all", "list", "every"])
# #                     )
# #                 )

# #                 # ============================================================
# #                 # 🔥 SAME EVENT GROUPING
# #                 # ============================================================
# #                 # def extract_event(p):
# #                 #     c = p.get("content", {})
# #                 #     if isinstance(c, dict):
# #                 #         return normalize_text(c.get("event", p.get("topic", "")))
# #                 #     return normalize_text(p.get("topic", ""))

# #                 # event_set = set(extract_event(p) for p in prioritized)
# #                 def extract_event(p):
# #                     c = p.get("content", {})

# #                     if isinstance(c, dict):
# #                         event = normalize_text(c.get("event", ""))
# #                         service = normalize_text(c.get("service", ""))

# #                         # 🔥 combine event + service
# #                         return f"{event}_{service}"

# #                     return normalize_text(p.get("topic", ""))

# #                 event_set = set(extract_event(p) for p in prioritized)

# #                 # ============================================================
# #                 # 🔥 FINAL SELECTION
# #                 # ============================================================
# #                 MULTI_ROW_TOPICS = {
# #                     "deductible",
# #                     "out-of-pocket",
# #                     "oop",
# #                     "imaging",
# #                     "diagnostic",
# #                     "hospital",
# #                     "rehabilitation",
# #                 }

# #                 if topic in MULTI_ROW_TOPICS:
# #                     selected_chunks = prioritized[:10]

# #                 elif len(prioritized) == 1:
# #                     selected_chunks = prioritized[:1]

# #                 elif len(event_set) == 1:
# #                     selected_chunks = prioritized

# #                 elif is_list_query:
# #                     selected_chunks = prioritized[:10]

# #                 else:
# #                     selected_chunks = prioritized[:3]

# #                 if not selected_chunks and prioritized:
# #                     selected_chunks = prioritized[:1]

# #                 # Always re-add info entries even if sliced off by [:3] or [:10].
# #                 # Cost entries fill the slice first (they're at the start of prioritized),
# #                 # so info entries at the end get cut. This ensures they're always included.
# #                 selected_ids = set(id(p) for p in selected_chunks)
# #                 for p in info_selected:
# #                     if id(p) not in selected_ids:
# #                         selected_chunks.append(p)

# #                 print(f"[*] FINAL SELECTED COUNT: {len(selected_chunks)}")

# #                 # ============================================================
# #                 # 🔥 DEDUP
# #                 # ============================================================
# #                 seen = set()
# #                 deduped_selected = []

# #                 for c in selected_chunks:
# #                     key = (
# #                         c.get("category"),
# #                         c.get("topic"),
# #                         json.dumps(c.get("content", {}), sort_keys=True),
# #                     )
# #                     if key in seen:
# #                         continue
# #                     seen.add(key)
# #                     deduped_selected.append(c)

# #                 selected_chunks = deduped_selected

# #                 # ============================================================
# #                 # 🧾 GROUP BY SECTION
# #                 # ============================================================
# #                 if "_seen_keys" not in sectioned_context:
# #                     sectioned_context["_seen_keys"] = set()

# #                 for selected in selected_chunks:
# #                     if not isinstance(selected, dict):
# #                         continue

# #                     chunk_type = selected.get("category", "").lower().strip()

# #                     # rx chunks are handled like cost chunks but with drug-specific fields
# #                     if chunk_type not in {"qa", "cost", "excluded", "info", "rx"}:
# #                         continue

# #                     if chunk_type not in sectioned_context:
# #                         sectioned_context[chunk_type] = []

# #                     content = selected.get("content", {})

# #                     if not isinstance(content, dict):
# #                         continue

# #                     if chunk_type == "rx":
# #                         structured = {
# #                             "drug_name": content.get(
# #                                 "drug_name", selected.get("topic", "")
# #                             ),
# #                             "tier": content.get("tier", ""),
# #                             "tier_label": content.get("tier_label", ""),
# #                             "requirements": content.get("requirements", ""),
# #                             "requirements_text": content.get("requirements_text", ""),
# #                             "drug_category": content.get("drug_category", ""),
# #                             "drug_subcategory": content.get("drug_subcategory", ""),
# #                             "page_number": selected.get("page_number", 0),
# #                         }

# #                     elif chunk_type == "cost":
# #                         structured = {
# #                             "event": content.get("event", selected.get("topic", "")),
# #                             "service": content.get(
# #                                 "service", selected.get("topic", "")
# #                             ),
# #                             "in_network": content.get("in_network") or "Data Not Found",
# #                             "out_of_network": content.get("out_of_network")
# #                             or "Data Not Found",
# #                             "notes": content.get("limitations") or "Data Not Found",
# #                             "page_number": selected.get("page_number", 0),
# #                         }

# #                     elif chunk_type == "qa":
# #                         if not content.get("answer") and not content.get("explanation"):
# #                             continue

# #                         structured = {
# #                             "question": content.get("question", ""),
# #                             "answer": content.get("answer") or "Data Not Found",
# #                             "explanation": content.get("explanation")
# #                             or "Data Not Found",
# #                         }

# #                     elif chunk_type == "info":
# #                         # Info entries carry prose coverage details.
# #                         # Truncate to 1000 chars so they don't fill the context trim limit.
# #                         raw_info = (
# #                             content.get("information")
# #                             or content.get("limitations")
# #                             or "Data Not Found"
# #                         )
# #                         if len(raw_info) > 1000:
# #                             raw_info = raw_info[:1000].rsplit(" ", 1)[0] + "..."
# #                         structured = {
# #                             "event": content.get("event", selected.get("topic", "")),
# #                             "service": content.get("service", "Coverage Information"),
# #                             "information": raw_info,
# #                             "page_number": selected.get("page_number", 0),
# #                         }

# #                     else:
# #                         structured = content

# #                     dedup_key = json.dumps(structured, sort_keys=True)

# #                     if dedup_key in sectioned_context["_seen_keys"]:
# #                         continue

# #                     sectioned_context["_seen_keys"].add(dedup_key)
# #                     sectioned_context[chunk_type].append(structured)

# #         # ============================================================
# #         # 🧠 BUILD FINAL CONTEXT (UNCHANGED ✅)
# #         # ============================================================
# #         final_context = ""

# #         for section, chunks in sectioned_context.items():
# #             if section.startswith("_"):
# #                 continue

# #             print(f"[*] BUILDING SECTION: {section}")

# #             final_context += f"\n\n### SECTION: {section.upper()}\n\n"

# #             for i, c in enumerate(chunks, 1):
# #                 final_context += f"Item {i}:\n"
# #                 final_context += json.dumps(c, indent=2)
# #                 final_context += "\n\n"

# #         return final_context.strip()

# #     except Exception as e:
# #         return f"ERROR: {str(e)}"


# # # ============================================================
# # # MCP TOOL
# # # ============================================================


# # @mcp.tool()
# # def query_insurance_benefits(
# #     query, topics, category, keywords, member_info: str = "{}"
# # ):
# #     """
# #     RETRIEVAL TOOL
# #     """
# #     logger.info(f"[TOOL CALL] query_insurance_benefits: {query}")
# #     topics_tuple = tuple(topics) if isinstance(topics, list) else topics
# #     keywords_tuple = tuple(keywords) if isinstance(keywords, list) else keywords
# #     result = get_plan_data_from_disk(
# #         query, topics_tuple, category, keywords_tuple, member_info
# #     )
# #     print(f"[*] RESULT RETURNED FROM SERVER : {result}")

# #     if result is None:
# #         logger.warning("[!] No FTS result, using fallback")
# #         # return fallback_to_summary(query, "medical")

# #     return str(result)  # 🔥 ALWAYS STRING


# # # ============================================================
# # # SERVER START
# # # ============================================================

# # if __name__ == "__main__":
# #     logger.info("Starting FastMCP server...")
# #     mcp.run(transport="stdio")
