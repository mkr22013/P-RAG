import re
import json
import os

from config import settings
from utility.llm import llm_chat, llm_generate, llm_chat_with_tools
from utility.utils import flatten_message_content, smart_match, RX_NOISE_WORDS
from utility.category import (
    detect_category,
    detect_category_rule_based,
    detect_category_from_history,
    extract_user_queries,
    is_conversational,
    is_drug_name_query,
    is_drug_match,
    correct_drug_spelling,
)
from utility.topic_resolver import resolve_insurance_topic, NOISE_WORDS
from utility.response_builder import (
    build_cost_table,
    build_info_response,
    build_rx_response,
    generate_ironclad_instruction,
)
from utility.prompts import (
    TOPIC_EXTRACTION_PROMPT,
    GUIDANCE_NO_CATEGORY,
    GUIDANCE_CONVERSATIONAL,
    GUIDANCE_MEDICAL_VAGUE,
    GUIDANCE_DENTAL_VAGUE,
    GUIDANCE_VISION_VAGUE,
)

from infrastructure.cache import get_query_result, set_query_result

last_type_global = None


def generate_summary(
    query: str,
    info_table: str = "",
    keywords: list | None = None,
) -> str:
    """
    Generate one direct sentence answering the user's question.
    Uses info_table for context. Skips if info is unrelated to query.
    Max 40 tokens. Silent fail: returns empty string if anything goes wrong.
    """
    try:
        content = info_table[:600] if info_table and info_table.strip() else ""
        if not content.strip():
            return ""

        # Relevance check — use query words + resolved topics/keywords
        # to verify the info content is about what was asked
        import re as _re
        from utility.utils import NOISE_WORDS

        # Use keywords only for relevance — they are the most specific extracted terms
        # Topics are too generic (e.g. "hospital", "network") and cause false positives
        # Query words are also too broad — keywords are already derived from the query
        check_terms = set()
        for k in keywords or []:
            check_terms.update(
                w
                for w in _re.sub(r"[^\w\s]", "", k.lower()).split()
                if len(w) > 3 and w not in NOISE_WORDS
            )

        content_lower = content.lower()
        # Word-boundary matching: "provide" won't match "provides"
        if check_terms and not any(
            _re.search(r"\b" + _re.escape(t) + r"\b", content_lower)
            for t in check_terms
        ):
            print(f"[*] SUMMARY SKIPPED — info not relevant (terms={check_terms})")
            return ""
        # Skip summary for value-seeking queries — the table already shows
        # exact amounts/percentages and the summary would be incomplete after stripping
        _ql = query.lower().strip()
        _value_seeking = any(
            _ql.startswith(p)
            for p in [
                "how much",
                "what is the cost",
                "what does it cost",
                "what is the copay",
                "what is the coinsurance",
                "what is the charge",
                "what is the fee",
                "what is the rate",
                "what is my copay",
                "what is my coinsurance",
                "what is my deductible",
            ]
        )
        if _value_seeking:
            print(
                f"[*] SUMMARY SKIPPED — value-seeking query, table is self-explanatory"
            )
            return ""

        prompt = (
            f"You are an insurance assistant. Write ONE plain sentence summarising the benefit below.\n"
            f"- State the fact directly. Do NOT start with Yes, No, or any affirmation.\n"
            f"- Example: 'Allergy testing and treatment is covered when provided by a certified allergy specialist.'\n"
            f"- Example: 'TMJ care is covered under your plan subject to applicable cost-sharing.'\n"
            f"- Do NOT mention any dollar amounts, copays, percentages or deductibles.\n"
            f"- No extra text, no preamble.\n"
            f"Query: {query}\n"
            f"Benefits data:\n{content}"
        )
        response_text = llm_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.0,
        )
        summary = response_text.strip()

        # Post-process: strip any leaked cost figures the model included anyway
        import re as _re

        summary = _re.sub(r"\$[\d,]+(?:\.\d+)?", "", summary)  # $25, $1,000
        summary = _re.sub(r"\d+%", "", summary)  # 20%, 10%
        summary = _re.sub(r"\s{2,}", " ", summary).strip()  # clean extra spaces
        if summary and not summary.endswith((".", "!", "?")):
            summary += "."
        print(f"[*] SUMMARY GENERATED: {summary[:100]}")
        return summary
    except Exception as e:
        print(f"[*] SUMMARY FAILED: {e}")
        return ""


