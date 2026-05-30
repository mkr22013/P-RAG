import re
import json
import os
import ollama

from utility.utils import flatten_message_content, smart_match
from utility.category import (
    detect_category,
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
    BENEFIT_SELECTION_PROMPT,
    MEDICAL_DETAIL_PROMPT,
    DENTAL_DETAIL_PROMPT,
    VISION_DETAIL_PROMPT,
    TOPIC_EXTRACTION_PROMPT,
)

LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

TOOL_RESULT_CACHE = {}
last_type_global = None


async def get_ai_response(query, history):
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

        benefit_prompt = BENEFIT_SELECTION_PROMPT
        if not p_type:
            # The prompt for member benefit selection
            # Example of how to use it
            return benefit_prompt

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
        # PROMPT: Category is Medical, but specific service is missing
        medical_detail_prompt = MEDICAL_DETAIL_PROMPT

        # PROMPT: Category is Dental, but specific service is missing
        dental_detail_prompt = DENTAL_DETAIL_PROMPT

        # PROMPT: Category is Vision, but specific service is missing
        vision_detail_prompt = VISION_DETAIL_PROMPT

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

            # Search recent_history for the last occurrence of any valid keyword
            # We look for the word itself or the word inside a Markdown table | PCP |
            user_queries = extract_user_queries(recent_history)

            for past_query in reversed(user_queries):
                print(f"[*] CHECKING PAST QUERY: {past_query}")

                history_word_list = [
                    re.sub(r"[^\w\s]", "", w) for w in past_query.split()
                ]

                history_topics = resolve_insurance_topic(history_word_list, past_query)

                if history_topics:
                    print(f"[*] RECOVERED TOPIC FROM HISTORY: {history_topics}")
                    found_topics.extend(history_topics)
                    break

        print(f"Final topic found from query : {found_topics}")
        last_type_global = p_type  # Now setting up the last type here as now we have category and topic and keywords
        # Once we reach here it means topic did not available
        if len(found_topics) == 0 and len(keywords) == 0:
            if p_type.lower() == "medical":
                return medical_detail_prompt
            elif p_type.lower() == "dental":
                return dental_detail_prompt
            elif p_type.lower() == "vision":
                return vision_detail_prompt
            else:
                return "I can help with Medical, Dental, or Vision benefits! Which would you like to explore?"

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
                            topics=found_topics,  # 🔥 FORCE FROM BACKEND
                            category=p_type,
                            keywords=keywords,
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
                    cost_table = None
                else:
                    cost_table = build_cost_table(
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
                    info_table = None
                else:
                    info_table = build_info_response(info_ctx, query, keywords)

                if cost_table not in (None, "__USE_LLM__") and info_table not in (
                    None,
                    "__USE_LLM__",
                ):
                    combined = cost_table.rstrip("\n") + "\n\n" + info_table
                    print("[*] COST+INFO COMBINED TABLE → RETURNING (NO LLM)")
                    return {"answer": combined}

                elif cost_table not in (None, "__USE_LLM__"):
                    print("[*] COST+INFO → RETURNING COST TABLE ONLY (NO LLM)")
                    return {"answer": cost_table}

                elif info_table not in (None, "__USE_LLM__"):
                    print("[*] COST+INFO → RETURNING INFO TABLE ONLY (NO LLM)")
                    return {"answer": info_table}

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
                            ct2 = build_cost_table(
                                cost_ctx, benefit_name, [benefit_name], found_topics
                            )
                            if ct2 not in (None, "__USE_LLM__"):
                                if intent == "cost":
                                    return {"answer": ct2}
                                it2 = build_info_response(
                                    info_ctx, benefit_name, [benefit_name]
                                )
                                if it2 not in (None, "__USE_LLM__"):
                                    return {"answer": ct2.rstrip("\n") + "\n\n" + it2}
                                return {"answer": ct2}
                        if intent in ("info", "both"):
                            it2 = build_info_response(
                                info_ctx, benefit_name, [benefit_name]
                            )
                            if it2 not in (None, "__USE_LLM__"):
                                return {"answer": it2}
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
                final_answer = build_cost_table(
                    clean_context, query, keywords, found_topics
                )
                if final_answer == "__USE_LLM__":
                    print("[*] SWITCHING TO LLM DUE TO NOISE")
                elif final_answer:
                    print("[*] PARSER SUCCESS → RETURNING TABLE")
                    return {"answer": final_answer}

            elif sections[0] == "info":
                print("[*] INFO DETECTED → BUILDING INFO TABLE")
                final_answer = build_info_response(clean_context, query, keywords)
                if final_answer == "__USE_LLM__":
                    print("[*] INFO FALLBACK TO LLM")
                elif final_answer:
                    print("[*] INFO PARSER SUCCESS → RETURNING TABLE")
                    return {"answer": final_answer}

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

        return {"answer": final_answer}

    except Exception as e:
        print(f"❌ SYNTHESIS ERROR: {e}")
        return f"⚠️ Client Logic Error: {str(e)}"


##====================================================Previously working code before refactor====================================================##

# # import os, ollama, re, json

# # from dotenv import load_dotenv
# # from datetime import datetime
# # from difflib import SequenceMatcher

# # load_dotenv()
# # LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

# # # --- GLOBAL RAM CACHE (Persistent for the session) ---
# # TOOL_RESULT_CACHE = {}
# # # --- GLOBAL SESSION STATE ---
# # CURRENT_YEAR_INT = datetime.now().year
# # last_type_global = None


# # def fuzzy_match(a, b, threshold=0.8):
# #     return SequenceMatcher(None, a, b).ratio() >= threshold


# # def smart_match(term, query_words, query_lower):
# #     """
# #     Priority:
# #     1. Exact phrase match
# #     2. Exact word match
# #     3. Fuzzy match (safe)
# #     """
# #     term = term.lower()

# #     # 1. Phrase match
# #     if " " in term:
# #         return term in query_lower

# #     # 2. Exact word match
# #     if term in query_words:
# #         return True

# #     # 3. Fuzzy match (safe)
# #     for w in query_words:
# #         if len(w) >= 4 and len(term) >= 4:
# #             if fuzzy_match(w, term):
# #                 return True

# #     return False


# # def resolve_insurance_topic(query_words, full_query_text, p_type=None):
# #     """
# #     Resolves topics and extracts clean keywords even with typos.
# #     """
# #     topics = []
# #     extracted_keywords = []
# #     query_lower = full_query_text.lower()

# #     def add_keyword(phrase):
# #         phrase = phrase.lower().strip()
# #         if phrase not in extracted_keywords:
# #             extracted_keywords.append(phrase)

# #     # 1. DEDUCTIBLE / OOP
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["deductible", "limit", "oop", "coinsurance"]
# #     ):
# #         # Check for out-of-pocket specifically
# #         if any(
# #             smart_match(p, query_words, query_lower)
# #             for p in ["out of pocket", "out-of-pocket", "oop", "pocket"]
# #         ):
# #             topics.append("out-of-pocket")
# #             add_keyword("out of pocket")
# #         else:
# #             topics.append("deductible")
# #             add_keyword("deductible")

# #     # 2. URGENT vs EMERGENCY
# #     if smart_match("urgent care", query_words, query_lower) or (
# #         smart_match("urgent", query_words, query_lower)
# #         and any(
# #             smart_match(w, query_words, query_lower)
# #             for w in ["care", "clinic", "center"]
# #         )
# #     ):
# #         topics.append("urgent care")
# #         add_keyword("urgent care")
# #     elif (
# #         re.search(r"\ber\b", query_lower)
# #         or smart_match("emergency", query_words, query_lower)
# #         or smart_match("ambulance", query_words, query_lower)
# #     ):
# #         topics.append("emergency")
# #         if smart_match("emergency room", query_words, query_lower):
# #             add_keyword("emergency room")
# #         else:
# #             add_keyword("emergency")

# #     # 3. DIAGNOSTIC / IMAGING (Fuzzy Safe)
# #     _dental_xray_terms = ["panoramic", "bitewing", "periapical"]
# #     _is_dental_xray = any(
# #         smart_match(w, query_words, query_lower) for w in _dental_xray_terms
# #     )
# #     if (
# #         any(
# #             smart_match(w, query_words, query_lower)
# #             for w in ["xray", "blood", "diagnostic"]
# #         )
# #         and not re.search(r"\bclass\s+[i123]", query_lower)
# #         and not _is_dental_xray
# #     ):
# #         topics.append("diagnostic")
# #         if smart_match("blood", query_words, query_lower):
# #             add_keyword("blood work")
# #         if smart_match("xray", query_words, query_lower) or "x-ray" in query_lower:
# #             add_keyword("x-ray")

# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["mri", "scan", "imaging", "ct"]
# #     ):
# #         topics.append("imaging")
# #         if smart_match("mri", query_words, query_lower):
# #             add_keyword("mri")
# #         if smart_match("ct", query_words, query_lower):
# #             add_keyword("ct scan")

# #     # 4. NETWORK
# #     _OUT_OF_NETWORK_TERMS = [
# #         "out of network",
# #         "out-of-network",
# #         "out of area",
# #         "out-of-area",
# #         "non participating",
# #         "non-participating",
# #         "nonparticipating",
# #     ]
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in [
# #             "network",
# #             "provider",
# #             "balance billing",
# #             "nonparticipating",
# #             "non-participating",
# #             "non participating",
# #         ]
# #     ):
# #         topics.append("network")
# #         if any(smart_match(w, query_words, query_lower) for w in _OUT_OF_NETWORK_TERMS):
# #             # Single-word keywords so server INFO loop can match against event names
# #             # and chunk_keywords. "Exclusions And Limitations" and "Referrals" entries
# #             # both contain "exclusions"/"participating"/"limitations" as standalone words.
# #             add_keyword("exclusions")
# #             add_keyword("participating")
# #             add_keyword("limitations")
# #             add_keyword("referrals")
# #             # Suppress COST for out-of-network queries (no D-codes apply)
# #             if "dental exclusions" not in topics:
# #                 topics.append("dental exclusions")

# #     # 5. DENTAL
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in [
# #             "dental",
# #             "tooth",
# #             "teeth",
# #             "gum",
# #             "cavity",
# #             "filling",
# #             "crown",
# #             "denture",
# #             "molar",
# #             "root canal",
# #             "canal",
# #             "implant",
# #             "tmj",
# #             "jaw",
# #             "bridge",
# #             "veneer",
# #             "onlay",
# #             "inlay",
# #             "orthodontic",
# #             "orthodontia",
# #             "panoramic",
# #             "sealant",
# #             "fluoride",
# #             "prophylaxis",
# #             "class i",
# #             "class ii",
# #             "class iii",
# #             "class 1",
# #             "class 2",
# #             "class 3",
# #             "apicoectomy",
# #             "retrograde",
# #         ]
# #     ) or re.search(r"\bclass\s+[i123]", query_lower):
# #         CLASS_I_TERMS = [
# #             "cleaning",
# #             "prophylaxis",
# #             "fluoride",
# #             "sealant",
# #             "preventive",
# #             "oral exam",
# #             "xray",
# #             "panoramic",
# #         ]
# #         CLASS_II_TERMS = [
# #             "filling",
# #             "extraction",
# #             "root canal",
# #             "canal",
# #             "pulp",
# #             "periodontic",
# #             "scaling",
# #             "basic",
# #         ]
# #         CLASS_III_TERMS = [
# #             "crown",
# #             "bridge",
# #             "denture",
# #             "implant",
# #             "veneer",
# #             "onlay",
# #             "inlay",
# #             "major",
# #         ]
# #         CLASS_I_RE = re.compile(r"\bclass\s+(i\b(?!i)|1\b|one\b)", re.I)
# #         CLASS_II_RE = re.compile(r"\bclass\s+(ii\b(?!i)|2\b|two\b)", re.I)
# #         CLASS_III_RE = re.compile(r"\bclass\s+(iii\b|3\b|three\b)", re.I)
# #         ORTHO_TERMS = ["braces", "orthodontic", "orthodontist", "retainer"]
# #         TMJ_TERMS = ["tmj", "jaw", "temporomandibular"]
# #         LIMIT_TERMS = [
# #             "annual max",
# #             "annual maximum",
# #             "benefit maximum",
# #             "dental deductible",
# #         ]

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_I_TERMS
# #         ) or CLASS_I_RE.search(query_lower):
# #             topics.append("class i")
# #             add_keyword("class i diagnostic and preventive services")
# #             # Willamette: also add D-code compatible keyword
# #             add_keyword("diagnostic and preventive")
# #             for term in CLASS_I_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         # Dental procedure synonyms — maps plain-language terms to
# #         # D-code service names for Willamette index compatibility
# #         _DENTAL_SYNONYMS = {
# #             "cleaning": ["prophylaxis"],
# #             "filling": ["amalgam", "composite", "restorative"],
# #             "extraction": ["erupted", "impacted"],
# #             "x-ray": ["radiographic", "bitewing", "periapical"],
# #             "xray": ["radiographic", "bitewing", "periapical"],
# #             "gum": ["periodontic", "gingivectomy"],
# #             "denture": ["removable"],
# #             "implant": ["endosteal"],
# #             "fluoride": ["varnish"],
# #             "bridge": ["pontic", "prosthodontics"],
# #             "crown": ["porcelain", "stainless"],
# #         }
# #         for term, synonyms in _DENTAL_SYNONYMS.items():
# #             if smart_match(term, query_words, query_lower):
# #                 for syn in synonyms:
# #                     add_keyword(syn)

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_II_TERMS
# #         ) or CLASS_II_RE.search(query_lower):
# #             topics.append("class ii")
# #             add_keyword("class ii basic services")
# #             # Add the specific matched term as keyword
# #             for term in CLASS_II_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_III_TERMS
# #         ) or CLASS_III_RE.search(query_lower):
# #             topics.append("class iii")
# #             add_keyword("class iii major services")
# #             # Add the specific matched term as keyword so the server
# #             # ranks that procedure's entry highest (e.g. "implant" → implant entry)
# #             for term in CLASS_III_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         if any(smart_match(w, query_words, query_lower) for w in ORTHO_TERMS):
# #             topics.append("orthodontic treatment")
# #             add_keyword("orthodontic treatment")

# #         if any(smart_match(w, query_words, query_lower) for w in TMJ_TERMS):
# #             topics.append("tmj")
# #             add_keyword("tmj")
# #             add_keyword("temporomandibular")

# #         if any(smart_match(w, query_words, query_lower) for w in LIMIT_TERMS):
# #             topics.append("plan limits")
# #             add_keyword("plan limits")

# #         DENTAL_EXCLUSION_TERMS = [
# #             "not covered",
# #             "excluded",
# #             "exclusions",
# #             "limitations",
# #             "what is not",
# #             "what are not",
# #         ]
# #         if any(
# #             smart_match(w, query_words, query_lower) for w in DENTAL_EXCLUSION_TERMS
# #         ):
# #             topics.append("dental exclusions")
# #             add_keyword("dental exclusions and limitations")

# #         # Office visit / copay queries (primarily Willamette-style plans)
# #         OFFICE_VISIT_TERMS = [
# #             "copay",
# #             "office visit",
# #             "general visit",
# #             "specialist visit",
# #             "visit copay",
# #         ]
# #         if any(smart_match(w, query_words, query_lower) for w in OFFICE_VISIT_TERMS):
# #             topics.append("office visit")
# #             add_keyword("office visit copayments")
# #             add_keyword("copay")

# #         # Broad dental list query (no specific class/procedure found)
# #         # → add all class topics so server fetches from every class.
# #         # Safe for Willamette too: class i/ii/iii topics score near-zero
# #         # against D-code entries so scoring falls back to keyword matching,
# #         # which works the same as the LLM path would have.
# #         if not topics and any(
# #             w in query_lower for w in ["all", "list", "every", "covered services"]
# #         ):
# #             topics.extend(["class i", "class ii", "class iii"])
# #             add_keyword("class i diagnostic and preventive services")
# #             add_keyword("class ii basic services")
# #             add_keyword("class iii major services")

# #         # No fallback — generic "dental" query goes to LLM

# #     # 6. VISION
# #     if any(
# #         smart_match(w, query_words, query_lower) for w in ["vision", "eye", "glasses"]
# #     ):

# #         HARDWARE_TERMS = [
# #             "hardware",
# #             "contact",
# #             "contacts",
# #             "lenses",
# #             "lens",
# #             "frames",
# #             "eyeglass",
# #             "eyeglasses",
# #             "bifocal",
# #             "trifocal",
# #             "progressive",
# #             "sunglasses",
# #         ]
# #         EXAM_TERMS = [
# #             "eye exam",
# #             "vision exam",
# #             "eye examination",
# #             "optometrist",
# #             "optometry",
# #             "refraction",
# #         ]
# #         OUT_OF_AREA_TERMS = [
# #             "out of area",
# #             "out-of-area",
# #             "outside washington",
# #             "outside alaska",
# #             "travelling",
# #             "traveling",
# #         ]
# #         PROVIDER_TERMS = [
# #             "in-network provider",
# #             "vision provider",
# #             "vision care provider",
# #             "out-of-network vision",
# #         ]
# #         EXCLUSION_TERMS = ["not covered", "excluded", "exclusions and limitations"]

# #         if any(smart_match(w, query_words, query_lower) for w in HARDWARE_TERMS):
# #             topics.append("vision hardware")
# #             add_keyword("vision hardware")

# #         if any(smart_match(w, query_words, query_lower) for w in EXAM_TERMS):
# #             topics.append("vision exams")
# #             add_keyword("vision exams")

# #         if any(smart_match(w, query_words, query_lower) for w in OUT_OF_AREA_TERMS):
# #             topics.append("out-of-area care")
# #             add_keyword("out-of-area care")

# #         if any(smart_match(w, query_words, query_lower) for w in PROVIDER_TERMS):
# #             topics.append("selecting a vision care provider")
# #             add_keyword("selecting a vision care provider")

# #         if any(smart_match(w, query_words, query_lower) for w in EXCLUSION_TERMS):
# #             topics.append("exclusions and limitations")
# #             add_keyword("exclusions and limitations")
# #         # No fallback — generic "vision" query goes to LLM

# #     # 7. PRIMARY / SPECIALIST
# #     if smart_match("primary care", query_words, query_lower) or any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["pcp", "primary", "physician"]
# #     ):
# #         topics.append("Professional Visit Copay")
# #         add_keyword("Professional Visit Copay")
# #     if smart_match("specialist", query_words, query_lower):
# #         topics.append("specialist")
# #         add_keyword("specialist visit")

# #     # 8. MENTAL HEALTH
# #     if re.search(r"\bmental\b", query_lower) or any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["behavioral", "psychiatrist", "psychological"]
# #     ):
# #         topics.append("mental-health")
# #         if smart_match("psychological testing", query_words, query_lower):
# #             add_keyword("psychological testing")
# #         if smart_match("neuropsychological testing", query_words, query_lower):
# #             add_keyword("neuropsychological testing")
# #         if smart_match("mental health visit", query_words, query_lower):
# #             add_keyword("mental health visit")
# #         if smart_match("behavioral health", query_words, query_lower):
# #             add_keyword("behavioral health")

# #     # 9. MATERNITY
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["pregnant", "maternity", "delivery", "prenatal"]
# #     ):
# #         topics.append("maternity")

# #     # 10. HOSPITAL
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["hospital", "nursing", "facility"]
# #     ):
# #         topics.append("hospital")

# #     # 12. PHARMACY
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["drug", "prescription", "pharmacy", "generic", "brand", "specialty"]
# #     ):
# #         topics.append("pharmacy")

# #     # 13. REHABILITATION
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["rehab", "therapy", "physical", "speech", "occupational"]
# #     ):
# #         topics.append("rehabilitation")
# #         if smart_match("physical therapy", query_words, query_lower):
# #             add_keyword("physical therapy")
# #         if smart_match("speech therapy", query_words, query_lower):
# #             add_keyword("speech therapy")
# #         if smart_match("occupational therapy", query_words, query_lower):
# #             add_keyword("occupational therapy")

# #     # 14. OTHER FLAGS
# #     if smart_match("referral", query_words, query_lower):
# #         topics.append("referral")
# #     if smart_match("authorization", query_words, query_lower):
# #         topics.append("prior authorization")

# #     print(f"[*] Resolved Topics : {topics}")
# #     print(f"[*] Extracted Keywords : {extracted_keywords}")

# #     # ── Residual keyword extraction (at end of resolve_insurance_topic) ───────
# #     RESIDUAL_STOP = {
# #         "what",
# #         "which",
# #         "how",
# #         "does",
# #         "do",
# #         "did",
# #         "is",
# #         "are",
# #         "was",
# #         "the",
# #         "a",
# #         "an",
# #         "and",
# #         "or",
# #         "but",
# #         "in",
# #         "on",
# #         "at",
# #         "to",
# #         "for",
# #         "of",
# #         "from",
# #         "by",
# #         "with",
# #         "that",
# #         "this",
# #         "can",
# #         "will",
# #         "would",
# #         "should",
# #         "could",
# #         "my",
# #         "your",
# #         "our",
# #         "its",
# #         "under",
# #         "about",
# #         "tell",
# #         "know",
# #         "want",
# #         "need",
# #         "get",
# #         "show",
# #         "covered",
# #         "coverage",
# #         "cover",
# #         "plan",
# #         "plans",
# #         "benefit",
# #         "benefits",
# #         "service",
# #         "services",
# #         "cost",
# #         "costs",
# #         "price",
# #         "fee",
# #         "amount",
# #         "amounts",
# #         "pay",
# #         "paying",
# #         "charge",
# #         "charges",
# #         "any",
# #         "some",
# #         "all",
# #         "option",
# #         "options",
# #         "information",
# #         "info",
# #         "detail",
# #         "details",
# #         "type",
# #         "types",
# #         "kind",
# #         "kinds",
# #         "question",
# #         "much",
# #         "many",
# #         "more",
# #         "have",
# #         "that",
# #         "them",
# #         "they",
# #         "been",
# #         "when",
# #         "where",
# #         "there",
# #         "teeth",
# #         "tooth",
# #         "treatment",
# #         "therapy",
# #         "procedure",
# #         "happens",
# #         "dentist",
# #     }

# #     already_captured = " ".join(topics + extracted_keywords).lower()

# #     for word in query_words:
# #         if len(word) > 3 and word not in RESIDUAL_STOP and word not in already_captured:
# #             extracted_keywords.append(word)

# #     # Dental overview detection: broad "what does my plan cover" queries
# #     # that produce no specific topic get expanded to all class topics.
# #     _OVERVIEW_TERMS = [
# #         "what does my plan cover",
# #         "what is covered",
# #         "what's covered",
# #         "overview",
# #         "summary",
# #         "all benefits",
# #         "all covered",
# #         "each type",
# #         "types of service",
# #         "types of coverage",
# #         "everything covered",
# #         "full coverage",
# #         "complete coverage",
# #         "services are covered",
# #         "services covered",
# #         "what services",
# #     ]
# #     _dental_words = ["dental", "tooth", "teeth", "gum", "oral", "dentist"]
# #     # Gate 4: no medical/vision words — prevents "my dental and medical plan" confusion
# #     _non_dental_words = [
# #         "medical",
# #         "vision",
# #         "eye",
# #         "glasses",
# #         "pcp",
# #         "hospital",
# #         "pharmacy",
# #         "prescription",
# #         "specialist",
# #         "deductible and medical",
# #     ]
# #     _safe_dental_overview = any(w in query_lower for w in _dental_words) and not any(
# #         w in query_lower for w in _non_dental_words
# #     )
# #     if (
# #         not topics
# #         and p_type == "dental"
# #         and _safe_dental_overview
# #         and any(smart_match(w, query_words, query_lower) for w in _OVERVIEW_TERMS)
# #     ):
# #         topics = ["class i", "class ii", "class iii", "plan limits"]
# #         for kw in [
# #             "class i diagnostic and preventive services",
# #             "class ii basic services",
# #             "class iii major services",
# #             "plan limits",
# #             "deductible",
# #             "annual maximum",
# #         ]:
# #             add_keyword(kw)
# #         print("[*] DENTAL OVERVIEW QUERY → expanding to all class topics")

# #     # Sort for consistent cache keys regardless of word order
# #     extracted_keywords = sorted(set(extracted_keywords))
# #     topics = sorted(set(topics))

# #     print(f"[*] Resolved Topics After Residual Extraction: {topics}")
# #     print(f"[*] Extracted Keywords After Residual Extraction: {extracted_keywords}")

# #     return {
# #         "topics": list(set(topics)),
# #         "keywords": list(set(extracted_keywords)),
# #     }


# # def generate_ironclad_instruction():
# #     return (
# #         "### ROLE: Health Insurance Benefits Auditor\n"
# #         "### TASK: Extract benefit details from provided plan data\n\n"
# #         "### 🚨 HARD OUTPUT CONTRACT (NON-NEGOTIABLE)\n"
# #         "1. You MUST return EXACTLY ONE Markdown table\n"
# #         "2. Returning MORE THAN ONE table = FAILURE\n"
# #         "3. Returning ZERO tables = FAILURE\n"
# #         "4. Any text outside the table = FAILURE\n\n"
# #         "### 🚨 TABLE FORMAT (FIXED)\n"
# #         "| Benefit | In-Network | Out-of-Network | Limitations |\n"
# #         "| :--- | :--- | :--- | :--- |\n\n"
# #         "### 🚨 FIELD MAPPING (CRITICAL)\n"
# #         "1. Each ROW contains: event, service, in_network, out_of_network, notes\n"
# #         "2. 'service' is the actual benefit item\n"
# #         "3. 'event' provides context for the service\n"
# #         "4. You MUST construct Benefit as:\n"
# #         "   → Benefit = event + ' - ' + service\n"
# #         "5. If event is missing, use only service\n"
# #         "6. NEVER ignore event if it exists\n\n"
# #         "### 🚨 ROW HANDLING RULE (CRITICAL)\n"
# #         "1. Each service represents ONE distinct row\n"
# #         "2. If multiple services exist under the same event, you MUST return multiple rows\n"
# #         "3. DO NOT merge different services into one row\n"
# #         "4. Merging rows = FAILURE\n\n"
# #         "### 🚨 RELEVANCE FILTER (CRITICAL)\n"
# #         "1. Include ONLY rows that directly answer the user’s question\n"
# #         "2. DO NOT include unrelated rows even if they are in the same section\n"
# #         "3. If multiple rows match the question, include ALL of them\n"
# #         "4. DO NOT drop valid rows just to reduce count\n"
# #         "5. Including irrelevant rows = FAILURE\n\n"
# #         "### 🚨 STRICT PARSING MODE\n"
# #         "1. Input context is already structured into ROW blocks\n"
# #         "2. Each ROW contains explicit fields\n"
# #         "3. Extract values EXACTLY as written\n"
# #         "4. DO NOT interpret, summarize, or infer\n"
# #         "5. DO NOT use prior knowledge\n\n"
# #         "### 🚨 DATA RULES\n"
# #         "1. Preserve exact wording\n"
# #         "2. If a field is missing, empty, or whitespace only → use 'Data Not Found'\n"
# #         "3. NEVER leave a Markdown cell empty (| |)\n\n"
# #         "### 🚨 ANTI-HALLUCINATION RULE\n"
# #         "1. Use ONLY items explicitly present in context\n"
# #         "2. DO NOT add new benefits not present in context\n"
# #         "3. DO NOT create rows that do not exist in input\n\n"
# #         "### 🚨 EXCLUDED / OTHER SERVICES\n"
# #         "If the answer belongs to excluded/other services:\n"
# #         "| Results |\n"
# #         "| :--- |\n\n"
# #         "### 🚨 FINAL INSTRUCTION\n"
# #         "Return EXACTLY ONE Markdown table and NOTHING ELSE."
# #     )


# # def flatten_message_content(content):
# #     """
# #     NUCLEAR NORMALIZER: Forces any Ollama response (List, Dict, or None)
# #     into a plain string to prevent Gradio/Streamlit/Pydantic validation errors.
# #     """
# #     if not content:
# #         return ""
# #     if isinstance(content, str):
# #         return content
# #     if isinstance(content, list):
# #         parts = []
# #         for item in content:
# #             if isinstance(item, dict):
# #                 parts.append(item.get("text", str(item)))
# #             else:
# #                 parts.append(str(item))
# #         return " ".join(parts).strip()
# #     return str(content).strip()


# # def build_category_prompt(query: str) -> str:
# #     return f"""
# #         You are a strict JSON classifier.

# #         Classify the query into ONE category:
# #         medical, dental, or vision.

# #         ### VERY IMPORTANT:
# #         Return ONLY this exact JSON format:

# #         {{
# #         "category": "medical"
# #         }}

# #         ### RULES:
# #         - The key MUST be "category"
# #         - The value MUST be one of: medical, dental, vision
# #         - Do NOT return anything else
# #         - Do NOT change the key name
# #         - Do NOT return null

# #         ### USER QUERY:
# #         "{query}"
# #         """


# # def get_category_from_llm(query: str) -> str:
# #     prompt = build_category_prompt(query)

# #     llm_messages = [{"role": "user", "content": prompt}]

# #     try:
# #         llm_response = ollama.chat(
# #             model=LOCAL_MODEL,
# #             messages=llm_messages,
# #             format="json",  # 🔥 ensures JSON output
# #             options={"temperature": 0.0, "num_ctx": 8192},
# #         )

# #         content = llm_response["message"]["content"]

# #         print(f"[*] RAW LLM CATEGORY RESPONSE: {content}")

# #         data = json.loads(content)

# #         category = data.get("category", "").strip().lower()

# #         # 🔥 HARD GUARD (CRITICAL)
# #         if category not in {"medical", "dental", "vision"}:
# #             print(f"[WARNING] Invalid category from LLM: {category}")
# #             return "medical"

# #         print(f"[*] LLM CATEGORY DETECTED: {category}")
# #         return category

# #     except Exception as e:
# #         print(f"[ERROR] LLM CATEGORY FAILED: {e}")
# #         return "medical"


# # def detect_category(query_words, query):
# #     category = None

# #     if any(
# #         w in query_words
# #         for w in [
# #             "dental",
# #             "ortho",
# #             "braces",
# #             "tooth",
# #             "teeth",
# #             "gum",
# #             "cavity",
# #             "filling",
# #             "crown",
# #             "denture",
# #             "molar",
# #             "canal",
# #             "implant",
# #             "tmj",
# #             "jaw",
# #             "orthodontic",
# #             "orthodontia",
# #             "panoramic",
# #             "sealant",
# #             "fluoride",
# #             "class",
# #         ]
# #     ):
# #         print("[*] CATEGORY MATCH → dental")
# #         category = "dental"
# #         return category

# #     if any(
# #         w in query_words
# #         for w in ["vision", "eye", "glasses", "lens", "lenses", "contacts"]
# #     ):
# #         print("[*] CATEGORY MATCH → vision")
# #         category = "vision"
# #         return category

# #     # Smart match fallback for procedure-specific dental terms not caught
# #     # by exact word match above (e.g. "sealants" → "sealant", "fillings" → "filling")
# #     _dental_proc_terms = [
# #         "sealant",
# #         "filling",
# #         "fluoride",
# #         "prophylaxis",
# #         "cleaning",
# #         "extraction",
# #         "periodontal",
# #         "scaling",
# #         "anesthesia",
# #         "sedation",
# #         "nitrous",
# #         "apicoectomy",
# #         "retrograde",
# #         "veneer",
# #         "onlay",
# #         "inlay",
# #     ]
# #     if any(smart_match(w, query_words, query.lower()) for w in _dental_proc_terms):
# #         print("[*] CATEGORY MATCH → dental (procedure)")
# #         return "dental"
# #     if any(
# #         w in query_words
# #         for w in [
# #             "medical",
# #             "doctor",
# #             "health",
# #             "hospital",
# #             "pcp",
# #             "emergency",
# #             "er",
# #             "urgent",
# #             "ambulance",
# #             "room",
# #             "immunization",
# #             "immunizations",
# #             "vaccination",
# #             "cancer",
# #         ]
# #     ):
# #         print("[*] CATEGORY MATCH → medical")
# #         category = "medical"
# #         return category
# #     # --------------------------------------------------
# #     # 🤖 LLM FALLBACK
# #     # --------------------------------------------------
# #     print("[*] CATEGORY NOT FOUND → CALLING LLM")
# #     if category == None:

# #         return get_category_from_llm(query)


# # def detect_category_from_history(history, limit=3):
# #     for msg in reversed(history[-limit:]):
# #         if msg["role"] == "user":
# #             query_lower = msg["content"].lower()
# #             query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]

# #             cat = detect_category(query_words, query_lower)
# #             if cat:
# #                 return cat
# #     return None


# # def extract_user_queries(recent_history):
# #     queries = []

# #     for msg in recent_history:
# #         if msg.get("role") == "user":
# #             queries.append(msg.get("content", "").lower())

# #     return queries


# # async def get_ai_response(query, history):
# #     # 1. ACCESS GLOBALS
# #     global p_type_fast, p_tier_fast, last_type_global

# #     found_topics = []
# #     keywords = []

# #     try:
# #         from insurance_mcp.server import query_insurance_benefits

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

# #         # --- 3. TYPE DETECTION ---
# #         p_type = detect_category(query_words, query)
# #         print(f"[*] FINAL TYPE DETECTED : {p_type}")
# #         if not p_type:
# #             print("[*] TYPE NOT DETECTED IN QUERY - SEARCHING HISTORY")
# #             p_type = detect_category_from_history(history)

# #         benefit_prompt = """
# #             Can you please let me know if you are looking for Medical, Dental, or Vision benefits?

# #             Please select from the following options:
# #             • Medical (Doctor visits, prescriptions, hospital stays)
# #             • Dental (Cleanings, X-rays, orthodontics)
# #             • Vision (Eye exams, glasses, contact lenses)

# #             Please type your selection below to get started.
# #             """
# #         if not p_type:
# #             # The prompt for member benefit selection
# #             # Example of how to use it
# #             return benefit_prompt

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
# #         # PROMPT: Category is Medical, but specific service is missing
# #         medical_detail_prompt = """
# #         I see you're interested in your Medical benefits! To give you the right details, what specifically are you looking for?

# #         Please select or type an option:
# #         • Deductibles & Out-of-Pocket Max
# #         • Emergency Room (ER) or Urgent Care costs
# #         • X-Rays, Lab Work, or Imaging
# #         • Office Visit Copays (PCP or Specialist)

# #         What would you like to check first?
# #         """

# #         # PROMPT: Category is Dental, but specific service is missing
# #         dental_detail_prompt = """
# #         Great, let's look at your Dental coverage. What specific information do you need?

# #         Please select or type an option:
# #         • Preventive Care (Cleanings & Exams)
# #         • Orthodontics (Braces or Aligners)
# #         • Basic Services (Fillings or Extractions)
# #         • Major Services (Crowns, Bridges, or Dentures)

# #         Which of these can I help you with?
# #         """

# #         # PROMPT: Category is Vision, but specific service is missing
# #         vision_detail_prompt = """
# #         I can certainly help with your Vision benefits! Which part of your coverage are you curious about?

# #         Please select or type an option:
# #         • Routine Eye Exams
# #         • Eyeglass Frames & Lenses
# #         • Contact Lens Allowance
# #         • Laser Vision Correction (LASIK)

# #         Please type your selection below to see your benefits.
# #         """

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
# #         # ========================================================================
# #         if not found_topics:
# #             print(f"[*] TURNING TO LLM TO FIND THE TOPIC FROM QUERY : {query}")
# #             topic_prompt = """
# #                 You are a medical insurance classification assistant.

# #                 Your task is to extract:
# #                 1. "topics" → benefit-level categories (NOT plan types)
# #                 2. "keywords" → specific services mentioned

# #                 ----------------------------------------
# #                 🚫 STRICT RULES:
# #                 ----------------------------------------

# #                 1. DO NOT return these as topics:
# #                 - medical
# #                 - dental
# #                 - vision

# #                 These are PLAN TYPES, not benefit topics.

# #                 2. Topics should describe SPECIFIC BENEFITS such as:
# #                 - preventive care
# #                 - emergency care
# #                 - imaging
# #                 - office visits
# #                 - hospital services
# #                 - pharmacy
# #                 - rehabilitation

# #                 (These are examples, not a fixed list.)

# #                 3. DO NOT invent vague categories like:
# #                 - other
# #                 - other medical
# #                 - general
# #                 - miscellaneous

# #                 4. If no clear topic exists → return:
# #                 "topics": ["UNKNOWN"]

# #                 5. Return canonical benefit names as they appear in the booklet.
# #                 For vision queries use: "vision hardware", "vision exams", "out-of-area care",
# #                 "exclusions and limitations", "selecting a vision care provider".
# #                 Example: "what does my vision plan cover" →
# #                 {"topics": ["vision hardware", "vision exams"], "keywords": ["vision hardware", "vision exams"]}

# #                 5. ALWAYS extract keywords (critical for retrieval)

# #                 ----------------------------------------
# #                 🎯 GUIDELINES:
# #                 ----------------------------------------

# #                 - Prefer specific topics over generic ones
# #                 - Use 1–2 words when possible
# #                 - Avoid combining multiple topics into one phrase
# #                 ❌ "emergency urgent care"
# #                 ✅ "emergency", "urgent care"

# #                 ----------------------------------------
# #                 ✅ EXAMPLES:
# #                 ----------------------------------------

# #                 User: "allergy testing and treatment cost"
# #                 Output:
# #                 {"topics": ["preventive care"], "keywords": ["allergy testing", "treatment"]}

# #                 User: "emergency room cost"
# #                 Output:
# #                 {"topics": ["emergency"], "keywords": ["emergency room"]}

# #                 User: "x ray and blood work"
# #                 Output:
# #                 {"topics": ["imaging"], "keywords": ["x ray", "blood work"]}

# #                 User: "tmj care"
# #                 Output:
# #                 {"topics": ["UNKNOWN"], "keywords": ["tmj", "temporomandibular joint"]}

# #                 User: "transplant cost"
# #                 Output:
# #                 {"topics": ["UNKNOWN"], "keywords": ["transplant"]}

# #                 ----------------------------------------
# #                 Return strictly valid JSON.
# #                 """

# #             llm_messages = [
# #                 {"role": "system", "content": topic_prompt},
# #                 {"role": "user", "content": f"User Query: {query_lower}"},
# #             ]

# #             # ============================================================
# #             # 🔥 LLM TOPIC + KEYWORD EXTRACTION (PRODUCTION SAFE)
# #             # ============================================================

# #             llm_response = ollama.chat(
# #                 model=LOCAL_MODEL,
# #                 messages=llm_messages,
# #                 format="json",
# #                 options={"temperature": 0.0, "num_ctx": 8192},
# #             )

# #             raw_content = llm_response["message"].get("content", "")
# #             print(f"[*] ROW CONTENT BY LLM AFTER TOPIC SEARCH : {raw_content}")

# #             match = re.search(r"\{.*\}", raw_content, re.DOTALL)

# #             # ============================================================
# #             # 🔧 HELPERS
# #             # ============================================================

# #             INVALID_TOPICS = {"medical", "dental", "vision"}

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
# #                         if clean_k and clean_k not in keywords:
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

# #             # Search recent_history for the last occurrence of any valid keyword
# #             # We look for the word itself or the word inside a Markdown table | PCP |
# #             user_queries = extract_user_queries(recent_history)

# #             for past_query in reversed(user_queries):
# #                 print(f"[*] CHECKING PAST QUERY: {past_query}")

# #                 history_word_list = [
# #                     re.sub(r"[^\w\s]", "", w) for w in past_query.split()
# #                 ]

# #                 history_topics = resolve_insurance_topic(history_word_list, past_query)

# #                 if history_topics:
# #                     print(f"[*] RECOVERED TOPIC FROM HISTORY: {history_topics}")
# #                     found_topics.extend(history_topics)
# #                     break

# #         print(f"Final topic found from query : {found_topics}")
# #         last_type_global = p_type  # Now setting up the last type here as now we have category and topic and keywords
# #         # Once we reach here it means topic did not available
# #         if len(found_topics) == 0 and len(keywords) == 0:
# #             if p_type.lower() == "medical":
# #                 return medical_detail_prompt
# #             elif p_type.lower() == "dental":
# #                 return dental_detail_prompt
# #             elif p_type.lower() == "vision":
# #                 return vision_detail_prompt
# #             else:
# #                 return "I can help with Medical, Dental, or Vision benefits! Which would you like to explore?"

# #         # topics_part = "_".join(sorted(filter(None, found_topics)))

# #         # if keywords:
# #         #     normalized_keywords = sorted(k.lower().strip() for k in keywords if k)
# #         #     keywords_part = "_".join(sorted(normalized_keywords))
# #         #     cache_key = f"{p_type}_{topics_part}_{keywords_part}"
# #         # else:
# #         #     cache_key = f"{p_type}_{topics_part}"

# #         # print(f"[*] CACHE KEY: {cache_key}")

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

# #         cached_context = TOOL_RESULT_CACHE.get(cache_key)

# #         if cached_context:
# #             print(f"[*] CACHE HIT: {cache_key}")
# #             print(f"[*] CACHED CONTEXT: {cached_context}")
# #             clean_context = cached_context  # ← reuse context
# #             # system_prompt = generate_ironclad_instruction()

# #             # messages = [
# #             #     {
# #             #         "role": "system",
# #             #         "content": system_prompt,
# #             #     },
# #             #     {
# #             #         "role": "user",
# #             #         "content": f"""
# #             #         Context:
# #             #         {cached_context}

# #             #         Question:
# #             #         {query}
# #             #         """,
# #             #     },
# #             # ]

# #             # response = ollama.chat(
# #             #     model=LOCAL_MODEL,
# #             #     messages=messages,
# #             #     options={"temperature": 0.0, "num_ctx": 8192},
# #             # )

# #             # final_content = flatten_message_content(
# #             #     response["message"].get("content", "")
# #             # ).strip()

# #             # return final_content

# #         # --- 9. THE REASONING LOOP ---
# #         messages = []

# #         # --- SYSTEM PROMPT ---
# #         system_prompt = {
# #             "role": "system",
# #             "content": (
# #                 "You are an AI assistant.\n"
# #                 "For every user question, you MUST call the tool "
# #                 "'query_insurance_benefits'.\n"
# #                 "Do NOT answer directly.\n"
# #                 "Return ONLY the tool call."
# #             ),
# #         }

# #         messages.append(system_prompt)

# #         # --- HISTORY ---
# #         # Pass last 4 turns (8 messages) to LLM — enough for conversational
# #         # context without bloating the prompt with stale exchanges.
# #         if history:
# #             for turn in history[-8:]:
# #                 messages.append(
# #                     {
# #                         "role": turn.get("role", "user"),
# #                         "content": flatten_message_content(turn.get("content", "")),
# #                     }
# #                 )

# #         messages.append({"role": "user", "content": query})

# #         # --- TOOL ---
# #         tools = [
# #             {
# #                 "type": "function",
# #                 "function": {
# #                     "name": "query_insurance_benefits",
# #                     "description": (
# #                         "MANDATORY: Retrieve insurance benefit details using the user query and resolved topics. "
# #                         "You MUST call this tool before answering."
# #                     ),
# #                     "parameters": {
# #                         "type": "object",
# #                         "properties": {
# #                             "query": {
# #                                 "type": "string",
# #                                 "description": "The full user question",
# #                             },
# #                             "topics": {
# #                                 "type": "array",
# #                                 "items": {"type": "string"},
# #                                 "description": "List of relevant topics like ['primary', 'urgent care', 'imaging']",
# #                             },
# #                             "category": {
# #                                 "type": "string",
# #                                 "description": "Category (medical / dental / sbc)",
# #                             },
# #                             "keywords": {
# #                                 "type": "array",
# #                                 "items": {"type": "string"},
# #                                 "description": "List of relevant keywords like ['immunization', 'mammogram']",
# #                             },
# #                         },
# #                         "required": ["query", "topics", "category"],
# #                     },
# #                 },
# #             }
# #         ]

# #         print(f"[*] QUERY: {query}")
# #         print(f"[*] TOPICS (fallback only): {found_topics}")

# #         # ============================================================
# #         # STEP 1 — TOOL CALL
# #         # ============================================================
# #         resp = ollama.chat(
# #             model=LOCAL_MODEL,
# #             messages=messages,
# #             tools=tools,
# #             options={"temperature": 0},
# #         )

# #         msg = resp["message"]
# #         tool_calls = msg.get("tool_calls", [])

# #         tool_result = None

# #         # ============================================================
# #         # STEP 2 — EXECUTE TOOL
# #         # ============================================================
# #         # Ensure keywords exists (from your LLM detection block). Default to empty list if not.
# #         if tool_calls:
# #             tool = tool_calls[0]
# #             function_name = tool["function"]["name"]
# #             arguments = tool["function"]["arguments"]

# #             print(f"[TOOL CALL]: {function_name} | {found_topics} | {p_type}")

# #             try:
# #                 if function_name == "query_insurance_benefits":
# #                     tool_result = query_insurance_benefits(
# #                         query=arguments.get("query", query),
# #                         topics=found_topics,  # 🔥 FORCE FROM BACKEND
# #                         category=p_type,
# #                         keywords=keywords,
# #                     )
# #                 print(
# #                     f"[*] TOOL RESULT after calling query_insurance_benefits : {tool_result}"
# #                 )
# #             except Exception as e:
# #                 print(f"[!] TOOL FAILURE: {e}")
# #                 tool_result = "RETRIEVAL ERROR"

# #         else:
# #             print("[!] No tool call — forcing retrieval")

# #             try:
# #                 tool_result = query_insurance_benefits(
# #                     query=query, topics=found_topics, category=p_type, keywords=keywords
# #                 )
# #             except Exception as e:
# #                 print(f"[!] TOOL FAILURE: {e}")
# #                 tool_result = "RETRIEVAL ERROR"

# #         # ============================================================
# #         # STEP 3 — KEYWORD FILTERING (HEADER-AWARE)
# #         # ============================================================
# #         # if tool_result and tool_result != "RETRIEVAL ERROR" and keywords:
# #         #     # Split by double newline to get items and section headers
# #         #     chunks = tool_result.split("\n\n")

# #         #     filtered_chunks = []
# #         #     for c in chunks:
# #         #         # 1. ALWAYS keep headers (lines starting with #)
# #         #         if c.strip().startswith("#"):
# #         #             filtered_chunks.append(c)
# #         #             continue

# #         #         # 2. ALWAYS keep cost items — they must never be filtered out
# #         #         # Cost items contain "in_network" or "out_of_network" fields
# #         #         if '"in_network"' in c or '"out_of_network"' in c:
# #         #             filtered_chunks.append(c)
# #         #             continue

# #         #         # 3. Keep other chunks that match keywords
# #         #         if any(k.lower() in c.lower() for k in keywords):
# #         #             filtered_chunks.append(c)

# #         #     # 3. Only update tool_result if we found actual matches (not just headers)
# #         #     # We check if there is at least one non-header chunk
# #         #     has_content = any(not c.strip().startswith("#") for c in filtered_chunks)

# #         #     if has_content:
# #         #         tool_result = "\n\n".join(filtered_chunks)
# #         #     else:
# #         #         print(
# #         #             "[*] No keyword matches found; keeping original content to avoid losing headers."
# #         #         )
# #         if tool_result and tool_result != "RETRIEVAL ERROR" and keywords:

# #             section_blocks = re.split(r"(### SECTION: [A-Z]+)", tool_result)

# #             rebuilt = []

# #             current_header = None

# #             for block in section_blocks:

# #                 block = block.strip()

# #                 if not block:
# #                     continue

# #                 # SECTION HEADER
# #                 if block.startswith("### SECTION:"):
# #                     current_header = block
# #                     rebuilt.append(block)
# #                     continue

# #                 # COST and INFO sections → NEVER FILTER
# #                 if current_header in ("### SECTION: COST", "### SECTION: INFO"):
# #                     rebuilt.append(block)
# #                     continue

# #                 # Other sections → keyword filter
# #                 if any(k.lower() in block.lower() for k in keywords):
# #                     rebuilt.append(block)

# #             tool_result = "\n\n".join(rebuilt)

# #         # ============================================================
# #         # 🔥 STEP 4 — CLEAN CONTEXT (CRITICAL)
# #         # ============================================================
# #         def trim_context(text, max_chars=8000):
# #             if not text:
# #                 return ""

# #             if len(text) <= max_chars:
# #                 return text

# #             cut = text[:max_chars]
# #             last_break = cut.rfind("\n\n")

# #             if last_break != -1:
# #                 return cut[:last_break]

# #             return cut

# #         # ============================================================
# #         # STEP 5 — FINAL ANSWER (NO TOOLS)
# #         # ============================================================

# #         # 🔥 CLEAN CONTEXT (single trim only)
# #         clean_context = trim_context(tool_result or "", 8000)
# #         print(f"[*] CLEAN CONTEXT FOR FINAL PROCESSING : {clean_context}")

# #         # ============================================================
# #         # STEP 6 — SETUP THE CACHE
# #         # ============================================================
# #         # After getting clean context store the value in cache
# #         TOOL_RESULT_CACHE[cache_key] = clean_context
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

# #         def build_cost_table(context: str, user_query: str, keywords: list) -> str:
# #             rows = []
# #             items = re.split(r"Item \d+:", context)
# #             for item in items:
# #                 item = item.strip()
# #                 if not item:
# #                     continue
# #                 json_match = re.search(r"\{.*\}", item, re.DOTALL)
# #                 if not json_match:
# #                     continue
# #                 try:
# #                     data = json.loads(json_match.group(0))
# #                 except Exception:
# #                     continue
# #                 event = data.get("event", "")
# #                 service = data.get("service", "")
# #                 in_net = data.get("in_network", "")
# #                 out_net = data.get("out_of_network", "")
# #                 limitation = data.get("notes") or "Data Not Found"
# #                 rows.append((event, service, in_net, out_net, limitation))
# #             if not rows:
# #                 return "No relevant cost information found."
# #             # if len(rows) > 10:
# #             #     print("[*] TOO MANY ROWS → FALLBACK TO LLM")
# #             #     return "__USE_LLM__"

# #             def norm(text):
# #                 return re.sub(r"\s+", " ", str(text).lower())

# #             def soft_match(term, text):
# #                 if re.search(r"\b" + re.escape(term) + r"\b", text):
# #                     return True
# #                 if term.endswith("s"):
# #                     if re.search(r"\b" + re.escape(term[:-1]) + r"\b", text):
# #                         return True
# #                 if re.search(r"\b" + re.escape(term + "s") + r"\b", text):
# #                     return True
# #                 return False

# #             query_words = [
# #                 w.lower() for w in re.split(r"\W+", user_query) if len(w) > 2
# #             ]
# #             STOP_WORDS = {
# #                 "show",
# #                 "me",
# #                 "you",
# #                 "can",
# #                 "what",
# #                 "are",
# #                 "is",
# #                 "the",
# #                 "for",
# #                 "all",
# #                 "tell",
# #                 "about",
# #                 "want",
# #                 "know",
# #                 "get",
# #                 "give",
# #                 "find",
# #                 "help",
# #                 "need",
# #                 "does",
# #                 "do",
# #                 "did",
# #                 "will",
# #                 "would",
# #                 "should",
# #                 "could",
# #                 "how",
# #                 "when",
# #                 "where",
# #                 "which",
# #                 "who",
# #                 "why",
# #                 "and",
# #                 "or",
# #                 "but",
# #                 "not",
# #                 "no",
# #                 "any",
# #                 "some",
# #                 "with",
# #                 "in",
# #                 "on",
# #                 "at",
# #                 "to",
# #                 "of",
# #                 "from",
# #                 "by",
# #                 "as",
# #                 "an",
# #                 "a",
# #                 "this",
# #                 "that",
# #                 "these",
# #                 "those",
# #                 "its",
# #                 "my",
# #                 "your",
# #                 "our",
# #                 "their",
# #                 "if",
# #                 "so",
# #                 "also",
# #                 "just",
# #                 "more",
# #                 "like",
# #                 "than",
# #                 "then",
# #                 "into",
# #                 "out",
# #                 "up",
# #                 "has",
# #                 "have",
# #                 "had",
# #                 "was",
# #                 "were",
# #                 "been",
# #                 "be",
# #                 "cost",
# #                 "costs",
# #                 "price",
# #                 "fee",
# #                 "amount",
# #                 "amounts",
# #                 "pay",
# #                 "paying",
# #                 "charge",
# #                 "charges",
# #             }
# #             WEAK_WORDS = {
# #                 "treatment",
# #                 "service",
# #                 "services",
# #                 "care",
# #                 "visit",
# #                 "visits",
# #                 "procedure",
# #                 "therapy",
# #                 "exam",
# #                 "test",
# #                 "testing",
# #                 "program",
# #                 "programs",
# #                 "cost",
# #                 "benefit",
# #                 "benefits",
# #                 "coverage",
# #                 "affect",
# #                 "affects",
# #                 "apply",
# #                 "applies",
# #                 "work",
# #                 "works",
# #                 "covered",
# #                 "cover",
# #                 "covers",
# #                 "plan",
# #                 "plans",
# #                 "under",
# #                 "include",
# #                 "includes",
# #                 "provide",
# #                 "office",
# #                 "clinic",
# #                 "clinics",
# #                 "setting",
# #                 "settings",
# #                 "facility",
# #                 "facilities",
# #                 "provides",
# #                 # Location/setting words — they qualify WHERE a service is rendered,
# #                 # not WHAT the benefit is.  Keeping them as strong terms causes
# #                 # every event with "Office and Clinic Visits" in its service row to
# #                 # score ≥ MIN_CONFIDENCE and pollute results for specific queries
# #                 # like "show me foot care in an office or clinic visit cost".
# #                 "office",
# #                 "clinic",
# #                 "clinics",
# #                 "setting",
# #                 "settings",
# #                 "facility",
# #                 "facilities",
# #                 "general",
# #                 "standard",
# #                 "regular",
# #                 "specific",
# #             }
# #             strong_terms = [
# #                 w for w in query_words if w not in STOP_WORDS and w not in WEAK_WORDS
# #             ]
# #             if keywords:
# #                 for k in keywords:
# #                     for part in re.split(r"\W+", k.lower()):
# #                         if part and part not in WEAK_WORDS:
# #                             strong_terms.append(part)
# #                             # "copay" prefix matches "copayments" event names
# #                             if part == "copay":
# #                                 strong_terms.append("copayments")

# #             # Common abbreviations and plain-language → benefit name mappings.
# #             # Handles "PCP visit", "ER", "meds", "telehealth" etc. without LLM.
# #             SYNONYMS = {
# #                 "pcp": ["professional visits"],
# #                 "gp": ["professional visits"],
# #                 "er": ["emergency room"],
# #                 "ed": ["emergency room"],
# #                 "rx": ["prescription drug"],
# #                 "meds": ["prescription drug"],
# #                 "medicine": ["prescription drug"],
# #                 "telehealth": ["virtual care"],
# #                 "telemedicine": ["virtual care"],
# #                 "mental": ["mental health"],
# #                 "psych": ["mental health"],
# #                 "lab": ["diagnostic"],
# #                 "labs": ["diagnostic"],
# #                 "xray": ["diagnostic"],
# #                 "mri": ["diagnostic"],
# #                 "uc": ["urgent care"],
# #                 "oop": ["out-of-pocket"],
# #                 "immunotherapy": ["cellular immunotherapy"],
# #                 "chemo": ["chemotherapy"],
# #                 "physio": ["rehabilitation"],
# #                 "snf": ["skilled nursing"],
# #                 "hme": ["home medical equipment"],
# #             }
# #             expanded = []
# #             for term in strong_terms:
# #                 if term in SYNONYMS:
# #                     expanded.extend(SYNONYMS[term])
# #             strong_terms = list(set(strong_terms + expanded))

# #             if not strong_terms:
# #                 strong_terms = query_words
# #             strong_terms = list(set(strong_terms))
# #             if any("class" in (k or "") for k in keywords):
# #                 print(f"[BCT] strong_terms={sorted(strong_terms)}")
# #             query_phrase = " ".join(strong_terms)
# #             event_groups = {}
# #             for r in rows:
# #                 event_groups.setdefault(norm(r[0]), []).append(r)
# #             event_scores = []
# #             for event, group_rows in event_groups.items():
# #                 score = 0
# #                 event_text = norm(event)
# #                 if query_phrase:
# #                     for r in group_rows:
# #                         if query_phrase in norm(" ".join(r)):
# #                             score += 500
# #                             break
# #                 for term in strong_terms:
# #                     if soft_match(term, event_text):
# #                         score += 200
# #                 for r in group_rows:
# #                     service_text = norm(r[1])
# #                     for term in strong_terms:
# #                         if soft_match(term, service_text):
# #                             score += 80
# #                 for r in group_rows:
# #                     full_text = norm(" ".join(r))
# #                     for term in strong_terms:
# #                         if soft_match(term, full_text):
# #                             score += 10
# #                 if score > 0:
# #                     event_scores.append((score, event, group_rows))

# #             # Minimum confidence threshold.
# #             # Score anatomy: event name match = 200/term, phrase match = 500.
# #             # A score of 150+ means at least one strong term matched the event name.
# #             # Below that = weak/accidental match → LLM handles it better.
# #             MIN_CONFIDENCE = 150

# #             if event_scores:
# #                 event_scores.sort(key=lambda x: x[0], reverse=True)
# #                 if any("class" in (k or "") for k in keywords):
# #                     for s, e, r in event_scores:
# #                         print(f"[BCT] score={s:5d} rows={len(r):2d} event={e[:45]!r}")
# #                 best_score, _, _ = event_scores[0]
# #                 if best_score < MIN_CONFIDENCE:
# #                     print(f"[*] LOW CONFIDENCE (score={best_score}) → LLM")
# #                     return "__USE_LLM__"

# #                 # Include ALL events that scored above MIN_CONFIDENCE ONLY when
# #                 # keywords explicitly name multiple distinct benefits
# #                 # e.g. ["vision hardware", "vision exams"] → show both
# #                 # "allergy testing" → only show top event (Psychological Testing
# #                 # would wrongly score high on "testing" alone)
# #                 multi_word_kws = [kw for kw in keywords if " " in kw.lower()]
# #                 confident = [
# #                     (s, e, r) for s, e, r in event_scores if s >= MIN_CONFIDENCE
# #                 ]

# #                 if len(confident) > 1 and len(multi_word_kws) >= 2:
# #                     print(
# #                         f"[*] MULTI-EVENT MATCH ({len(confident)} events) → SHOWING ALL"
# #                     )
# #                     best_rows = [row for _, _, rows in confident for row in rows]
# #                 else:
# #                     best_rows = event_scores[0][2]
# #             else:
# #                 # No event matched — too vague, let LLM handle it
# #                 return "__USE_LLM__"

# #             # 🔥 Multi-class list query: balance rows across events so no class
# #             # is crowded out by a higher-scoring event hitting the row cap.
# #             # e.g. "show me all covered services" returns Class I (score 1000),
# #             # Class II (600), Class III (600) — without balancing, [:10] would
# #             # cut off Class II entirely since Class I + Class III fills 11 rows.
# #             class_topics_for_balance = [
# #                 t
# #                 for t in found_topics
# #                 if re.match(r"^class\s+[i123]+$", t.lower().strip())
# #             ]
# #             if len(class_topics_for_balance) > 1 and len(best_rows) > 10:
# #                 per_event = {}
# #                 for r in best_rows:
# #                     per_event.setdefault(r[0], []).append(r)
# #                 max_per = max(1, 10 // len(per_event))
# #                 balanced = []
# #                 for event_rows in per_event.values():
# #                     balanced.extend(event_rows[:max_per])
# #                 best_rows = balanced
# #                 print(
# #                     f"[*] BALANCED ROWS: {len(best_rows)} across {len(per_event)} events"
# #                 )
# #             elif len(best_rows) > 10:
# #                 print("[*] TOO MANY FILTERED ROWS → USING TOP 10")
# #                 best_rows = best_rows[:10]

# #             # Copay filter — only show copayment events when "copay" is in query.
# #             # Matches both: event name contains "copay" (Office Visit Copayments)
# #             # AND in_network value contains "copay" (e.g. "$20 copay" for D9440).
# #             if "copay" in user_query.lower():
# #                 _copay_rows = [
# #                     r
# #                     for r in best_rows
# #                     if "copay" in r[0].lower() or "copay" in r[2].lower()
# #                 ]
# #                 if _copay_rows:
# #                     best_rows = _copay_rows

# #             # Suppress COST entirely when every row has no usable in_network value
# #             # (e.g. orthodontic D8xxx entries with "Data Not Found" — misleading to show)
# #             _no_data_rows = [
# #                 r for r in best_rows if r[2] in ("", "Data Not Found", "Data not found")
# #             ]
# #             if len(_no_data_rows) == len(best_rows) and best_rows:
# #                 return None

# #             # Service-level keyword filter — when specific procedure keywords are
# #             # present (e.g. radiographic, bitewing, prophylaxis), only show rows
# #             # whose service name contains at least one of those terms.
# #             # Prevents unrelated services in the same event from appearing
# #             # (e.g. D1310 Nutritional Counseling showing in an x-ray query).
# #             _SERVICE_SPECIFIC = {
# #                 "radiographic",
# #                 "bitewing",
# #                 "periapical",
# #                 "panoramic",
# #                 "prophylaxis",
# #                 "sealant",
# #                 "fluoride",
# #                 "varnish",
# #                 "amalgam",
# #                 "composite",
# #                 "endodontic",
# #                 "canal",
# #                 "pontic",
# #                 "extraction",
# #                 "scaling",
# #                 "debridement",
# #                 "gingivectomy",
# #             }
# #             _svc_kws = [kw for kw in keywords if kw in _SERVICE_SPECIFIC]
# #             if _svc_kws:
# #                 _svc_rows = [
# #                     r for r in best_rows if any(kw in norm(r[1]) for kw in _svc_kws)
# #                 ]
# #                 if _svc_rows:
# #                     best_rows = _svc_rows

# #             # Dental class-specific filter — when a single class topic is detected
# #             # (class i, class ii, or class iii), only show rows from that class.
# #             # Prevents coinsurance-word contamination pulling in wrong classes.
# #             # Does NOT fire when multiple class topics are present (comparison queries).
# #             class_topics = [
# #                 t
# #                 for t in found_topics
# #                 if re.match(r"^class\s+[i123]+$", t.lower().strip())
# #             ]
# #             if len(class_topics) == 1:
# #                 cf = class_topics[0].lower()
# #                 class_filtered = [r for r in best_rows if cf in r[0].lower()]
# #                 if class_filtered:
# #                     best_rows = class_filtered
# #                     print(f"[*] CLASS FILTER applied: {cf} → {len(best_rows)} rows")

# #             is_list_query = any(
# #                 w in user_query.lower() for w in ["all", "list", "which", "show me"]
# #             )
# #             final_rows = best_rows[:10] if is_list_query else best_rows
# #             table = (
# #                 "| Benefit | Service | In-Network | Out-of-Network | Limitations |\n"
# #             )
# #             table += "| :--- | :--- | :--- | :--- | :--- |\n"
# #             for e, s, i, o, l in final_rows:
# #                 table += f"| {e} | {s} | {i} | {o} | {l} |\n"
# #             return table

# #         def build_info_response(context: str, user_query: str, keywords: list) -> str:
# #             rows = []
# #             items = re.split(r"Item \d+:", context)
# #             for item in items:
# #                 item = item.strip()
# #                 if not item:
# #                     continue
# #                 json_match = re.search(r"\{.*\}", item, re.DOTALL)
# #                 if not json_match:
# #                     continue
# #                 try:
# #                     data = json.loads(json_match.group(0))
# #                 except Exception:
# #                     continue
# #                 event = data.get("event", "")
# #                 information = (
# #                     data.get("information")
# #                     or data.get("limitations")
# #                     or "Data Not Found"
# #                 )
# #                 if event and information and information != "Data Not Found":
# #                     rows.append((event, information))
# #             if not rows:
# #                 return "__USE_LLM__"

# #             # Relevance filter: when specific keywords exist, only keep rows
# #             # whose event name matches at least one specific keyword.
# #             # Prevents "Dental Emergency" / "Dental Implant Surgery" from
# #             # appearing for unrelated queries just because "dental" matches.
# #             _GENERIC_KWS = {
# #                 "dental",
# #                 "vision",
# #                 "medical",
# #                 "plan",
# #                 "care",
# #                 "benefit",
# #                 "benefits",
# #                 "coverage",
# #             }
# #             _specific_kws = [
# #                 k for k in keywords if k not in _GENERIC_KWS and len(k) > 3
# #             ]
# #             if _specific_kws:

# #                 def _relevant(event_name):
# #                     ev = event_name.lower()
# #                     return any(
# #                         re.search(r"\b" + re.escape(k) + r"\b", ev)
# #                         for k in _specific_kws
# #                     )

# #                 filtered = [(e, i) for e, i in rows if _relevant(e)]
# #                 if filtered:
# #                     rows = filtered

# #             table = "| Topic | Coverage Information |\n"
# #             table += "| :--- | :--- |\n"
# #             for event, info in rows:
# #                 table += f"| {event} | {info} |\n"
# #             return table

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
# #                     cost_table = None
# #                 else:
# #                     cost_table = build_cost_table(cost_ctx, query, keywords)

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
# #                     info_table = None
# #                 else:
# #                     info_table = build_info_response(info_ctx, query, keywords)

# #                 if cost_table not in (None, "__USE_LLM__") and info_table not in (
# #                     None,
# #                     "__USE_LLM__",
# #                 ):
# #                     combined = cost_table.rstrip("\n") + "\n\n" + info_table
# #                     print("[*] COST+INFO COMBINED TABLE → RETURNING (NO LLM)")
# #                     return {"answer": combined}

# #                 elif cost_table not in (None, "__USE_LLM__"):
# #                     print("[*] COST+INFO → RETURNING COST TABLE ONLY (NO LLM)")
# #                     return {"answer": cost_table}

# #                 elif info_table not in (None, "__USE_LLM__"):
# #                     print("[*] COST+INFO → RETURNING INFO TABLE ONLY (NO LLM)")
# #                     return {"answer": info_table}

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
# #                     import ollama as _ollama, json as _json, os as _os

# #                     mini_resp = _ollama.generate(
# #                         model=_os.getenv("OLLAMA_MODEL", "llama3.1"),
# #                         prompt=mini_prompt,
# #                         format="json",
# #                         options={"temperature": 0, "num_predict": 60},
# #                     )
# #                     mini_data = _json.loads(mini_resp["response"])
# #                     benefit_name = mini_data.get("benefit", "")
# #                     intent = mini_data.get("intent", "both")
# #                     print(f"[*] MINI-LLM: benefit={benefit_name!r}  intent={intent}")

# #                     if benefit_name:
# #                         if intent in ("cost", "both"):
# #                             ct2 = build_cost_table(
# #                                 cost_ctx, benefit_name, [benefit_name]
# #                             )
# #                             if ct2 not in (None, "__USE_LLM__"):
# #                                 if intent == "cost":
# #                                     return {"answer": ct2}
# #                                 it2 = build_info_response(
# #                                     info_ctx, benefit_name, [benefit_name]
# #                                 )
# #                                 if it2 not in (None, "__USE_LLM__"):
# #                                     return {"answer": ct2.rstrip("\n") + "\n\n" + it2}
# #                                 return {"answer": ct2}
# #                         if intent in ("info", "both"):
# #                             it2 = build_info_response(
# #                                 info_ctx, benefit_name, [benefit_name]
# #                             )
# #                             if it2 not in (None, "__USE_LLM__"):
# #                                 return {"answer": it2}
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
# #                 final_answer = build_cost_table(clean_context, query, keywords)
# #                 if final_answer == "__USE_LLM__":
# #                     print("[*] SWITCHING TO LLM DUE TO NOISE")
# #                 elif final_answer:
# #                     print("[*] PARSER SUCCESS → RETURNING TABLE")
# #                     return {"answer": final_answer}

# #             elif sections[0] == "info":
# #                 print("[*] INFO DETECTED → BUILDING INFO TABLE")
# #                 final_answer = build_info_response(clean_context, query, keywords)
# #                 if final_answer == "__USE_LLM__":
# #                     print("[*] INFO FALLBACK TO LLM")
# #                 elif final_answer:
# #                     print("[*] INFO PARSER SUCCESS → RETURNING TABLE")
# #                     return {"answer": final_answer}

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

# #         final_resp = ollama.chat(
# #             model=LOCAL_MODEL,
# #             messages=final_messages,
# #             options={"temperature": 0},
# #         )

# #         # ============================================================
# #         # FINAL RESPONSE (STRICT CONTRACT WITH UI)
# #         # ============================================================

# #         msg = final_resp.get("message", {})

# #         # 🔥 SAFELY EXTRACT CONTENT
# #         if isinstance(msg, dict):
# #             final_answer = msg.get("content", "")
# #         else:
# #             final_answer = str(msg)

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

# #         return {"answer": final_answer}

# #     except Exception as e:
# #         print(f"❌ SYNTHESIS ERROR: {e}")
# #         return f"⚠️ Client Logic Error: {str(e)}"
