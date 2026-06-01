import re
import json
import os
import ollama

from utility.utils import flatten_message_content, smart_match
from utility.category import (
    detect_category,
    detect_category_rule_based,
    detect_category_from_history,
    extract_user_queries,
)
from utility.topic_resolver import resolve_insurance_topic, NOISE_WORDS
from utility.response_builder import (
    build_cost_table,
    build_info_response,
    generate_ironclad_instruction,
)
from utility.prompts import (
    TOPIC_EXTRACTION_PROMPT,
    GUIDANCE_NO_CATEGORY,
    GUIDANCE_MEDICAL_VAGUE,
    GUIDANCE_DENTAL_VAGUE,
    GUIDANCE_VISION_VAGUE,
)

LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

TOOL_RESULT_CACHE = {}
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
        response = ollama.chat(
            model=LOCAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_predict": 40},
        )
        summary = response["message"]["content"].strip()

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

    try:
        from insurance_mcp.server import query_insurance_benefits

        # --- 2. CONTEXT MERGING (MEMORY) ---
        # Limit to last 5 turns (10 messages) — avoids stale context polluting
        # topic resolution and keyword extraction for long conversations.
        recent_history = " ".join(
            [flatten_message_content(m["content"]) for m in history[-10:]]
        )
        query_lower = query.lower()

        # Clean words for surgical matching
        query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]

        print(f"[*] Query Words for Matching: {query_words}")
        print("[DEBUG] urgent match:", smart_match("urgent", query_words, query_lower))

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
        # ========================================================================
        if not found_topics:
            print(f"[*] TURNING TO LLM TO FIND THE TOPIC FROM QUERY : {query}")
            topic_prompt = TOPIC_EXTRACTION_PROMPT

            llm_messages = [
                {"role": "system", "content": topic_prompt},
                {"role": "user", "content": f"User Query: {query_lower}"},
            ]

            # ============================================================
            # 🔥 LLM TOPIC + KEYWORD EXTRACTION (PRODUCTION SAFE)
            # ============================================================

            llm_response = ollama.chat(
                model=LOCAL_MODEL,
                messages=llm_messages,
                format="json",
                options={"temperature": 0.0, "num_ctx": 8192},
            )

            raw_content = llm_response["message"].get("content", "")
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

        cached_context = TOOL_RESULT_CACHE.get(cache_key)
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
            # STEP 1 — TOOL CALL
            # ============================================================
            resp = ollama.chat(
                model=LOCAL_MODEL,
                messages=messages,
                tools=tools,
                options={"temperature": 0},
            )

            msg = resp["message"]
            tool_calls = msg.get("tool_calls", [])

            tool_result = None

            # ============================================================
            # STEP 2 — EXECUTE TOOL
            # ============================================================
            # Ensure keywords exists (from your LLM detection block). Default to empty list if not.
            if tool_calls:
                tool = tool_calls[0]
                function_name = tool["function"]["name"]
                arguments = tool["function"]["arguments"]

                print(f"[TOOL CALL]: {function_name} | {found_topics} | {p_type}")

                try:
                    if function_name == "query_insurance_benefits":
                        tool_result = query_insurance_benefits(
                            query=arguments.get("query", query),
                            topics=found_topics,
                            category=p_type,
                            keywords=keywords,
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
                print("[!] No tool call — forcing retrieval")

                try:
                    tool_result = query_insurance_benefits(
                        query=query,
                        topics=found_topics,
                        category=p_type,
                        keywords=keywords,
                        member_info=json.dumps(member_info) if member_info else "{}",
                    )
                except Exception as e:
                    print(f"[!] TOOL FAILURE: {e}")
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
                """
                Section-aware trim — never cuts a section off mid-item.
                Keeps complete items from COST and INFO sections separately,
                trimming the number of items per section rather than raw chars.
                Falls back to char trim only when no section structure is found.
                """
                if not text or len(text) <= max_chars:
                    return text

                import re as _re

                # Split into named sections
                section_re = _re.compile(r"(### SECTION: \w+\s*\n)")
                parts = section_re.split(text)
                # parts alternates: [preamble, header1, body1, header2, body2, ...]

                sections = {}
                i = 1
                while i < len(parts) - 1:
                    header = parts[i].strip()  # e.g. "### SECTION: COST"
                    body = parts[i + 1] if i + 1 < len(parts) else ""
                    name = header.replace("### SECTION:", "").strip().lower()
                    sections[name] = (header, body)
                    i += 2

                if not sections:
                    # No section structure — fall back to original char trim
                    cut = text[:max_chars]
                    last_break = cut.rfind("\n\n")
                    return cut[:last_break] if last_break != -1 else cut

                # Budget: give COST section up to 3000 chars, INFO gets the rest
                cost_budget = min(3000, max_chars // 2)
                info_budget = max_chars - cost_budget

                # Get keywords from outer scope for relevance sorting
                _trim_keywords = keywords if "keywords" in dir() else []

                result_parts = []
                for name, (header, body) in sections.items():
                    budget = cost_budget if name == "cost" else info_budget

                    items = _re.split(r"(?=Item \d+:)", body.strip())
                    items = [it for it in items if it.strip()]

                    # For INFO section: sort keyword-matching items first
                    if name == "info" and _trim_keywords:

                        def _score(item):
                            item_lower = item.lower()
                            return sum(
                                1 for kw in _trim_keywords if kw.lower() in item_lower
                            )

                        items = sorted(items, key=_score, reverse=True)

                    kept = []
                    total = len(header) + 2
                    for item in items:
                        if total + len(item) > budget and kept:
                            break
                        kept.append(item)
                        total += len(item)

                    section_text = f"{header}\n\n" + "\n".join(kept)
                    result_parts.append(section_text)

                return "\n\n".join(result_parts)

            # ============================================================
            # STEP 5 — FINAL ANSWER (NO TOOLS)
            # ============================================================

            # 🔥 CLEAN CONTEXT (single trim only)
            clean_context = trim_context(tool_result or "", 8000)
            print(f"[*] CLEAN CONTEXT FOR FINAL PROCESSING : {clean_context}")

            # ============================================================
            # STEP 6 — SETUP THE CACHE
            # ============================================================
            # After getting clean context store the value in cache
            TOOL_RESULT_CACHE[cache_key] = clean_context
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
                    import ollama as _ollama, json as _json, os as _os

                    mini_resp = _ollama.generate(
                        model=_os.getenv("OLLAMA_MODEL", "llama3.1"),
                        prompt=mini_prompt,
                        format="json",
                        options={"temperature": 0, "num_predict": 60},
                    )
                    mini_data = _json.loads(mini_resp["response"])
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

        final_resp = ollama.chat(
            model=LOCAL_MODEL,
            messages=final_messages,
            options={"temperature": 0},
        )

        # ============================================================
        # FINAL RESPONSE (STRICT CONTRACT WITH UI)
        # ============================================================

        msg = final_resp.get("message", {})

        # 🔥 SAFELY EXTRACT CONTENT
        if isinstance(msg, dict):
            final_answer = msg.get("content", "")
        else:
            final_answer = str(msg)

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