async def get_ai_response(
    query: str,
    history: list,
    member_info: dict | None = None,
    current_category: str = "",
):
    # 1. ACCESS GLOBALS
    global p_type_fast, p_tier_fast, last_type_global

    found_topics = []
    keywords = []

    # Categories that bypass LLM tool call — direct to tools.py
    # These have well-defined scoring and structured indices
    # Add new categories here ONLY when they have a proper indexer + scoring logic
    DIRECT_CATEGORIES = {"medical", "dental", "vision", "rx"}

    try:
        from insurance_mcp.tools import (
            get_plan_data_from_disk as query_insurance_benefits,
        )

        # --- 2. CONTEXT MERGING (MEMORY) ---
        # Limit to last 5 turns (10 messages) — avoids stale context polluting
        # topic resolution and keyword extraction for long conversations.
        recent_history = " ".join(
            [flatten_message_content(m["content"]) for m in history[-10:]]
        )
        query_lower = query.lower()

        # Clean words for surgical matching
        query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]

        # --- PASTED RESPONSE DETECTION ---
        # Detect when member pastes back our own drug list response.
        # Pattern: contains "ask me about any specific drug" or
        # starts with "here are the medications"
        _pasted_response = (
            "ask me about any specific drug" in query_lower
            or query_lower.strip().startswith("here are the medications")
            or query_lower.strip().startswith("here are the covered medications")
        )
        if _pasted_response:
            return {
                "answer": (
                    "It looks like you pasted the medication list. "
                    "Please type the name of **one specific drug** "
                    "to see its tier, cost and requirements.\n\n"
                    "For example: *metformin* or *ozempic*"
                ),
                "pages": [],
                "source": "",
            }

        print(f"[*] Query Words for Matching: {query_words}")
        print("[DEBUG] urgent match:", smart_match("urgent", query_words, query_lower))

        # --- CONVERSATIONAL INTENT CHECK ---
        # Short-circuit before any RAG pipeline if query is a greeting,
        # follow-up, or clearly non-benefit question.
        if is_conversational(query):
            print("[*] CONVERSATIONAL QUERY DETECTED → returning guidance")
            return GUIDANCE_CONVERSATIONAL

        # --- 3. TYPE DETECTION ---
        p_type = detect_category(query_words, query)
        print(f"[*] FINAL TYPE DETECTED : {p_type}")
        if not p_type:
            print("[*] TYPE NOT DETECTED IN QUERY - SEARCHING HISTORY")
            p_type = detect_category_from_history(history)

        if not p_type:
            print("[*] NO CATEGORY DETECTED → returning guidance")
            return GUIDANCE_NO_CATEGORY

        if last_type_global is not None and last_type_global != p_type:
            print("[*] CATEGORY CHANGED → SKIP HISTORY")
            skip_topic_history_check = True
        else:
            skip_topic_history_check = False

        # --- 4. UNIFIED TOPIC DETECTION ---
        resolved = resolve_insurance_topic(query_words, query_lower, p_type=p_type)

        found_topics = resolved.get("topics", [])
        keywords = resolved.get("keywords", [])

        print(f"[*] LENGTH OF DETECTED TOPIC : {len(found_topics)}")

        # ========================================================================
        # DENTAL OVERVIEW DETECTION — "what does my plan cover" / broad queries
        # Map to all class topics so server returns a comprehensive overview
        # instead of falling to LLM with nonsense topics like "pharmacy"
        # ========================================================================
        _OVERVIEW_TERMS = [
            "what does my plan cover",
            "what is covered",
            "what's covered",
            "overview",
            "summary",
            "all benefits",
            "all covered",
            "each type",
            "types of service",
            "types of coverage",
            "everything covered",
            "full coverage",
            "complete coverage",
            "services are covered",
            "services covered",
            "what services",
        ]
        _dental_words = ["dental", "tooth", "teeth", "gum", "oral", "dentist"]
        if (
            not found_topics
            and any(w in query_lower for w in _dental_words)
            and any(smart_match(w, query_words, query_lower) for w in _OVERVIEW_TERMS)
        ):
            found_topics = ["class i", "class ii", "class iii", "plan limits"]
            keywords = keywords + [
                "class i diagnostic and preventive services",
                "class ii basic services",
                "class iii major services",
                "plan limits",
                "deductible",
                "annual maximum",
            ]
            print(f"[*] DENTAL OVERVIEW QUERY → expanding to all class topics")

        # ========================================================================
        # IF STILL TOPIC IS BLANK THEN WE NEED TO TAKE LLM HELP TO IDENTIFY TOPIC
        # Skip this for rx — the rx block below builds drug-name keywords
        # directly from query_words, so the generic topic LLM call would be
        # wasted work that gets immediately discarded.
        # ========================================================================
        if not found_topics and p_type != "rx":
            print(f"[*] TURNING TO LLM TO FIND THE TOPIC FROM QUERY : {query}")
            topic_prompt = TOPIC_EXTRACTION_PROMPT

            llm_messages = [
                {"role": "system", "content": topic_prompt},
                {"role": "user", "content": f"User Query: {query_lower}"},
            ]

            # ============================================================
            # 🔥 LLM TOPIC + KEYWORD EXTRACTION (PRODUCTION SAFE)
            # ============================================================

            raw_content = llm_chat(
                messages=llm_messages,
                format="json",
                max_tokens=200,
            )
            print(f"[*] ROW CONTENT BY LLM AFTER TOPIC SEARCH : {raw_content}")

            match = re.search(r"\{.*\}", raw_content, re.DOTALL)

            # ============================================================
            # 🔧 HELPERS
            # ============================================================

            INVALID_TOPICS = {"medical", "dental", "vision"}
            INVALID_KEYWORDS = NOISE_WORDS

            def normalize_topic(t):
                return str(t).lower().strip()

            def word_count_topic(t):
                # treat "_" and "-" as separators but NOT destructive
                parts = re.split(r"[ _-]+", t)
                return len([p for p in parts if p])

            def canonicalize_topic(t):
                # unify format → prefer hyphen
                return t.replace("_", "-").strip()

            def normalize_keyword(k):
                return re.sub(r"[^\w\s-]", "", str(k).lower()).strip()

            # ============================================================
            # 🔍 PARSE RESPONSE
            # ============================================================

            if match:
                try:
                    json_str = match.group(0)
                    data = json.loads(json_str)

                    # -------------------------
                    # 🔹 RAW VALUES
                    # -------------------------
                    new_topics = data.get("topics", []) or []
                    raw_keywords = data.get("keywords", []) or []

                    print(f"[*] RAW TOPICS FROM LLM: {new_topics}")
                    print(f"[*] RAW KEYWORDS FROM LLM: {raw_keywords}")

                    # ====================================================
                    # 🔥 CLEAN KEYWORDS (ALWAYS KEEP)
                    # ====================================================
                    keywords = []
                    for k in raw_keywords:
                        clean_k = normalize_keyword(k)
                        if (
                            clean_k
                            and clean_k not in keywords
                            and clean_k not in INVALID_KEYWORDS
                        ):
                            keywords.append(clean_k)

                    print(f"[*] CLEANED KEYWORDS: {keywords}")

                    # ====================================================
                    # 🔥 CLEAN TOPICS (STRICT FILTER)
                    # ====================================================
                    cleaned_topics = []

                    if isinstance(new_topics, list):
                        for t in new_topics:
                            if not t:
                                continue

                            raw = normalize_topic(t)

                            # ❌ skip UNKNOWN
                            if raw == "unknown":
                                continue

                            # ❌ skip category leakage
                            if raw in INVALID_TOPICS:
                                print(f"[*] SKIPPING INVALID TOPIC: {raw}")
                                continue

                            # ❌ reject noisy topics (>2 words)
                            wc = word_count_topic(raw)
                            if wc > 2:
                                print(f"[*] REJECTING NOISY TOPIC ({wc} words): {raw}")
                                continue

                            # ❌ reject topics whose every word is a noise word
                            # e.g. "plan details", "coverage information", "plan overview"
                            topic_words = re.split(r"[ _-]+", raw)
                            if all(w in NOISE_WORDS for w in topic_words if w):
                                print(f"[*] REJECTING ALL-NOISE TOPIC: {raw}")
                                continue

                            # ✅ canonical form
                            clean_topic = canonicalize_topic(raw)

                            if clean_topic not in cleaned_topics:
                                cleaned_topics.append(clean_topic)

                    # ====================================================
                    # 🔥 FINAL MERGE INTO found_topics
                    # ====================================================
                    if cleaned_topics:
                        print(f"[*] FINAL CLEANED TOPICS: {cleaned_topics}")

                        for t in cleaned_topics:
                            if t not in found_topics:
                                found_topics.append(t)

                    else:
                        # print("[*] NO VALID TOPIC FROM LLM → USING KEYWORDS ONLY")
                        print("[*] NO VALID TOPIC FROM LLM → USING KEYWORDS AS TOPICS")

                        # 🔥 fallback: use keywords as retrieval topics
                        for kw in keywords:
                            if kw not in found_topics:
                                found_topics.append(kw)

                except json.JSONDecodeError as e:
                    print(f"[!] JSON PARSE ERROR: {e}")
            else:
                print(f"[!] No JSON block found in LLM response: {raw_content}")
        # ========================================================================

        if len(found_topics) == 0 and skip_topic_history_check == False:

            user_queries = extract_user_queries(history[-10:])

            for past_query in reversed(user_queries):
                print(f"[*] CHECKING PAST QUERY: {past_query}")

                history_word_list = [
                    re.sub(r"[^\w\s]", "", w) for w in past_query.split()
                ]

                # Stop at category boundary — rule-based only, no LLM.
                # If past query is clearly a different category → stop searching.
                # If ambiguous (returns None) → assume same category, continue.
                past_cat = detect_category_rule_based(history_word_list, past_query)
                if past_cat is not None and past_cat != p_type:
                    print(
                        f"[*] HISTORY BOUNDARY HIT ({past_cat} != {p_type}) → stopping"
                    )
                    break

                history_resolved = resolve_insurance_topic(
                    history_word_list, past_query
                )
                history_topics = history_resolved.get("topics", [])
                history_keywords = history_resolved.get("keywords", [])

                if history_topics:
                    print(f"[*] RECOVERED TOPIC FROM HISTORY: {history_topics}")
                    found_topics.extend(
                        t for t in history_topics if t not in found_topics
                    )
                    keywords = list(set(keywords + history_keywords))
                    break

        print(f"Final topic found from query : {found_topics}")
        last_type_global = p_type  # Now setting up the last type here as now we have category and topic and keywords

        # Page reference helpers — used at every return point below
        # member_info shape: {"year":..., "plans": {"medical": {...}, "dental": {...}}}
        _plan_info = (member_info.get("plans", {}) if member_info else {}).get(
            p_type, {}
        )
        _page_offset = _plan_info.get("page_offset", 0)
        _booklet_name = _plan_info.get("plan", "")

        def _add_offset(raw_pages):
            return sorted(
                p - _page_offset for p in raw_pages if p > 0 and p - _page_offset > 0
            )

        # Once we reach here it means topic did not available
        if len(found_topics) == 0 and len(keywords) == 0:
            print(f"[*] NO TOPIC FOUND FOR {p_type} → returning guidance")
            if p_type.lower() == "medical":
                return GUIDANCE_MEDICAL_VAGUE
            elif p_type.lower() == "dental":
                return GUIDANCE_DENTAL_VAGUE
            elif p_type.lower() == "vision":
                return GUIDANCE_VISION_VAGUE
            else:
                return GUIDANCE_NO_CATEGORY

        # 🔥 normalize topics
        topics_part = "_".join(sorted(filter(None, found_topics)))

        # 🔥 normalize keywords (only if present) — dedupe, lowercase, sort for consistent key
        # regardless of query word order
        if keywords:
            normalized_keywords = sorted(
                set(k.lower().strip() for k in keywords if k and len(k.strip()) > 2)
            )
            keywords_part = "_".join(normalized_keywords)
            cache_key = (
                f"{p_type}_{topics_part}_{keywords_part}"
                if keywords_part
                else f"{p_type}_{topics_part}"
            )
        else:
            cache_key = f"{p_type}_{topics_part}"

        print(f"[*] CACHE KEY: {cache_key}")

        cached_context = await get_query_result(cache_key)
        cache_hit = False
        clean_context = ""

        if cached_context:
            print(f"[*] CACHE HIT: {cache_key}")
            print(f"[*] CACHED CONTEXT: {cached_context}")
            clean_context = cached_context
            cache_hit = True

        # --- 9. THE REASONING LOOP (skipped on cache hit) ---
        if not cache_hit:
            messages = []

            # --- SYSTEM PROMPT ---
            system_prompt = {
                "role": "system",
                "content": (
                    "You are an AI assistant.\n"
                    "For every user question, you MUST call the tool "
                    "'query_insurance_benefits'.\n"
                    "Do NOT answer directly.\n"
                    "Return ONLY the tool call."
                ),
            }

            messages.append(system_prompt)

            # --- HISTORY ---
            # Pass last 4 turns (8 messages) to LLM — enough for conversational
            # context without bloating the prompt with stale exchanges.
            if history:
                for turn in history[-8:]:
                    messages.append(
                        {
                            "role": turn.get("role", "user"),
                            "content": flatten_message_content(turn.get("content", "")),
                        }
                    )

            messages.append({"role": "user", "content": query})

            # --- TOOL ---
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "query_insurance_benefits",
                        "description": (
                            "MANDATORY: Retrieve insurance benefit details using the user query and resolved topics. "
                            "You MUST call this tool before answering."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "The full user question",
                                },
                                "topics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "List of relevant topics like ['primary', 'urgent care', 'imaging']",
                                },
                                "category": {
                                    "type": "string",
                                    "description": "Category (medical / dental / sbc)",
                                },
                                "keywords": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "List of relevant keywords like ['immunization', 'mammogram']",
                                },
                            },
                            "required": ["query", "topics", "category"],
                        },
                    },
                }
            ]

            print(f"[*] QUERY: {query}")
            print(f"[*] TOPICS (fallback only): {found_topics}")

            # ============================================================
            # STEP 1 — RETRIEVE CHUNKS
            # Direct call for known categories — no LLM tool overhead
            # LLM tool orchestration reserved for future unknown categories
            # ============================================================
            tool_result = None

            if p_type == "rx":
                # ── Rx: dual-index query ──────────────────────────────────────

                # Extract drug name keywords — skip general terms AND
                # process/action words that dilute scoring (need, require, does,
                # prior, authorization etc.) so the actual drug name dominates
                # the topic/keyword match instead of being diluted by these.
                # RX_NOISE_WORDS imported from utility.utils — single shared
                # source used by BOTH client.py (here) and tools.py (chunk
                # scoring), so the two layers can never silently diverge again.
                rx_keywords = tuple(
                    w for w in query_words if len(w) > 3 and w not in RX_NOISE_WORDS
                )
                if rx_keywords:
                    rx_keywords = tuple(correct_drug_spelling(w) for w in rx_keywords)
                    print(f"[*] RX KEYWORDS AFTER CORRECTION: {rx_keywords}")

                print(f"[DIRECT CALL]: rx dual-index | keywords={rx_keywords} | rx")
                try:
                    # Step 1 — drug coverage from Rx formulary index
                    #
                    # When rx_keywords is empty, the query has no drug name —
                    # but it may still have a meaningful CONCEPT word (e.g.
                    # "preventive", "ACA") that should drive the search, just
                    # via requirement-matching instead of name-matching.
                    # Only fall back to the generic formulary search when
                    # there's truly no signal at all (e.g. "what is formulary
                    # drugs?" — nothing left to search by except the concept
                    # of formulary itself).
                    _PURE_NOISE_WORDS = {
                        "want",
                        "know",
                        "about",
                        "tell",
                        "please",
                        "could",
                        "would",
                        "like",
                        "they",
                        "them",
                        "have",
                        "give",
                        "more",
                        "information",
                        "details",
                        "find",
                        "looking",
                        "what",
                        "drug",
                        "drugs",
                        "formulary",
                        "list",
                        "covered",
                        "cost",
                        "much",
                        "show",
                        "does",
                        "plan",
                        "cover",
                        "prescription",
                        "medication",
                    }
                    concept_terms = tuple(
                        w
                        for w in query_words
                        if len(w) > 3 and w not in _PURE_NOISE_WORDS
                    )

                    # ── Condition-based query resolution ──────────────────
                    # If query contains a condition term (e.g. "asthma",
                    # "diabetes") but no specific drug name, resolve
                    # condition → drug names first.
                    condition_drugs = []
                    if not is_drug_name_query(query_words):
                        try:
                            from utility.condition_resolver import (
                                resolve_query_to_drugs,
                            )

                            condition_drugs = resolve_query_to_drugs(
                                query, use_llm_fallback=False
                            )
                            if condition_drugs:
                                print(
                                    f"[*] CONDITION RESOLVED: {len(condition_drugs)} drugs found"
                                )
                        except Exception as _ce:
                            print(f"[!] Condition resolution failed: {_ce}")
                            condition_drugs = []

                    if condition_drugs:
                        if len(condition_drugs) > 10:
                            # Too many drugs to show full table — return name list
                            # and ask member to pick a specific drug.
                            # Works for both UI and API clients.
                            from utility.condition_resolver import (
                                find_canonical_condition,
                                extract_condition_terms,
                            )

                            candidates = extract_condition_terms(query)
                            canonical = next(
                                (
                                    find_canonical_condition(t)
                                    for t in candidates
                                    if find_canonical_condition(t)
                                ),
                                None,
                            )
                            label = canonical.title() if canonical else "your condition"

                            # Split into covered vs not-covered using drug_words
                            # entry_type — devices/vaccines excluded already
                            # We don't know coverage status without index lookup
                            # so just list all as candidates
                            drug_list = ", ".join(sorted(condition_drugs))

                            rx_plan_info = (
                                member_info.get("plans", {}).get("rx", {})
                                if member_info
                                else {}
                            )
                            rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
                            rx_variant = rx_plan_info.get("variant", "")
                            source = (
                                f"{rx_plan_name} ({rx_variant})"
                                if rx_variant
                                else rx_plan_name
                            )

                            answer = (
                                f"Here are the medications that may be available for "
                                f"**{label}** on your formulary:\n\n"
                                f"**{drug_list}**\n\n"
                                f"Ask me about any specific drug to see its tier, "
                                f"cost and requirements."
                            )
                            return {
                                "answer": answer,
                                "pages": [],
                                "source": source,
                            }

                        # <= 10 drugs — use drug names as topics for full table
                        search_terms = tuple(condition_drugs)
                        condition_keywords = tuple(condition_drugs)
                        query_has_real_drug_name = True
                    else:
                        query_has_real_drug_name = is_drug_name_query(query_words)
                        search_terms = (
                            rx_keywords
                            or concept_terms
                            or ("formulary", "drug", "list", "tier", "coverage")
                        )
                        condition_keywords = search_terms

                    rx_result = query_insurance_benefits(
                        query=query,
                        topics=search_terms,
                        category="rx",
                        keywords=condition_keywords,
                        member_info=json.dumps(member_info) if member_info else "{}",
                        include_requirements_text=not query_has_real_drug_name,
                    )
                    print(
                        f"[*] RX INDEX RESULT: {rx_result[:200] if rx_result else 'empty'}"
                    )

                    # Filter rx chunks to only keep entries where drug name
                    # matches the search keyword — removes category noise.
                    # Uses fuzzy matching to handle spelling mistakes in drug names
                    # e.g. "metfromin" matches "metformin", "flucanazole" matches "fluconazole"
                    #
                    # IMPORTANT: only apply this filter when the query actually
                    # contains a real drug name word. Requirement/category-style
                    # queries like "what preventive drugs do I have" have no drug
                    # name in them — applying the filter there would incorrectly
                    # discard correctly-scored results (e.g. ACA-flagged drugs
                    # that tools.py found via requirement matching, not name match).
                    used_generic_fallback = not rx_keywords and not concept_terms

                    if used_generic_fallback:
                        # No specific drug name AND no meaningful concept word —
                        # this is a purely conceptual question (e.g. "what is
                        # formulary drugs?"). The RX section (if any) contains
                        # arbitrary drugs from the generic fallback search, not
                        # anything the member actually asked about — strip it
                        # so only INFO content remains for LLM synthesis.
                        import re as _re

                        rx_result = _re.sub(
                            r"### SECTION: RX.*?(?=### SECTION:|$)",
                            "",
                            rx_result or "",
                            flags=_re.DOTALL,
                        ).strip()
                        print("[*] NO RX KEYWORDS — stripped RX section, INFO only")

                    elif rx_result and condition_drugs:
                        # Filter results to only keep drugs in our condition list
                        import re as _re

                        condition_drugs_set = set(condition_drugs)
                        filtered_items = []
                        not_covered_names = []
                        for item_match in _re.finditer(
                            r"Item \d+:\s*\{[^{}]+\}", rx_result, _re.DOTALL
                        ):
                            item_text = item_match.group()
                            drug_name_match = _re.search(
                                r'"drug_name":\s*"([^"]+)"', item_text
                            )
                            if drug_name_match:
                                drug_name = drug_name_match.group(1).lower()
                                first_word = drug_name.split()[0] if drug_name else ""
                                if first_word in condition_drugs_set:
                                    if "Not on Formulary" in item_text:
                                        brand_name = (
                                            drug_name_match.group(1).split()[0].upper()
                                        )
                                        if brand_name not in not_covered_names:
                                            not_covered_names.append(brand_name)
                                    else:
                                        filtered_items.append(item_text)
                        if filtered_items:
                            rx_result = "### SECTION: RX\n\n" + "\n".join(
                                filtered_items
                            )
                            if not_covered_names:
                                rx_result += f'\n\n### SECTION: NOT_COVERED\n{", ".join(sorted(not_covered_names))}'
                        print(
                            f"[*] CONDITION FILTER: {len(filtered_items)} covered, {len(not_covered_names)} not covered"
                        )

                    elif rx_result and query_has_real_drug_name and not condition_drugs:
                        import re as _re

                        # is_drug_match imported from utility.category —
                        # handles exact, character-similarity, and phonetic matching

                        filtered_items = []
                        for item_match in _re.finditer(
                            r"Item \d+:\s*\{[^{}]+\}", rx_result, _re.DOTALL
                        ):
                            item_text = item_match.group()
                            drug_name_match = _re.search(
                                r'"drug_name":\s*"([^"]+)"', item_text
                            )
                            if drug_name_match:
                                drug_name = drug_name_match.group(1).lower()
                                if any(
                                    is_drug_match(kw, drug_name) for kw in rx_keywords
                                ):
                                    filtered_items.append(item_text)
                        if filtered_items:
                            rx_result = "### SECTION: RX\n\n" + "\n".join(
                                filtered_items
                            )
                        else:
                            # No matching drug entries — strip RX section, keep INFO only
                            rx_result = _re.sub(
                                r"### SECTION: RX.*?(?=### SECTION:|$)",
                                "",
                                rx_result or "",
                                flags=_re.DOTALL,
                            ).strip()
                        print(
                            f"[*] RX FILTERED: {len(filtered_items)} matching drug entries"
                        )

                    # After filtering — check if only INFO remains (general formulary question)
                    # Return info directly without drug table or cost table
                    has_rx_section = "### SECTION: RX" in (rx_result or "")
                    has_info_section = "### SECTION: INFO" in (rx_result or "")

                    # Drug not found — query had a drug-like keyword but nothing matched
                    # Covers both known drugs (query_has_real_drug_name=True) and
                    # unknown/misspelled drugs (rx_keywords present but no index match)
                    _no_results = not has_rx_section and not has_info_section
                    _drug_query = query_has_real_drug_name or (
                        rx_keywords
                        and not condition_drugs
                        and not used_generic_fallback
                    )
                    if _no_results and _drug_query and not condition_drugs:
                        drug_name = rx_keywords[0] if rx_keywords else "that drug"
                        rx_plan_info = (
                            member_info.get("plans", {}).get("rx", {})
                            if member_info
                            else {}
                        )
                        rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
                        rx_variant = rx_plan_info.get("variant", "")
                        rx_source = (
                            f"{rx_plan_name} ({rx_variant})"
                            if rx_variant
                            else rx_plan_name
                        )
                        print(f"[*] DRUG NOT FOUND: {drug_name}")
                        return {
                            "answer": (
                                f"**{drug_name.title()}** was not found on your formulary.\n\n"
                                f"It may be:\n"
                                f"- Known by a different name (brand vs generic)\n"
                                f"- Not covered under your current plan\n"
                                f"- Spelled differently\n\n"
                                f"Please check with your pharmacist or call member services for assistance."
                            ),
                            "pages": [],
                            "source": rx_source,
                        }

                    if has_info_section and not has_rx_section:
                        print(f"[*] RX INFO ONLY — sending to LLM for synthesis")
                        rx_plan_info = (
                            member_info.get("plans", {}).get("rx", {})
                            if member_info
                            else {}
                        )
                        rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
                        rx_variant = rx_plan_info.get("variant", "")
                        rx_source = (
                            f"{rx_plan_name} ({rx_variant})"
                            if rx_variant
                            else rx_plan_name
                        )

                        # Send to LLM to synthesize readable answer from raw INFO chunks
                        synthesis_messages = [
                            {
                                "role": "system",
                                "content": "You are a health insurance assistant. Answer the member's question using only the information provided. Be clear and concise. Do not add information not in the context.",
                            },
                            {
                                "role": "user",
                                "content": f"Member question: {query}\n\nContext from formulary booklet:\n{rx_result}",
                            },
                        ]
                        synthesized = llm_chat(
                            messages=synthesis_messages, max_tokens=500
                        )
                        return {
                            "answer": synthesized or rx_result,
                            "pages": [],
                            "source": rx_source,
                        }

                    # Step 2 — tier cost from medical index
                    # Use plan_type from member_info to decide medical vs sbc
                    plan_type = ""
                    if member_info and "plans" in member_info:
                        medical_plan = member_info["plans"].get("medical", {})
                        plan_type = medical_plan.get("plan_type", "").lower()

                    cost_category = "sbc" if "hsa" in plan_type else "medical"
                    cost_result = query_insurance_benefits(
                        query="prescription drug tier cost",
                        topics=("prescription drug",),
                        category=cost_category,
                        keywords=(
                            "prescription",
                            "drug",
                            "generic",
                            "brand",
                            "specialty",
                            "tier",
                        ),
                        member_info=json.dumps(member_info) if member_info else "{}",
                    )
                    print(f"[*] COST INDEX RESULT: {cost_result}")

                    # Step 3 — build two-section response and return directly
                    answer, rx_pages, cost_pages = build_rx_response(
                        rx_context=rx_result or "",
                        cost_context=cost_result or "",
                    )
                    all_pages = sorted(set(rx_pages + cost_pages))
                    print(f"[*] RX RESPONSE BUILT: {len(all_pages)} pages")

                    # Source shows both booklets with their respective page numbers
                    rx_plan_info = (
                        member_info.get("plans", {}).get("rx", {})
                        if member_info
                        else {}
                    )
                    rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
                    rx_variant = rx_plan_info.get("variant", "")
                    rx_source_name = (
                        f"{rx_plan_name} ({rx_variant})" if rx_variant else rx_plan_name
                    )

                    medical_plan = (
                        member_info.get("plans", {})
                        .get("medical", {})
                        .get("plan", "Medical Plan")
                        if member_info
                        else "Medical Plan"
                    )

                    # Build source string with per-booklet page numbers
                    source_parts = []
                    if rx_pages:
                        rx_page_str = ", ".join(str(p) for p in sorted(set(rx_pages)))
                        source_parts.append(f"{rx_source_name} | Pages {rx_page_str}")
                    if cost_pages:
                        cost_page_str = ", ".join(
                            str(p) for p in sorted(set(cost_pages))
                        )
                        source_parts.append(
                            f"{medical_plan} | Page {cost_page_str}"
                            if len(set(cost_pages)) == 1
                            else f"{medical_plan} | Pages {cost_page_str}"
                        )

                    source = (
                        " || ".join(source_parts) if source_parts else rx_source_name
                    )

                    # Add condition-based prefix message
                    if condition_drugs:
                        try:
                            from utility.condition_resolver import (
                                find_canonical_condition,
                                extract_condition_terms,
                            )

                            candidates = extract_condition_terms(query)
                            canonical = next(
                                (
                                    find_canonical_condition(t)
                                    for t in candidates
                                    if find_canonical_condition(t)
                                ),
                                None,
                            )
                            label = canonical or "your condition"
                            answer = (
                                f"Here are the covered medications for **{label}** on your formulary:\n\n"
                                + answer
                            )
                        except Exception:
                            pass

                    return {
                        "answer": answer,
                        "pages": [],  # pages encoded in source string for rx queries
                        "source": source,
                    }

                except Exception as e:
                    print(f"[!] RX RETRIEVAL FAILURE: {e}")
                    return {
                        "answer": "Unable to retrieve drug coverage information. Please try again.",
                        "pages": [],
                        "source": "",
                    }

            elif p_type in DIRECT_CATEGORIES:
                # Direct call — bypass LLM tool decision entirely
                print(
                    f"[DIRECT CALL]: query_insurance_benefits | {found_topics} | {p_type}"
                )

                # Determine whether this looks like a cost-specific query
                # (asking "how much"/"what does X cost"/"copay"/etc.) versus
                # a conceptual/info-seeking query (e.g. "is X covered when
                # travelling", "what does X mean", "how does X work").
                # Cost-specific queries should NOT search limitations text
                # (avoids reopening dollar-amount/cost-share leakage into
                # unrelated topic matches). Conceptual queries SHOULD search
                # it, since that's often where the only substantive
                # explanation lives (e.g. "Out-Of-Area Care" travel coverage).
                _COST_SIGNAL_WORDS = {
                    "cost",
                    "costs",
                    "copay",
                    "copays",
                    "coinsurance",
                    "much",
                    "price",
                    "fee",
                    "fees",
                    "deductible",
                    "pay",
                    "paying",
                    "charge",
                    "charges",
                    "amount",
                }
                is_cost_specific_query = any(
                    w in query_words for w in _COST_SIGNAL_WORDS
                )
                include_limitations = not is_cost_specific_query

                try:
                    tool_result = query_insurance_benefits(
                        query=query,
                        topics=tuple(found_topics),
                        category=p_type,
                        keywords=tuple(keywords),
                        member_info=json.dumps(member_info) if member_info else "{}",
                        include_limitations_text=include_limitations,
                    )
                    print(
                        f"[*] TOOL RESULT after calling query_insurance_benefits : {tool_result}"
                    )
                except Exception as e:
                    print(f"[!] RETRIEVAL FAILURE: {e}")
                    tool_result = "RETRIEVAL ERROR"

            else:
                # LLM tool orchestration — for unknown/future categories
                # Works with both Ollama (dev) and OpenAI-compatible gateways (prod)
                print(f"[LLM TOOL CALL]: category={p_type} — using LLM orchestration")
                tool_response = llm_chat_with_tools(
                    messages=messages,
                    tools=tools,
                )
                msg_content = tool_response.get("content", "")
                tool_calls = tool_response.get("tool_calls", [])

                if tool_calls:
                    tool = tool_calls[0]
                    function_name = tool["function"]["name"]
                    arguments = tool["function"]["arguments"]
                    print(f"[TOOL CALL]: {function_name} | {found_topics} | {p_type}")
                    try:
                        if function_name == "query_insurance_benefits":
                            tool_result = query_insurance_benefits(
                                query=arguments.get("query", query),
                                topics=tuple(found_topics),
                                category=p_type,
                                keywords=tuple(keywords),
                                member_info=(
                                    json.dumps(member_info) if member_info else "{}"
                                ),
                            )
                        print(
                            f"[*] TOOL RESULT after calling query_insurance_benefits : {tool_result}"
                        )
                    except Exception as e:
                        print(f"[!] TOOL FAILURE: {e}")
                        tool_result = "RETRIEVAL ERROR"
                else:
                    print("[!] No tool call from LLM — forcing direct retrieval")
                    try:
                        tool_result = query_insurance_benefits(
                            query=query,
                            topics=tuple(found_topics),
                            category=p_type,
                            keywords=tuple(keywords),
                            member_info=(
                                json.dumps(member_info) if member_info else "{}"
                            ),
                        )
                    except Exception as e:
                        print(f"[!] RETRIEVAL FAILURE: {e}")
                        tool_result = "RETRIEVAL ERROR"

            # ============================================================
            # STEP 3 — KEYWORD FILTERING (HEADER-AWARE)
            # ============================================================
            if tool_result and tool_result != "RETRIEVAL ERROR" and keywords:

                section_blocks = re.split(r"(### SECTION: [A-Z]+)", tool_result)
                rebuilt = []
                current_header = None

                for block in section_blocks:
                    block = block.strip()
                    if not block:
                        continue

                    # SECTION HEADER
                    if block.startswith("### SECTION:"):
                        current_header = block
                        rebuilt.append(block)
                        continue

                    # COST and INFO sections → NEVER FILTER
                    if current_header in ("### SECTION: COST", "### SECTION: INFO"):
                        rebuilt.append(block)
                        continue

                    # Other sections → keyword filter
                    if any(k.lower() in block.lower() for k in keywords):
                        rebuilt.append(block)

                tool_result = "\n\n".join(rebuilt)

            # ============================================================
            # 🔥 STEP 4 — CLEAN CONTEXT (CRITICAL)
            # ============================================================
            def trim_context(text, max_chars=8000):
                if not text:
                    return ""

                if len(text) <= max_chars:
                    return text

                cut = text[:max_chars]
                last_break = cut.rfind("\n\n")

                if last_break != -1:
                    return cut[:last_break]

                return cut

            # ============================================================
            # STEP 5 — FINAL ANSWER (NO TOOLS)
            # ============================================================

            # 🔥 CLEAN CONTEXT (single trim only)
            clean_context = trim_context(tool_result or "", 8000)
            print(f"[*] CLEAN CONTEXT FOR FINAL PROCESSING : {clean_context}")

            # ============================================================
            # STEP 6 — SETUP THE CACHE
            # ============================================================
            # After getting clean context store the value in cache (24h TTL)
            await set_query_result(cache_key, clean_context)
        sections = []

        if "### SECTION: QA" in clean_context:
            sections.append("qa")

        if "### SECTION: COST" in clean_context:
            sections.append("cost")

        if "### SECTION: EXCLUDED" in clean_context:
            sections.append("excluded")

        if "### SECTION: INFO" in clean_context:
            sections.append("info")

        print(f"[*] DETECTED SECTIONS: {sections}")

        # ── Define table builders here so they're available in all branches ──
        # --------------------------------------------------------
        # 🔥 MULTI-SECTION → try no-LLM first, fall back to LLM
        # --------------------------------------------------------
        if len(sections) > 1:

            if set(sections) == {"cost", "info"}:
                print("[*] COST+INFO → BUILDING BOTH TABLES (NO LLM)")

                # Extract each section's context separately
                def split_section(ctx, name):
                    marker = f"### SECTION: {name.upper()}"
                    start = ctx.find(marker)
                    if start == -1:
                        return ctx
                    next_marker = ctx.find("### SECTION:", start + len(marker))
                    return ctx[start:next_marker] if next_marker != -1 else ctx[start:]

                cost_ctx = split_section(clean_context, "cost")
                info_ctx = split_section(clean_context, "info")

                # Suppress COST for exclusion queries — "what's not covered?"
                # is a pure INFO question; showing covered services is misleading.
                _exclusion_query = any(
                    t in found_topics
                    for t in [
                        "dental exclusions",
                        "exclusions and limitations",
                        "vision exclusions",
                    ]
                )
                if _exclusion_query:
                    cost_table, cost_pages = None, []
                else:
                    cost_table, cost_pages = build_cost_table(
                        cost_ctx, query, keywords, found_topics
                    )

                # Suppress INFO for dental pure-cost queries (copay, x-ray)
                # to prevent unrelated prose (e.g. "Dental Emergency") appearing.
                # Only applies to dental — medical/vision x-ray queries need INFO.
                _SERVICE_SPECIFIC_KEYS = {
                    "radiographic",
                    "bitewing",
                    "periapical",
                    "panoramic",
                    "prophylaxis",
                    "sealant",
                    "fluoride",
                    "varnish",
                    "amalgam",
                    "composite",
                    "endodontic",
                    "canal",
                    "pontic",
                    "extraction",
                    "scaling",
                    "debridement",
                    "gingivectomy",
                    "prosthodontics",
                }
                _pure_cost_query = (p_type == "dental") and (
                    any(
                        w in query.lower()
                        for w in ["copay", "x-ray", "xray", "radiograph"]
                    )
                    or any(kw in _SERVICE_SPECIFIC_KEYS for kw in keywords)
                )
                if _pure_cost_query:
                    info_table, info_pages = None, []
                else:
                    info_table, info_pages = build_info_response(
                        info_ctx, query, keywords
                    )

                if cost_table not in (None, "__USE_LLM__") and info_table not in (
                    None,
                    "__USE_LLM__",
                ):
                    summary = generate_summary(
                        query,
                        info_table=info_table,
                        keywords=keywords,
                    )
                    combined = cost_table.rstrip("\n") + "\n\n" + info_table
                    answer = f"{summary}\n\n{combined}" if summary else combined
                    pages = _add_offset(sorted(set(cost_pages + info_pages)))
                    print("[*] COST+INFO COMBINED TABLE → RETURNING (NO LLM)")
                    return {
                        "answer": answer,
                        "category": p_type,
                        "pages": pages,
                        "source": _booklet_name,
                    }

                elif cost_table not in (None, "__USE_LLM__"):
                    # Cost-only — no summary (table is self-explanatory)
                    print("[*] COST+INFO → RETURNING COST TABLE ONLY (NO LLM)")
                    return {
                        "answer": cost_table,
                        "category": p_type,
                        "pages": _add_offset(cost_pages),
                        "source": _booklet_name,
                    }

                elif info_table not in (None, "__USE_LLM__"):
                    summary = generate_summary(
                        query,
                        info_table=info_table,
                        keywords=keywords,
                    )
                    answer = f"{summary}\n\n{info_table}" if summary else info_table
                    print("[*] COST+INFO → RETURNING INFO TABLE ONLY (NO LLM)")
                    return {
                        "answer": answer,
                        "category": p_type,
                        "pages": _add_offset(info_pages),
                        "source": _booklet_name,
                    }

                # ── Tier 2: cheap mini-LLM (~50-100 tokens) ──────────────────
                # Scoring failed for both — LLM extracts the canonical benefit
                # name and intent, then re-scores. 10-20x cheaper than full LLM.
                print("[*] SCORING FAILED → MINI-LLM CLASSIFICATION (TIER 2)")
                try:
                    mini_prompt = (
                        f"Extract from the query: (1) the benefit/service name as it "
                        f"appears in a benefits booklet, (2) intent: cost, info, or both.\n"
                        f'Return ONLY JSON: {{"benefit": "...", "intent": "cost|info|both"}}\n'
                        f"Examples:\n"
                        f'  "what i need to pay for my PCP visit" → {{"benefit": "Professional Visits And Services", "intent": "cost"}}\n'
                        f'  "what is prior authorization" → {{"benefit": "Prior Authorization", "intent": "info"}}\n'
                        f'  "blood products coverage and cost" → {{"benefit": "Blood Products And Services", "intent": "both"}}\n'
                        f"Query: {query}"
                    )
                    from utility.llm import llm_generate
                    import json as _json

                    mini_content = llm_generate(prompt=mini_prompt, max_tokens=60)
                    mini_data = _json.loads(mini_content) if mini_content else {}
                    benefit_name = mini_data.get("benefit", "")
                    intent = mini_data.get("intent", "both")
                    print(f"[*] MINI-LLM: benefit={benefit_name!r}  intent={intent}")

                    if benefit_name:
                        if intent in ("cost", "both"):
                            ct2, cp2 = build_cost_table(
                                cost_ctx, benefit_name, [benefit_name], found_topics
                            )
                            if ct2 not in (None, "__USE_LLM__"):
                                if intent == "cost":
                                    return {
                                        "answer": ct2,
                                        "pages": _add_offset(cp2),
                                        "source": _booklet_name,
                                    }
                                it2, ip2 = build_info_response(
                                    info_ctx, benefit_name, [benefit_name]
                                )
                                if it2 not in (None, "__USE_LLM__"):
                                    return {
                                        "answer": ct2.rstrip("\n") + "\n\n" + it2,
                                        "pages": _add_offset(sorted(set(cp2 + ip2))),
                                        "source": _booklet_name,
                                    }
                                return {
                                    "answer": ct2,
                                    "pages": _add_offset(cp2),
                                    "source": _booklet_name,
                                }
                        if intent in ("info", "both"):
                            it2, ip2 = build_info_response(
                                info_ctx, benefit_name, [benefit_name]
                            )
                            if it2 not in (None, "__USE_LLM__"):
                                return {
                                    "answer": it2,
                                    "pages": _add_offset(ip2),
                                    "source": _booklet_name,
                                }
                except Exception as e:
                    print(f"[*] MINI-LLM FAILED: {e}")
                # fallthrough to full LLM (Tier 3)

            else:
                print("[*] MULTI SECTION → USING LLM")

        # --------------------------------------------------------
        # 🔥 SINGLE SECTION → OPTIMIZE
        # --------------------------------------------------------
        elif len(sections) == 1:
            if sections[0] == "cost":
                print("[*] COST DETECTED → TRYING PARSER")
                final_answer, final_pages = build_cost_table(
                    clean_context, query, keywords, found_topics
                )
                if final_answer == "__USE_LLM__":
                    print("[*] SWITCHING TO LLM DUE TO NOISE")
                elif final_answer:
                    # Cost-only — no summary (table is self-explanatory)
                    print("[*] PARSER SUCCESS → RETURNING TABLE")
                    return {
                        "answer": final_answer,
                        "category": p_type,
                        "pages": _add_offset(final_pages),
                        "source": _booklet_name,
                    }

            elif sections[0] == "info":
                print("[*] INFO DETECTED → BUILDING INFO TABLE")
                final_answer, final_pages = build_info_response(
                    clean_context, query, keywords
                )
                if final_answer == "__USE_LLM__":
                    print("[*] INFO FALLBACK TO LLM")
                elif final_answer:
                    print("[*] INFO PARSER SUCCESS → RETURNING TABLE")
                    return {
                        "answer": final_answer,
                        "pages": _add_offset(final_pages),
                        "source": _booklet_name,
                    }

        # --------------------------------------------------------
        # 🔥 FALLBACK
        # --------------------------------------------------------
        else:
            print("[*] NO SECTION DETECTED → USING LLM")

        print(f"[*] CLEAN CONTENT SENDING TO LLM : {clean_context}")
        if clean_context.strip() == "":
            print("[!] EMPTY CONTEXT → SKIPPING LLM")
            return {"answer": "No relevant information found."}

        final_messages = [
            {
                "role": "system",
                "content": generate_ironclad_instruction(),  # 🔥 USE YOUR STRONG PROMPT
            },
            {
                "role": "user",
                "content": f"""
        User Question:
        {query}
 
        Context:
        {clean_context}
        """,
            },
        ]

        final_answer = llm_chat(
            messages=final_messages,
            temperature=0.0,
        )

        # ============================================================
        # FINAL RESPONSE (STRICT CONTRACT WITH UI)
        # ============================================================

        # ============================================================
        # 🔥 FORCE STRING (HARD GUARANTEE)
        # ============================================================

        if not isinstance(final_answer, str):
            try:
                final_answer = json.dumps(final_answer)
            except Exception:
                final_answer = str(final_answer)

        final_answer = (final_answer or "").strip()

        # ============================================================
        # 🔥 IRON CURTAIN (KEEP — GOOD LOGIC)
        # ============================================================

        if "|" in final_answer:
            start_index = final_answer.find("|")
            end_index = final_answer.rfind("|")

            if end_index > start_index:
                final_answer = final_answer[start_index : end_index + 1].strip()

        # ============================================================
        # 🔥 FALLBACK (ONLY IF EMPTY OR BAD OUTPUT)
        # ============================================================
        if not final_answer or "|" not in final_answer:
            print("[!] BAD OR EMPTY FINAL ANSWER")

            final_answer = "No relevant information found."
        # ============================================================
        # 🔥 FINAL SAFETY (ABSOLUTE GUARANTEE FOR UI)
        # ============================================================

        if not isinstance(final_answer, str):
            final_answer = str(final_answer)

        # 🔥 THIS IS THE MOST IMPORTANT LINE
        final_answer = final_answer or "No data found."

        print(f"[FINAL ANSWER TYPE]: {type(final_answer)}")
        print(f"[FINAL ANSWER LENGTH]: {len(final_answer)}")
        print(f"Final answer = {final_answer[:500]}")

        # Tier 3: parse pages from all chunks in context (LLM used them all)
        import re as _re

        _t3_pages = []
        for _item in _re.split(r"Item \d+:", clean_context):
            _m = _re.search(r'"page_number"\s*:\s*(\d+)', _item)
            if _m:
                _pg = int(_m.group(1))
                if _pg > 0:
                    _t3_pages.append(_pg - _page_offset)

        return {
            "answer": final_answer,
            "category": p_type,
            "pages": sorted(set(_t3_pages)),
            "source": _booklet_name,
        }

    except Exception as e:
        print(f"❌ SYNTHESIS ERROR: {e}")
        return f"⚠️ Client Logic Error: {str(e)}"


# # ========================================Previous working code before Rx spelling mistake changes========================================

# # import re
# # import json
# # import os

# # from config import settings
# # from utility.llm import llm_chat, llm_generate, llm_chat_with_tools
# # from utility.utils import flatten_message_content, smart_match
# # from utility.category import (
# #     detect_category,
# #     detect_category_rule_based,
# #     detect_category_from_history,
# #     extract_user_queries,
# #     is_conversational,
# #     is_drug_name_query,
# # )
# # from utility.topic_resolver import resolve_insurance_topic, NOISE_WORDS
# # from utility.response_builder import (
# #     build_cost_table,
# #     build_info_response,
# #     build_rx_response,
# #     generate_ironclad_instruction,
# # )
# # from utility.prompts import (
# #     TOPIC_EXTRACTION_PROMPT,
# #     GUIDANCE_NO_CATEGORY,
# #     GUIDANCE_CONVERSATIONAL,
# #     GUIDANCE_MEDICAL_VAGUE,
# #     GUIDANCE_DENTAL_VAGUE,
# #     GUIDANCE_VISION_VAGUE,
# # )

# # from infrastructure.cache import get_query_result, set_query_result

# # last_type_global = None


# # def generate_summary(
# #     query: str,
# #     info_table: str = "",
# #     keywords: list | None = None,
# # ) -> str:
# #     """
# #     Generate one direct sentence answering the user's question.
# #     Uses info_table for context. Skips if info is unrelated to query.
# #     Max 40 tokens. Silent fail: returns empty string if anything goes wrong.
# #     """
# #     try:
# #         content = info_table[:600] if info_table and info_table.strip() else ""
# #         if not content.strip():
# #             return ""

# #         # Relevance check — use query words + resolved topics/keywords
# #         # to verify the info content is about what was asked
# #         import re as _re
# #         from utility.utils import NOISE_WORDS

# #         # Use keywords only for relevance — they are the most specific extracted terms
# #         # Topics are too generic (e.g. "hospital", "network") and cause false positives
# #         # Query words are also too broad — keywords are already derived from the query
# #         check_terms = set()
# #         for k in keywords or []:
# #             check_terms.update(
# #                 w
# #                 for w in _re.sub(r"[^\w\s]", "", k.lower()).split()
# #                 if len(w) > 3 and w not in NOISE_WORDS
# #             )

# #         content_lower = content.lower()
# #         # Word-boundary matching: "provide" won't match "provides"
# #         if check_terms and not any(
# #             _re.search(r"\b" + _re.escape(t) + r"\b", content_lower)
# #             for t in check_terms
# #         ):
# #             print(f"[*] SUMMARY SKIPPED — info not relevant (terms={check_terms})")
# #             return ""
# #         # Skip summary for value-seeking queries — the table already shows
# #         # exact amounts/percentages and the summary would be incomplete after stripping
# #         _ql = query.lower().strip()
# #         _value_seeking = any(
# #             _ql.startswith(p)
# #             for p in [
# #                 "how much",
# #                 "what is the cost",
# #                 "what does it cost",
# #                 "what is the copay",
# #                 "what is the coinsurance",
# #                 "what is the charge",
# #                 "what is the fee",
# #                 "what is the rate",
# #                 "what is my copay",
# #                 "what is my coinsurance",
# #                 "what is my deductible",
# #             ]
# #         )
# #         if _value_seeking:
# #             print(
# #                 f"[*] SUMMARY SKIPPED — value-seeking query, table is self-explanatory"
# #             )
# #             return ""

# #         prompt = (
# #             f"You are an insurance assistant. Write ONE plain sentence summarising the benefit below.\n"
# #             f"- State the fact directly. Do NOT start with Yes, No, or any affirmation.\n"
# #             f"- Example: 'Allergy testing and treatment is covered when provided by a certified allergy specialist.'\n"
# #             f"- Example: 'TMJ care is covered under your plan subject to applicable cost-sharing.'\n"
# #             f"- Do NOT mention any dollar amounts, copays, percentages or deductibles.\n"
# #             f"- No extra text, no preamble.\n"
# #             f"Query: {query}\n"
# #             f"Benefits data:\n{content}"
# #         )
# #         response_text = llm_chat(
# #             messages=[{"role": "user", "content": prompt}],
# #             max_tokens=40,
# #             temperature=0.0,
# #         )
# #         summary = response_text.strip()

# #         # Post-process: strip any leaked cost figures the model included anyway
# #         import re as _re

# #         summary = _re.sub(r"\$[\d,]+(?:\.\d+)?", "", summary)  # $25, $1,000
# #         summary = _re.sub(r"\d+%", "", summary)  # 20%, 10%
# #         summary = _re.sub(r"\s{2,}", " ", summary).strip()  # clean extra spaces
# #         if summary and not summary.endswith((".", "!", "?")):
# #             summary += "."
# #         print(f"[*] SUMMARY GENERATED: {summary[:100]}")
# #         return summary
# #     except Exception as e:
# #         print(f"[*] SUMMARY FAILED: {e}")
# #         return ""


# # async def get_ai_response(
# #     query: str,
# #     history: list,
# #     member_info: dict | None = None,
# #     current_category: str = "",
# # ):
# #     # 1. ACCESS GLOBALS
# #     global p_type_fast, p_tier_fast, last_type_global

# #     found_topics = []
# #     keywords = []

# #     # Categories that bypass LLM tool call — direct to tools.py
# #     # These have well-defined scoring and structured indices
# #     # Add new categories here ONLY when they have a proper indexer + scoring logic
# #     DIRECT_CATEGORIES = {"medical", "dental", "vision", "rx"}

# #     try:
# #         from insurance_mcp.tools import (
# #             get_plan_data_from_disk as query_insurance_benefits,
# #         )

# #         # --- 2. CONTEXT MERGING (MEMORY) ---
# #         # Limit to last 5 turns (10 messages) — avoids stale context polluting
# #         # topic resolution and keyword extraction for long conversations.
# #         recent_history = " ".join(
# #             [flatten_message_content(m["content"]) for m in history[-10:]]
# #         )
# #         query_lower = query.lower()

# #         # Clean words for surgical matching
# #         query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]

# #         print(f"[*] Query Words for Matching: {query_words}")
# #         print("[DEBUG] urgent match:", smart_match("urgent", query_words, query_lower))

# #         # --- CONVERSATIONAL INTENT CHECK ---
# #         # Short-circuit before any RAG pipeline if query is a greeting,
# #         # follow-up, or clearly non-benefit question.
# #         if is_conversational(query):
# #             print("[*] CONVERSATIONAL QUERY DETECTED → returning guidance")
# #             return GUIDANCE_CONVERSATIONAL

# #         # --- 3. TYPE DETECTION ---
# #         p_type = detect_category(query_words, query)
# #         print(f"[*] FINAL TYPE DETECTED : {p_type}")
# #         if not p_type:
# #             print("[*] TYPE NOT DETECTED IN QUERY - SEARCHING HISTORY")
# #             p_type = detect_category_from_history(history)

# #         if not p_type:
# #             print("[*] NO CATEGORY DETECTED → returning guidance")
# #             return GUIDANCE_NO_CATEGORY

# #         if last_type_global is not None and last_type_global != p_type:
# #             print("[*] CATEGORY CHANGED → SKIP HISTORY")
# #             skip_topic_history_check = True
# #         else:
# #             skip_topic_history_check = False

# #         # --- 4. UNIFIED TOPIC DETECTION ---
# #         resolved = resolve_insurance_topic(query_words, query_lower, p_type=p_type)

# #         found_topics = resolved.get("topics", [])
# #         keywords = resolved.get("keywords", [])

# #         print(f"[*] LENGTH OF DETECTED TOPIC : {len(found_topics)}")

# #         # ========================================================================
# #         # DENTAL OVERVIEW DETECTION — "what does my plan cover" / broad queries
# #         # Map to all class topics so server returns a comprehensive overview
# #         # instead of falling to LLM with nonsense topics like "pharmacy"
# #         # ========================================================================
# #         _OVERVIEW_TERMS = [
# #             "what does my plan cover",
# #             "what is covered",
# #             "what's covered",
# #             "overview",
# #             "summary",
# #             "all benefits",
# #             "all covered",
# #             "each type",
# #             "types of service",
# #             "types of coverage",
# #             "everything covered",
# #             "full coverage",
# #             "complete coverage",
# #             "services are covered",
# #             "services covered",
# #             "what services",
# #         ]
# #         _dental_words = ["dental", "tooth", "teeth", "gum", "oral", "dentist"]
# #         if (
# #             not found_topics
# #             and any(w in query_lower for w in _dental_words)
# #             and any(smart_match(w, query_words, query_lower) for w in _OVERVIEW_TERMS)
# #         ):
# #             found_topics = ["class i", "class ii", "class iii", "plan limits"]
# #             keywords = keywords + [
# #                 "class i diagnostic and preventive services",
# #                 "class ii basic services",
# #                 "class iii major services",
# #                 "plan limits",
# #                 "deductible",
# #                 "annual maximum",
# #             ]
# #             print(f"[*] DENTAL OVERVIEW QUERY → expanding to all class topics")

# #         # ========================================================================
# #         # IF STILL TOPIC IS BLANK THEN WE NEED TO TAKE LLM HELP TO IDENTIFY TOPIC
# #         # Skip this for rx — the rx block below builds drug-name keywords
# #         # directly from query_words, so the generic topic LLM call would be
# #         # wasted work that gets immediately discarded.
# #         # ========================================================================
# #         if not found_topics and p_type != "rx":
# #             print(f"[*] TURNING TO LLM TO FIND THE TOPIC FROM QUERY : {query}")
# #             topic_prompt = TOPIC_EXTRACTION_PROMPT

# #             llm_messages = [
# #                 {"role": "system", "content": topic_prompt},
# #                 {"role": "user", "content": f"User Query: {query_lower}"},
# #             ]

# #             # ============================================================
# #             # 🔥 LLM TOPIC + KEYWORD EXTRACTION (PRODUCTION SAFE)
# #             # ============================================================

# #             raw_content = llm_chat(
# #                 messages=llm_messages,
# #                 format="json",
# #                 max_tokens=200,
# #             )
# #             print(f"[*] ROW CONTENT BY LLM AFTER TOPIC SEARCH : {raw_content}")

# #             match = re.search(r"\{.*\}", raw_content, re.DOTALL)

# #             # ============================================================
# #             # 🔧 HELPERS
# #             # ============================================================

# #             INVALID_TOPICS = {"medical", "dental", "vision"}
# #             INVALID_KEYWORDS = NOISE_WORDS

# #             def normalize_topic(t):
# #                 return str(t).lower().strip()

# #             def word_count_topic(t):
# #                 # treat "_" and "-" as separators but NOT destructive
# #                 parts = re.split(r"[ _-]+", t)
# #                 return len([p for p in parts if p])

# #             def canonicalize_topic(t):
# #                 # unify format → prefer hyphen
# #                 return t.replace("_", "-").strip()

# #             def normalize_keyword(k):
# #                 return re.sub(r"[^\w\s-]", "", str(k).lower()).strip()

# #             # ============================================================
# #             # 🔍 PARSE RESPONSE
# #             # ============================================================

# #             if match:
# #                 try:
# #                     json_str = match.group(0)
# #                     data = json.loads(json_str)

# #                     # -------------------------
# #                     # 🔹 RAW VALUES
# #                     # -------------------------
# #                     new_topics = data.get("topics", []) or []
# #                     raw_keywords = data.get("keywords", []) or []

# #                     print(f"[*] RAW TOPICS FROM LLM: {new_topics}")
# #                     print(f"[*] RAW KEYWORDS FROM LLM: {raw_keywords}")

# #                     # ====================================================
# #                     # 🔥 CLEAN KEYWORDS (ALWAYS KEEP)
# #                     # ====================================================
# #                     keywords = []
# #                     for k in raw_keywords:
# #                         clean_k = normalize_keyword(k)
# #                         if (
# #                             clean_k
# #                             and clean_k not in keywords
# #                             and clean_k not in INVALID_KEYWORDS
# #                         ):
# #                             keywords.append(clean_k)

# #                     print(f"[*] CLEANED KEYWORDS: {keywords}")

# #                     # ====================================================
# #                     # 🔥 CLEAN TOPICS (STRICT FILTER)
# #                     # ====================================================
# #                     cleaned_topics = []

# #                     if isinstance(new_topics, list):
# #                         for t in new_topics:
# #                             if not t:
# #                                 continue

# #                             raw = normalize_topic(t)

# #                             # ❌ skip UNKNOWN
# #                             if raw == "unknown":
# #                                 continue

# #                             # ❌ skip category leakage
# #                             if raw in INVALID_TOPICS:
# #                                 print(f"[*] SKIPPING INVALID TOPIC: {raw}")
# #                                 continue

# #                             # ❌ reject noisy topics (>2 words)
# #                             wc = word_count_topic(raw)
# #                             if wc > 2:
# #                                 print(f"[*] REJECTING NOISY TOPIC ({wc} words): {raw}")
# #                                 continue

# #                             # ❌ reject topics whose every word is a noise word
# #                             # e.g. "plan details", "coverage information", "plan overview"
# #                             topic_words = re.split(r"[ _-]+", raw)
# #                             if all(w in NOISE_WORDS for w in topic_words if w):
# #                                 print(f"[*] REJECTING ALL-NOISE TOPIC: {raw}")
# #                                 continue

# #                             # ✅ canonical form
# #                             clean_topic = canonicalize_topic(raw)

# #                             if clean_topic not in cleaned_topics:
# #                                 cleaned_topics.append(clean_topic)

# #                     # ====================================================
# #                     # 🔥 FINAL MERGE INTO found_topics
# #                     # ====================================================
# #                     if cleaned_topics:
# #                         print(f"[*] FINAL CLEANED TOPICS: {cleaned_topics}")

# #                         for t in cleaned_topics:
# #                             if t not in found_topics:
# #                                 found_topics.append(t)

# #                     else:
# #                         # print("[*] NO VALID TOPIC FROM LLM → USING KEYWORDS ONLY")
# #                         print("[*] NO VALID TOPIC FROM LLM → USING KEYWORDS AS TOPICS")

# #                         # 🔥 fallback: use keywords as retrieval topics
# #                         for kw in keywords:
# #                             if kw not in found_topics:
# #                                 found_topics.append(kw)

# #                 except json.JSONDecodeError as e:
# #                     print(f"[!] JSON PARSE ERROR: {e}")
# #             else:
# #                 print(f"[!] No JSON block found in LLM response: {raw_content}")
# #         # ========================================================================

# #         if len(found_topics) == 0 and skip_topic_history_check == False:

# #             user_queries = extract_user_queries(history[-10:])

# #             for past_query in reversed(user_queries):
# #                 print(f"[*] CHECKING PAST QUERY: {past_query}")

# #                 history_word_list = [
# #                     re.sub(r"[^\w\s]", "", w) for w in past_query.split()
# #                 ]

# #                 # Stop at category boundary — rule-based only, no LLM.
# #                 # If past query is clearly a different category → stop searching.
# #                 # If ambiguous (returns None) → assume same category, continue.
# #                 past_cat = detect_category_rule_based(history_word_list, past_query)
# #                 if past_cat is not None and past_cat != p_type:
# #                     print(
# #                         f"[*] HISTORY BOUNDARY HIT ({past_cat} != {p_type}) → stopping"
# #                     )
# #                     break

# #                 history_resolved = resolve_insurance_topic(
# #                     history_word_list, past_query
# #                 )
# #                 history_topics = history_resolved.get("topics", [])
# #                 history_keywords = history_resolved.get("keywords", [])

# #                 if history_topics:
# #                     print(f"[*] RECOVERED TOPIC FROM HISTORY: {history_topics}")
# #                     found_topics.extend(
# #                         t for t in history_topics if t not in found_topics
# #                     )
# #                     keywords = list(set(keywords + history_keywords))
# #                     break

# #         print(f"Final topic found from query : {found_topics}")
# #         last_type_global = p_type  # Now setting up the last type here as now we have category and topic and keywords

# #         # Page reference helpers — used at every return point below
# #         # member_info shape: {"year":..., "plans": {"medical": {...}, "dental": {...}}}
# #         _plan_info = (member_info.get("plans", {}) if member_info else {}).get(
# #             p_type, {}
# #         )
# #         _page_offset = _plan_info.get("page_offset", 0)
# #         _booklet_name = _plan_info.get("plan", "")

# #         def _add_offset(raw_pages):
# #             return sorted(
# #                 p - _page_offset for p in raw_pages if p > 0 and p - _page_offset > 0
# #             )

# #         # Once we reach here it means topic did not available
# #         if len(found_topics) == 0 and len(keywords) == 0:
# #             print(f"[*] NO TOPIC FOUND FOR {p_type} → returning guidance")
# #             if p_type.lower() == "medical":
# #                 return GUIDANCE_MEDICAL_VAGUE
# #             elif p_type.lower() == "dental":
# #                 return GUIDANCE_DENTAL_VAGUE
# #             elif p_type.lower() == "vision":
# #                 return GUIDANCE_VISION_VAGUE
# #             else:
# #                 return GUIDANCE_NO_CATEGORY

# #         # 🔥 normalize topics
# #         topics_part = "_".join(sorted(filter(None, found_topics)))

# #         # 🔥 normalize keywords (only if present) — dedupe, lowercase, sort for consistent key
# #         # regardless of query word order
# #         if keywords:
# #             normalized_keywords = sorted(
# #                 set(k.lower().strip() for k in keywords if k and len(k.strip()) > 2)
# #             )
# #             keywords_part = "_".join(normalized_keywords)
# #             cache_key = (
# #                 f"{p_type}_{topics_part}_{keywords_part}"
# #                 if keywords_part
# #                 else f"{p_type}_{topics_part}"
# #             )
# #         else:
# #             cache_key = f"{p_type}_{topics_part}"

# #         print(f"[*] CACHE KEY: {cache_key}")

# #         cached_context = await get_query_result(cache_key)
# #         cache_hit = False
# #         clean_context = ""

# #         if cached_context:
# #             print(f"[*] CACHE HIT: {cache_key}")
# #             print(f"[*] CACHED CONTEXT: {cached_context}")
# #             clean_context = cached_context
# #             cache_hit = True

# #         # --- 9. THE REASONING LOOP (skipped on cache hit) ---
# #         if not cache_hit:
# #             messages = []

# #             # --- SYSTEM PROMPT ---
# #             system_prompt = {
# #                 "role": "system",
# #                 "content": (
# #                     "You are an AI assistant.\n"
# #                     "For every user question, you MUST call the tool "
# #                     "'query_insurance_benefits'.\n"
# #                     "Do NOT answer directly.\n"
# #                     "Return ONLY the tool call."
# #                 ),
# #             }

# #             messages.append(system_prompt)

# #             # --- HISTORY ---
# #             # Pass last 4 turns (8 messages) to LLM — enough for conversational
# #             # context without bloating the prompt with stale exchanges.
# #             if history:
# #                 for turn in history[-8:]:
# #                     messages.append(
# #                         {
# #                             "role": turn.get("role", "user"),
# #                             "content": flatten_message_content(turn.get("content", "")),
# #                         }
# #                     )

# #             messages.append({"role": "user", "content": query})

# #             # --- TOOL ---
# #             tools = [
# #                 {
# #                     "type": "function",
# #                     "function": {
# #                         "name": "query_insurance_benefits",
# #                         "description": (
# #                             "MANDATORY: Retrieve insurance benefit details using the user query and resolved topics. "
# #                             "You MUST call this tool before answering."
# #                         ),
# #                         "parameters": {
# #                             "type": "object",
# #                             "properties": {
# #                                 "query": {
# #                                     "type": "string",
# #                                     "description": "The full user question",
# #                                 },
# #                                 "topics": {
# #                                     "type": "array",
# #                                     "items": {"type": "string"},
# #                                     "description": "List of relevant topics like ['primary', 'urgent care', 'imaging']",
# #                                 },
# #                                 "category": {
# #                                     "type": "string",
# #                                     "description": "Category (medical / dental / sbc)",
# #                                 },
# #                                 "keywords": {
# #                                     "type": "array",
# #                                     "items": {"type": "string"},
# #                                     "description": "List of relevant keywords like ['immunization', 'mammogram']",
# #                                 },
# #                             },
# #                             "required": ["query", "topics", "category"],
# #                         },
# #                     },
# #                 }
# #             ]

# #             print(f"[*] QUERY: {query}")
# #             print(f"[*] TOPICS (fallback only): {found_topics}")

# #             # ============================================================
# #             # STEP 1 — RETRIEVE CHUNKS
# #             # Direct call for known categories — no LLM tool overhead
# #             # LLM tool orchestration reserved for future unknown categories
# #             # ============================================================
# #             tool_result = None

# #             if p_type == "rx":
# #                 # ── Rx: dual-index query ──────────────────────────────────────

# #                 # Extract drug name keywords — skip general terms AND
# #                 # process/action words that dilute scoring (need, require, does,
# #                 # prior, authorization etc.) so the actual drug name dominates
# #                 # the topic/keyword match instead of being diluted by these.
# #                 _RX_NOISE_WORDS = {
# #                     "what",
# #                     "tier",
# #                     "covered",
# #                     "cost",
# #                     "much",
# #                     "show",
# #                     "does",
# #                     "plan",
# #                     "cover",
# #                     "drug",
# #                     "prescription",
# #                     "medication",
# #                     "formulary",
# #                     "generic",
# #                     "brand",
# #                     "pharmacy",
# #                     "refill",
# #                     "drugs",
# #                     "tiers",
# #                     "mean",
# #                     "means",
# #                     "explain",
# #                     "definition",
# #                     "list",
# #                     "covered",
# #                     "benefits",
# #                     "need",
# #                     "needs",
# #                     "require",
# #                     "requires",
# #                     "requirement",
# #                     "prior",
# #                     "authorization",
# #                     "step",
# #                     "therapy",
# #                     "quantity",
# #                     "limit",
# #                     "specialty",
# #                     "under",
# #                     "your",
# #                     "this",
# #                     # Conversational filler words — carry no drug-name signal
# #                     "want",
# #                     "know",
# #                     "about",
# #                     "tell",
# #                     "please",
# #                     "could",
# #                     "would",
# #                     "like",
# #                     "they",
# #                     "them",
# #                     "have",
# #                     "give",
# #                     "more",
# #                     "information",
# #                     "details",
# #                     "find",
# #                     "looking",
# #                     # General concept words also in GENERAL_RX_TERMS — these
# #                     # signal an info question, not a drug name, so they
# #                     # should never survive into rx_keywords either
# #                     "preventive",
# #                     "exception",
# #                     "optional",
# #                     "chemotherapy",
# #                 }
# #                 rx_keywords = tuple(
# #                     w for w in query_words if len(w) > 3 and w not in _RX_NOISE_WORDS
# #                 )
# #                 # No fallback to raw `keywords` here — that variable comes from
# #                 # the generic topic_resolver.py pipeline and is NOT noise-filtered,
# #                 # so falling back to it would reintroduce exactly the words we
# #                 # just filtered out (e.g. "formulary", "drugs"). An empty
# #                 # rx_keywords is the correct signal for "no real drug name in
# #                 # this query" — it's handled below by query_has_real_drug_name.

# #                 print(f"[DIRECT CALL]: rx dual-index | keywords={rx_keywords} | rx")
# #                 try:
# #                     # Step 1 — drug coverage from Rx formulary index
# #                     #
# #                     # When rx_keywords is empty, the query has no drug name —
# #                     # but it may still have a meaningful CONCEPT word (e.g.
# #                     # "preventive", "ACA") that should drive the search, just
# #                     # via requirement-matching instead of name-matching.
# #                     # Only fall back to the generic formulary search when
# #                     # there's truly no signal at all (e.g. "what is formulary
# #                     # drugs?" — nothing left to search by except the concept
# #                     # of formulary itself).
# #                     _PURE_NOISE_WORDS = {
# #                         "want",
# #                         "know",
# #                         "about",
# #                         "tell",
# #                         "please",
# #                         "could",
# #                         "would",
# #                         "like",
# #                         "they",
# #                         "them",
# #                         "have",
# #                         "give",
# #                         "more",
# #                         "information",
# #                         "details",
# #                         "find",
# #                         "looking",
# #                         "what",
# #                         "drug",
# #                         "drugs",
# #                         "formulary",
# #                         "list",
# #                         "covered",
# #                         "cost",
# #                         "much",
# #                         "show",
# #                         "does",
# #                         "plan",
# #                         "cover",
# #                         "prescription",
# #                         "medication",
# #                     }
# #                     concept_terms = tuple(
# #                         w
# #                         for w in query_words
# #                         if len(w) > 3 and w not in _PURE_NOISE_WORDS
# #                     )
# #                     search_terms = (
# #                         rx_keywords
# #                         or concept_terms
# #                         or ("formulary", "drug", "list", "tier", "coverage")
# #                     )
# #                     rx_result = query_insurance_benefits(
# #                         query=query,
# #                         topics=search_terms,
# #                         category="rx",
# #                         keywords=search_terms,
# #                         member_info=json.dumps(member_info) if member_info else "{}",
# #                     )
# #                     print(
# #                         f"[*] RX INDEX RESULT: {rx_result[:200] if rx_result else 'empty'}"
# #                     )

# #                     # Filter rx chunks to only keep entries where drug name
# #                     # matches the search keyword — removes category noise.
# #                     # Uses fuzzy matching to handle spelling mistakes in drug names
# #                     # e.g. "metfromin" matches "metformin", "flucanazole" matches "fluconazole"
# #                     #
# #                     # IMPORTANT: only apply this filter when the query actually
# #                     # contains a real drug name word. Requirement/category-style
# #                     # queries like "what preventive drugs do I have" have no drug
# #                     # name in them — applying the filter there would incorrectly
# #                     # discard correctly-scored results (e.g. ACA-flagged drugs
# #                     # that tools.py found via requirement matching, not name match).
# #                     query_has_real_drug_name = is_drug_name_query(query_words)
# #                     used_generic_fallback = not rx_keywords and not concept_terms

# #                     if used_generic_fallback:
# #                         # No specific drug name AND no meaningful concept word —
# #                         # this is a purely conceptual question (e.g. "what is
# #                         # formulary drugs?"). The RX section (if any) contains
# #                         # arbitrary drugs from the generic fallback search, not
# #                         # anything the member actually asked about — strip it
# #                         # so only INFO content remains for LLM synthesis.
# #                         import re as _re

# #                         rx_result = _re.sub(
# #                             r"### SECTION: RX.*?(?=### SECTION:|$)",
# #                             "",
# #                             rx_result or "",
# #                             flags=_re.DOTALL,
# #                         ).strip()
# #                         print("[*] NO RX KEYWORDS — stripped RX section, INFO only")

# #                     elif rx_result and query_has_real_drug_name:
# #                         import re as _re
# #                         from difflib import SequenceMatcher

# #                         def is_drug_match(keyword: str, drug_name: str) -> bool:
# #                             """
# #                             Returns True if keyword matches drug name exactly or closely.
# #                             Exact match: "metformin" in "metformin oral tablet 500 mg"
# #                             Fuzzy match: "metfromin" → 0.94 similarity → matches
# #                             Threshold 0.82 catches one/two letter typos without false positives.
# #                             """
# #                             kw = keyword.lower()
# #                             dn = drug_name.lower()
# #                             # Exact substring match first (fast path)
# #                             if kw in dn:
# #                                 return True
# #                             # Fuzzy match against each word in drug name
# #                             for word in dn.split():
# #                                 if len(word) > 4 and len(kw) > 4:
# #                                     score = SequenceMatcher(None, kw, word).ratio()
# #                                     if score >= 0.82:
# #                                         return True
# #                             return False

# #                         filtered_items = []
# #                         for item_match in _re.finditer(
# #                             r"Item \d+:\s*\{[^{}]+\}", rx_result, _re.DOTALL
# #                         ):
# #                             item_text = item_match.group()
# #                             drug_name_match = _re.search(
# #                                 r'"drug_name":\s*"([^"]+)"', item_text
# #                             )
# #                             if drug_name_match:
# #                                 drug_name = drug_name_match.group(1).lower()
# #                                 if any(
# #                                     is_drug_match(kw, drug_name) for kw in rx_keywords
# #                                 ):
# #                                     filtered_items.append(item_text)
# #                         if filtered_items:
# #                             rx_result = "### SECTION: RX\n\n" + "\n".join(
# #                                 filtered_items
# #                             )
# #                         else:
# #                             # No matching drug entries — strip RX section, keep INFO only
# #                             rx_result = _re.sub(
# #                                 r"### SECTION: RX.*?(?=### SECTION:|$)",
# #                                 "",
# #                                 rx_result or "",
# #                                 flags=_re.DOTALL,
# #                             ).strip()
# #                         print(
# #                             f"[*] RX FILTERED: {len(filtered_items)} matching drug entries"
# #                         )

# #                     # After filtering — check if only INFO remains (general formulary question)
# #                     # Return info directly without drug table or cost table
# #                     has_rx_section = "### SECTION: RX" in (rx_result or "")
# #                     has_info_section = "### SECTION: INFO" in (rx_result or "")

# #                     if has_info_section and not has_rx_section:
# #                         print(f"[*] RX INFO ONLY — sending to LLM for synthesis")
# #                         rx_plan_info = (
# #                             member_info.get("plans", {}).get("rx", {})
# #                             if member_info
# #                             else {}
# #                         )
# #                         rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
# #                         rx_variant = rx_plan_info.get("variant", "")
# #                         rx_source = (
# #                             f"{rx_plan_name} ({rx_variant})"
# #                             if rx_variant
# #                             else rx_plan_name
# #                         )

# #                         # Send to LLM to synthesize readable answer from raw INFO chunks
# #                         synthesis_messages = [
# #                             {
# #                                 "role": "system",
# #                                 "content": "You are a health insurance assistant. Answer the member's question using only the information provided. Be clear and concise. Do not add information not in the context.",
# #                             },
# #                             {
# #                                 "role": "user",
# #                                 "content": f"Member question: {query}\n\nContext from formulary booklet:\n{rx_result}",
# #                             },
# #                         ]
# #                         synthesized = llm_chat(
# #                             messages=synthesis_messages, max_tokens=500
# #                         )
# #                         return {
# #                             "answer": synthesized or rx_result,
# #                             "pages": [],
# #                             "source": rx_source,
# #                         }

# #                     # Step 2 — tier cost from medical index
# #                     # Use plan_type from member_info to decide medical vs sbc
# #                     plan_type = ""
# #                     if member_info and "plans" in member_info:
# #                         medical_plan = member_info["plans"].get("medical", {})
# #                         plan_type = medical_plan.get("plan_type", "").lower()

# #                     cost_category = "sbc" if "hsa" in plan_type else "medical"
# #                     cost_result = query_insurance_benefits(
# #                         query="prescription drug tier cost",
# #                         topics=("prescription drug",),
# #                         category=cost_category,
# #                         keywords=(
# #                             "prescription",
# #                             "drug",
# #                             "generic",
# #                             "brand",
# #                             "specialty",
# #                             "tier",
# #                         ),
# #                         member_info=json.dumps(member_info) if member_info else "{}",
# #                     )
# #                     print(f"[*] COST INDEX RESULT: {cost_result}")

# #                     # Step 3 — build two-section response and return directly
# #                     answer, rx_pages, cost_pages = build_rx_response(
# #                         rx_context=rx_result or "",
# #                         cost_context=cost_result or "",
# #                     )
# #                     all_pages = sorted(set(rx_pages + cost_pages))
# #                     print(f"[*] RX RESPONSE BUILT: {len(all_pages)} pages")

# #                     # Source shows both booklets with their respective page numbers
# #                     rx_plan_info = (
# #                         member_info.get("plans", {}).get("rx", {})
# #                         if member_info
# #                         else {}
# #                     )
# #                     rx_plan_name = rx_plan_info.get("plan", "Rx Formulary")
# #                     rx_variant = rx_plan_info.get("variant", "")
# #                     rx_source_name = (
# #                         f"{rx_plan_name} ({rx_variant})" if rx_variant else rx_plan_name
# #                     )

# #                     medical_plan = (
# #                         member_info.get("plans", {})
# #                         .get("medical", {})
# #                         .get("plan", "Medical Plan")
# #                         if member_info
# #                         else "Medical Plan"
# #                     )

# #                     # Build source string with per-booklet page numbers
# #                     source_parts = []
# #                     if rx_pages:
# #                         rx_page_str = ", ".join(str(p) for p in sorted(set(rx_pages)))
# #                         source_parts.append(f"{rx_source_name} | Pages {rx_page_str}")
# #                     if cost_pages:
# #                         cost_page_str = ", ".join(
# #                             str(p) for p in sorted(set(cost_pages))
# #                         )
# #                         source_parts.append(
# #                             f"{medical_plan} | Page {cost_page_str}"
# #                             if len(set(cost_pages)) == 1
# #                             else f"{medical_plan} | Pages {cost_page_str}"
# #                         )

# #                     source = (
# #                         " || ".join(source_parts) if source_parts else rx_source_name
# #                     )

# #                     return {
# #                         "answer": answer,
# #                         "pages": [],  # pages encoded in source string for rx queries
# #                         "source": source,
# #                     }

# #                 except Exception as e:
# #                     print(f"[!] RX RETRIEVAL FAILURE: {e}")
# #                     return {
# #                         "answer": "Unable to retrieve drug coverage information. Please try again.",
# #                         "pages": [],
# #                         "source": "",
# #                     }

# #             elif p_type in DIRECT_CATEGORIES:
# #                 # Direct call — bypass LLM tool decision entirely
# #                 print(
# #                     f"[DIRECT CALL]: query_insurance_benefits | {found_topics} | {p_type}"
# #                 )
# #                 try:
# #                     tool_result = query_insurance_benefits(
# #                         query=query,
# #                         topics=tuple(found_topics),
# #                         category=p_type,
# #                         keywords=tuple(keywords),
# #                         member_info=json.dumps(member_info) if member_info else "{}",
# #                     )
# #                     print(
# #                         f"[*] TOOL RESULT after calling query_insurance_benefits : {tool_result}"
# #                     )
# #                 except Exception as e:
# #                     print(f"[!] RETRIEVAL FAILURE: {e}")
# #                     tool_result = "RETRIEVAL ERROR"

# #             else:
# #                 # LLM tool orchestration — for unknown/future categories
# #                 # Works with both Ollama (dev) and OpenAI-compatible gateways (prod)
# #                 print(f"[LLM TOOL CALL]: category={p_type} — using LLM orchestration")
# #                 tool_response = llm_chat_with_tools(
# #                     messages=messages,
# #                     tools=tools,
# #                 )
# #                 msg_content = tool_response.get("content", "")
# #                 tool_calls = tool_response.get("tool_calls", [])

# #                 if tool_calls:
# #                     tool = tool_calls[0]
# #                     function_name = tool["function"]["name"]
# #                     arguments = tool["function"]["arguments"]
# #                     print(f"[TOOL CALL]: {function_name} | {found_topics} | {p_type}")
# #                     try:
# #                         if function_name == "query_insurance_benefits":
# #                             tool_result = query_insurance_benefits(
# #                                 query=arguments.get("query", query),
# #                                 topics=tuple(found_topics),
# #                                 category=p_type,
# #                                 keywords=tuple(keywords),
# #                                 member_info=(
# #                                     json.dumps(member_info) if member_info else "{}"
# #                                 ),
# #                             )
# #                         print(
# #                             f"[*] TOOL RESULT after calling query_insurance_benefits : {tool_result}"
# #                         )
# #                     except Exception as e:
# #                         print(f"[!] TOOL FAILURE: {e}")
# #                         tool_result = "RETRIEVAL ERROR"
# #                 else:
# #                     print("[!] No tool call from LLM — forcing direct retrieval")
# #                     try:
# #                         tool_result = query_insurance_benefits(
# #                             query=query,
# #                             topics=tuple(found_topics),
# #                             category=p_type,
# #                             keywords=tuple(keywords),
# #                             member_info=(
# #                                 json.dumps(member_info) if member_info else "{}"
# #                             ),
# #                         )
# #                     except Exception as e:
# #                         print(f"[!] RETRIEVAL FAILURE: {e}")
# #                         tool_result = "RETRIEVAL ERROR"

# #             # ============================================================
# #             # STEP 3 — KEYWORD FILTERING (HEADER-AWARE)
# #             # ============================================================
# #             if tool_result and tool_result != "RETRIEVAL ERROR" and keywords:

# #                 section_blocks = re.split(r"(### SECTION: [A-Z]+)", tool_result)
# #                 rebuilt = []
# #                 current_header = None

# #                 for block in section_blocks:
# #                     block = block.strip()
# #                     if not block:
# #                         continue

# #                     # SECTION HEADER
# #                     if block.startswith("### SECTION:"):
# #                         current_header = block
# #                         rebuilt.append(block)
# #                         continue

# #                     # COST and INFO sections → NEVER FILTER
# #                     if current_header in ("### SECTION: COST", "### SECTION: INFO"):
# #                         rebuilt.append(block)
# #                         continue

# #                     # Other sections → keyword filter
# #                     if any(k.lower() in block.lower() for k in keywords):
# #                         rebuilt.append(block)

# #                 tool_result = "\n\n".join(rebuilt)

# #             # ============================================================
# #             # 🔥 STEP 4 — CLEAN CONTEXT (CRITICAL)
# #             # ============================================================
# #             def trim_context(text, max_chars=8000):
# #                 if not text:
# #                     return ""

# #                 if len(text) <= max_chars:
# #                     return text

# #                 cut = text[:max_chars]
# #                 last_break = cut.rfind("\n\n")

# #                 if last_break != -1:
# #                     return cut[:last_break]

# #                 return cut

# #             # ============================================================
# #             # STEP 5 — FINAL ANSWER (NO TOOLS)
# #             # ============================================================

# #             # 🔥 CLEAN CONTEXT (single trim only)
# #             clean_context = trim_context(tool_result or "", 8000)
# #             print(f"[*] CLEAN CONTEXT FOR FINAL PROCESSING : {clean_context}")

# #             # ============================================================
# #             # STEP 6 — SETUP THE CACHE
# #             # ============================================================
# #             # After getting clean context store the value in cache (24h TTL)
# #             await set_query_result(cache_key, clean_context)
# #         sections = []

# #         if "### SECTION: QA" in clean_context:
# #             sections.append("qa")

# #         if "### SECTION: COST" in clean_context:
# #             sections.append("cost")

# #         if "### SECTION: EXCLUDED" in clean_context:
# #             sections.append("excluded")

# #         if "### SECTION: INFO" in clean_context:
# #             sections.append("info")

# #         print(f"[*] DETECTED SECTIONS: {sections}")

# #         # ── Define table builders here so they're available in all branches ──
# #         # --------------------------------------------------------
# #         # 🔥 MULTI-SECTION → try no-LLM first, fall back to LLM
# #         # --------------------------------------------------------
# #         if len(sections) > 1:

# #             if set(sections) == {"cost", "info"}:
# #                 print("[*] COST+INFO → BUILDING BOTH TABLES (NO LLM)")

# #                 # Extract each section's context separately
# #                 def split_section(ctx, name):
# #                     marker = f"### SECTION: {name.upper()}"
# #                     start = ctx.find(marker)
# #                     if start == -1:
# #                         return ctx
# #                     next_marker = ctx.find("### SECTION:", start + len(marker))
# #                     return ctx[start:next_marker] if next_marker != -1 else ctx[start:]

# #                 cost_ctx = split_section(clean_context, "cost")
# #                 info_ctx = split_section(clean_context, "info")

# #                 # Suppress COST for exclusion queries — "what's not covered?"
# #                 # is a pure INFO question; showing covered services is misleading.
# #                 _exclusion_query = any(
# #                     t in found_topics
# #                     for t in [
# #                         "dental exclusions",
# #                         "exclusions and limitations",
# #                         "vision exclusions",
# #                     ]
# #                 )
# #                 if _exclusion_query:
# #                     cost_table, cost_pages = None, []
# #                 else:
# #                     cost_table, cost_pages = build_cost_table(
# #                         cost_ctx, query, keywords, found_topics
# #                     )

# #                 # Suppress INFO for dental pure-cost queries (copay, x-ray)
# #                 # to prevent unrelated prose (e.g. "Dental Emergency") appearing.
# #                 # Only applies to dental — medical/vision x-ray queries need INFO.
# #                 _SERVICE_SPECIFIC_KEYS = {
# #                     "radiographic",
# #                     "bitewing",
# #                     "periapical",
# #                     "panoramic",
# #                     "prophylaxis",
# #                     "sealant",
# #                     "fluoride",
# #                     "varnish",
# #                     "amalgam",
# #                     "composite",
# #                     "endodontic",
# #                     "canal",
# #                     "pontic",
# #                     "extraction",
# #                     "scaling",
# #                     "debridement",
# #                     "gingivectomy",
# #                     "prosthodontics",
# #                 }
# #                 _pure_cost_query = (p_type == "dental") and (
# #                     any(
# #                         w in query.lower()
# #                         for w in ["copay", "x-ray", "xray", "radiograph"]
# #                     )
# #                     or any(kw in _SERVICE_SPECIFIC_KEYS for kw in keywords)
# #                 )
# #                 if _pure_cost_query:
# #                     info_table, info_pages = None, []
# #                 else:
# #                     info_table, info_pages = build_info_response(
# #                         info_ctx, query, keywords
# #                     )

# #                 if cost_table not in (None, "__USE_LLM__") and info_table not in (
# #                     None,
# #                     "__USE_LLM__",
# #                 ):
# #                     summary = generate_summary(
# #                         query,
# #                         info_table=info_table,
# #                         keywords=keywords,
# #                     )
# #                     combined = cost_table.rstrip("\n") + "\n\n" + info_table
# #                     answer = f"{summary}\n\n{combined}" if summary else combined
# #                     pages = _add_offset(sorted(set(cost_pages + info_pages)))
# #                     print("[*] COST+INFO COMBINED TABLE → RETURNING (NO LLM)")
# #                     return {
# #                         "answer": answer,
# #                         "category": p_type,
# #                         "pages": pages,
# #                         "source": _booklet_name,
# #                     }

# #                 elif cost_table not in (None, "__USE_LLM__"):
# #                     # Cost-only — no summary (table is self-explanatory)
# #                     print("[*] COST+INFO → RETURNING COST TABLE ONLY (NO LLM)")
# #                     return {
# #                         "answer": cost_table,
# #                         "category": p_type,
# #                         "pages": _add_offset(cost_pages),
# #                         "source": _booklet_name,
# #                     }

# #                 elif info_table not in (None, "__USE_LLM__"):
# #                     summary = generate_summary(
# #                         query,
# #                         info_table=info_table,
# #                         keywords=keywords,
# #                     )
# #                     answer = f"{summary}\n\n{info_table}" if summary else info_table
# #                     print("[*] COST+INFO → RETURNING INFO TABLE ONLY (NO LLM)")
# #                     return {
# #                         "answer": answer,
# #                         "category": p_type,
# #                         "pages": _add_offset(info_pages),
# #                         "source": _booklet_name,
# #                     }

# #                 # ── Tier 2: cheap mini-LLM (~50-100 tokens) ──────────────────
# #                 # Scoring failed for both — LLM extracts the canonical benefit
# #                 # name and intent, then re-scores. 10-20x cheaper than full LLM.
# #                 print("[*] SCORING FAILED → MINI-LLM CLASSIFICATION (TIER 2)")
# #                 try:
# #                     mini_prompt = (
# #                         f"Extract from the query: (1) the benefit/service name as it "
# #                         f"appears in a benefits booklet, (2) intent: cost, info, or both.\n"
# #                         f'Return ONLY JSON: {{"benefit": "...", "intent": "cost|info|both"}}\n'
# #                         f"Examples:\n"
# #                         f'  "what i need to pay for my PCP visit" → {{"benefit": "Professional Visits And Services", "intent": "cost"}}\n'
# #                         f'  "what is prior authorization" → {{"benefit": "Prior Authorization", "intent": "info"}}\n'
# #                         f'  "blood products coverage and cost" → {{"benefit": "Blood Products And Services", "intent": "both"}}\n'
# #                         f"Query: {query}"
# #                     )
# #                     from utility.llm import llm_generate
# #                     import json as _json

# #                     mini_content = llm_generate(prompt=mini_prompt, max_tokens=60)
# #                     mini_data = _json.loads(mini_content) if mini_content else {}
# #                     benefit_name = mini_data.get("benefit", "")
# #                     intent = mini_data.get("intent", "both")
# #                     print(f"[*] MINI-LLM: benefit={benefit_name!r}  intent={intent}")

# #                     if benefit_name:
# #                         if intent in ("cost", "both"):
# #                             ct2, cp2 = build_cost_table(
# #                                 cost_ctx, benefit_name, [benefit_name], found_topics
# #                             )
# #                             if ct2 not in (None, "__USE_LLM__"):
# #                                 if intent == "cost":
# #                                     return {
# #                                         "answer": ct2,
# #                                         "pages": _add_offset(cp2),
# #                                         "source": _booklet_name,
# #                                     }
# #                                 it2, ip2 = build_info_response(
# #                                     info_ctx, benefit_name, [benefit_name]
# #                                 )
# #                                 if it2 not in (None, "__USE_LLM__"):
# #                                     return {
# #                                         "answer": ct2.rstrip("\n") + "\n\n" + it2,
# #                                         "pages": _add_offset(sorted(set(cp2 + ip2))),
# #                                         "source": _booklet_name,
# #                                     }
# #                                 return {
# #                                     "answer": ct2,
# #                                     "pages": _add_offset(cp2),
# #                                     "source": _booklet_name,
# #                                 }
# #                         if intent in ("info", "both"):
# #                             it2, ip2 = build_info_response(
# #                                 info_ctx, benefit_name, [benefit_name]
# #                             )
# #                             if it2 not in (None, "__USE_LLM__"):
# #                                 return {
# #                                     "answer": it2,
# #                                     "pages": _add_offset(ip2),
# #                                     "source": _booklet_name,
# #                                 }
# #                 except Exception as e:
# #                     print(f"[*] MINI-LLM FAILED: {e}")
# #                 # fallthrough to full LLM (Tier 3)

# #             else:
# #                 print("[*] MULTI SECTION → USING LLM")

# #         # --------------------------------------------------------
# #         # 🔥 SINGLE SECTION → OPTIMIZE
# #         # --------------------------------------------------------
# #         elif len(sections) == 1:
# #             if sections[0] == "cost":
# #                 print("[*] COST DETECTED → TRYING PARSER")
# #                 final_answer, final_pages = build_cost_table(
# #                     clean_context, query, keywords, found_topics
# #                 )
# #                 if final_answer == "__USE_LLM__":
# #                     print("[*] SWITCHING TO LLM DUE TO NOISE")
# #                 elif final_answer:
# #                     # Cost-only — no summary (table is self-explanatory)
# #                     print("[*] PARSER SUCCESS → RETURNING TABLE")
# #                     return {
# #                         "answer": final_answer,
# #                         "category": p_type,
# #                         "pages": _add_offset(final_pages),
# #                         "source": _booklet_name,
# #                     }

# #             elif sections[0] == "info":
# #                 print("[*] INFO DETECTED → BUILDING INFO TABLE")
# #                 final_answer, final_pages = build_info_response(
# #                     clean_context, query, keywords
# #                 )
# #                 if final_answer == "__USE_LLM__":
# #                     print("[*] INFO FALLBACK TO LLM")
# #                 elif final_answer:
# #                     print("[*] INFO PARSER SUCCESS → RETURNING TABLE")
# #                     return {
# #                         "answer": final_answer,
# #                         "pages": _add_offset(final_pages),
# #                         "source": _booklet_name,
# #                     }

# #         # --------------------------------------------------------
# #         # 🔥 FALLBACK
# #         # --------------------------------------------------------
# #         else:
# #             print("[*] NO SECTION DETECTED → USING LLM")

# #         print(f"[*] CLEAN CONTENT SENDING TO LLM : {clean_context}")
# #         if clean_context.strip() == "":
# #             print("[!] EMPTY CONTEXT → SKIPPING LLM")
# #             return {"answer": "No relevant information found."}

# #         final_messages = [
# #             {
# #                 "role": "system",
# #                 "content": generate_ironclad_instruction(),  # 🔥 USE YOUR STRONG PROMPT
# #             },
# #             {
# #                 "role": "user",
# #                 "content": f"""
# #         User Question:
# #         {query}

# #         Context:
# #         {clean_context}
# #         """,
# #             },
# #         ]

# #         final_answer = llm_chat(
# #             messages=final_messages,
# #             temperature=0.0,
# #         )

# #         # ============================================================
# #         # FINAL RESPONSE (STRICT CONTRACT WITH UI)
# #         # ============================================================

# #         # ============================================================
# #         # 🔥 FORCE STRING (HARD GUARANTEE)
# #         # ============================================================

# #         if not isinstance(final_answer, str):
# #             try:
# #                 final_answer = json.dumps(final_answer)
# #             except Exception:
# #                 final_answer = str(final_answer)

# #         final_answer = (final_answer or "").strip()

# #         # ============================================================
# #         # 🔥 IRON CURTAIN (KEEP — GOOD LOGIC)
# #         # ============================================================

# #         if "|" in final_answer:
# #             start_index = final_answer.find("|")
# #             end_index = final_answer.rfind("|")

# #             if end_index > start_index:
# #                 final_answer = final_answer[start_index : end_index + 1].strip()

# #         # ============================================================
# #         # 🔥 FALLBACK (ONLY IF EMPTY OR BAD OUTPUT)
# #         # ============================================================
# #         if not final_answer or "|" not in final_answer:
# #             print("[!] BAD OR EMPTY FINAL ANSWER")

# #             final_answer = "No relevant information found."
# #         # ============================================================
# #         # 🔥 FINAL SAFETY (ABSOLUTE GUARANTEE FOR UI)
# #         # ============================================================

# #         if not isinstance(final_answer, str):
# #             final_answer = str(final_answer)

# #         # 🔥 THIS IS THE MOST IMPORTANT LINE
# #         final_answer = final_answer or "No data found."

# #         print(f"[FINAL ANSWER TYPE]: {type(final_answer)}")
# #         print(f"[FINAL ANSWER LENGTH]: {len(final_answer)}")
# #         print(f"Final answer = {final_answer[:500]}")

# #         # Tier 3: parse pages from all chunks in context (LLM used them all)
# #         import re as _re

# #         _t3_pages = []
# #         for _item in _re.split(r"Item \d+:", clean_context):
# #             _m = _re.search(r'"page_number"\s*:\s*(\d+)', _item)
# #             if _m:
# #                 _pg = int(_m.group(1))
# #                 if _pg > 0:
# #                     _t3_pages.append(_pg - _page_offset)

# #         return {
# #             "answer": final_answer,
# #             "category": p_type,
# #             "pages": sorted(set(_t3_pages)),
# #             "source": _booklet_name,
# #         }

# #     except Exception as e:
# #         print(f"❌ SYNTHESIS ERROR: {e}")
# #         return f"⚠️ Client Logic Error: {str(e)}"
