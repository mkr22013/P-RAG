import os, ollama, re
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()
LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

# --- GLOBAL RAM CACHE (Persistent for the session) ---
TOOL_RESULT_CACHE = {}
# --- GLOBAL SESSION STATE ---
p_type_fast = "medical"
p_tier_fast = "gold"  # Default starting point
CURRENT_YEAR_INT = datetime.now().year


def resolve_insurance_topic(query_words, full_query_text):
    """SINGLE SOURCE OF TRUTH: Maps keywords to our specific index topics."""
    query_lower = full_query_text.lower()

    # 1. DEDUCTIBLES
    if any(
        w in query_words
        for w in ["deductible", "deductibles", "out-of-pocket", "oop", "limit"]
    ):
        return "deductible"

    # 2. EMERGENCY & AMBULANCE (Surgical Match)
    # \ber\b ensures 'ER' is a standalone word. We also check for 'room'.
    if (
        re.search(r"\ber\b", query_lower)
        or "emergency" in query_lower
        or "ambulance" in query_words
        or "room" in query_words
    ):
        # If 'urgent' is also present, we pivot to Urgent Care topic
        if "urgent" in query_words:
            return "urgent"
        return "emergency"

    # 3. URGENT CARE (Dedicated Topic)
    if any(w in query_words for w in ["urgent", "clinic", "after-hours"]):
        return "urgent"

    # 4. IMAGING
    if any(
        w in query_words for w in ["xray", "x-ray", "imaging", "mri", "scan", "blood"]
    ):
        return "imaging"

    # 5. DENTAL & VISION
    if any(w in query_words for w in ["dental", "ortho", "braces"]):
        return "orthodontia"
    if any(w in query_words for w in ["vision", "eye", "glasses"]):
        return "vision"

    # 6. PRIMARY & SPECIALIST
    if (
        any(
            w in query_words
            for w in ["pcp", "primary", "doctor", "physician", "copay", "specialist"]
        )
        or "primary care" in query_lower
    ):
        if "specialist" in query_words:
            return "specialist"
        return "primary"

    return "benefit"


def lock_plan_metadata(found_years, detected_type, detected_tier):
    """
    CROSS-REFERENCE: Compares detected intent against the ACTUAL DB Index.
    Returns (Valid Years, Valid Type, Valid Tier).
    """
    from server import get_available_plans
    import ast

    # 1. Get the actual schema from the DB
    raw_schema = get_available_plans()
    if "DATABASE INFO" in raw_schema:
        return found_years, detected_type, detected_tier  # Fallback if DB empty

    # Convert the string list back to Python objects
    # Expected format: [(2026, 'Medical', 'Gold'), (2025, 'Medical', 'Gold')]
    try:
        schema_data = ast.literal_eval(
            raw_schema.replace("DATA SOURCE SCHEMA (Year, Type, Tier): ", "")
        )
    except:
        return found_years, detected_type, detected_tier

    d_tier_str = str(detected_tier or "").lower()
    d_type_str = str(detected_type or "medical").lower()
    # 2. Find the best matches
    valid_years = [y for y, t, tr in schema_data if y in found_years]
    valid_type = next(
        (t for y, t, tr in schema_data if t.lower() == d_type_str), detected_type
    )
    valid_tier = next(
        (tr for y, t, tr in schema_data if tr.lower() == d_tier_str), detected_tier
    )

    return valid_years or found_years, valid_type, valid_tier


def generate_ironclad_instruction(
    p_tier_fast, p_type_fast, header_line, separator_line, p_topic
):
    # DYNAMIC FORBIDDEN LIST: Keeps the table focused
    forbidden_map = {
        "imaging": "Pharmacy, Generic, Brand, Deductible, Specialist, and PCP",
        "deductible": "Specialist, PCP, Imaging, ER, and Pharmacy",
        "emergency": "Deductible, Specialist, PCP, and Preventive",
        "primary": "Specialist, Preventive, Dental, Ortho, Deductible, and ER",
        "specialist": "Primary, PCP, Preventive, Deductible, and Imaging",
    }
    erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")

    return (
        f"### ROLE: Lead Insurance Auditor for Premera ({p_tier_fast} {p_type_fast}).\n"
        f"### MANDATORY: MARKDOWN TABLE ONLY. START WITH '|'. NO NARRATION.\n\n"
        "--- RULE 1: TOPIC FOCUS (STRICT) ---\n"
        f"You are searching for '{p_topic}'. Physically DELETE any rows for: {erase_list}. "
        "Only report the benefit that specifically matches your search topic.\n\n"
        "--- RULE 2: DATA ACCURACY & YEAR SILOS ---\n"
        "1. YEAR SILOS: Treat 2024 and 2026 as separate documents. "
        "If 2024 says '$50', report '$50'. If 2026 says '$55', report '$55'.\n"
        "2. NO BLANKS: You MUST fill every cell in the header. If a 2026 HMO benefit "
        "is not covered Out-of-Network, you MUST write 'Not Covered'. NEVER leave a cell empty.\n"
        "3. FALLBACK: If a value is truly missing from a year's data, write 'Data Not Found'.\n\n"
        "--- RULE 3: ARCHITECTURE ---\n"
        f"HEADER:\n{header_line}\n{separator_line}\n"
        "1. PIPE-COUNTING: 1st data column = In-Network | 2nd data column = Out-of-Network.\n"
        "2. LITERAL MIRRORING: Mirror characters exactly. If it says '$50', write '$50'."
    )


def flatten_message_content(content):
    """
    NUCLEAR NORMALIZER: Forces any Ollama response (List, Dict, or None)
    into a plain string to prevent Gradio/Streamlit/Pydantic validation errors.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return " ".join(parts).strip()
    return str(content).strip()


async def get_ai_response(query, history):
    # 1. ACCESS GLOBALS
    global p_type_fast, p_tier_fast

    has_retrieved_data = False
    turn_count = 0
    try:
        from server import query_insurance_benefits, get_available_plans

        # --- 2. CONTEXT MERGING (MEMORY) ---
        recent_history = " ".join(
            [flatten_message_content(m["content"]) for m in history]
        )
        query_lower = query.lower()  # <--- ADD THIS LINE HERE
        full_context_query = f"{recent_history} {query_lower}"

        # Clean words for surgical matching
        query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]

        # --- 3. SMART TOPIC & TYPE DETECTION ---
        new_type = p_type_fast  # Default to current session type

        if any(w in query_words for w in ["dental", "ortho", "braces"]):
            new_type = "dental"
        elif any(w in query_words for w in ["vision", "eye", "glasses"]):
            new_type = "vision"
        elif any(
            w in query_words
            for w in [
                "medical",
                "doctor",
                "health",
                "hospital",
                "pcp",
                "emergency",
                "er",
                "urgent",
                "ambulance",
                "room",
            ]
        ):
            new_type = "medical"

        # --- 4. THE TOPIC SHIFT GUARD ---
        if new_type.lower() != p_type_fast.lower():
            print(f"[*] TOPIC SHIFT: {p_type_fast} -> {new_type}. Wiping tier.")
            p_tier_fast = None
            # DO NOT set p_type_fast yet! Wait for the Lock check.

        # --- 5. YEAR EXTRACTION ---
        found_years = re.findall(r"202\d", query)
        if not found_years:
            found_years = sorted(list(set(re.findall(r"202\d", full_context_query))))
            if not found_years:
                found_years = [CURRENT_YEAR_INT]

        # --- 6. TIER PERSISTENCE ---
        detected_tier = p_tier_fast  # Start with what we already know
        p_tier_match = re.search(r"(gold|silver|bronze)", query_lower)
        if p_tier_match:
            detected_tier = p_tier_match.group()
        elif not detected_tier:
            p_tier_match = re.search(r"(gold|silver|bronze)", full_context_query)
            if p_tier_match:
                detected_tier = p_tier_match.group()

        # --- 7. THE SOURCE OF TRUTH LOCK (CRITICAL FIX) ---
        # We pass 'new_type' and 'detected_tier' to the checker.
        # It returns the EXACT casing/names found in the SQLite DB.
        found_years, locked_type, locked_tier = lock_plan_metadata(
            found_years, new_type, detected_tier
        )

        # NOW we commit to the globals
        p_type_fast = locked_type
        p_tier_fast = locked_tier

        # --- 8. UNIFIED TOPIC ROUTING (WITH PERSISTENCE) ---
        detected_topic = resolve_insurance_topic(query_words, query_lower)
        p_topic = "benefit"  # Initialize scope

        if detected_topic == "benefit":
            # Search history for our diagnostic string "Topic: (\w+)"
            all_topics = re.findall(r"Topic[:=]\s*(\w+)", recent_history)
            if all_topics:
                # Recover the MOST RECENT context (e.g., 'emergency')
                p_topic = all_topics[-1].lower()
                print(f"[*] TOPIC PERSISTENCE: Recovered '{p_topic}' from history.")
            else:
                p_topic = "benefit"
        else:
            # User provided a specific topic keyword (e.g., "PCP")
            p_topic = detected_topic

        # NOW it is safe to use p_topic for your logs and synthesis
        print(
            f"[*] RESOLVED CONTEXT: Years={found_years}, Type={p_type_fast}, Tier={p_tier_fast or 'Unknown'}, Topic={p_topic}"
        )

        # We dynamically build the year-specific columns based on the 'found_years' list
        year_cols = " | ".join(
            [f"{y} In-Network | {y} Out-of-Network" for y in found_years]
        )
        header_line = f"| Benefit | {year_cols} |"
        separator_line = f"| :--- | {' :--- |' * (len(found_years) * 2)}"

        # --- 8. TURBO CACHE HIT CHECK ---
        cached_data_fragments = []
        if found_years and p_tier_fast:
            for y in found_years:
                # Key must include p_topic to distinguish between PCP and Deductible fetches
                key = f"{y}_{p_type_fast}_{p_tier_fast}_{p_topic}".replace(
                    " ", ""
                ).lower()
                if key in TOOL_RESULT_CACHE:
                    cached_data_fragments.append(TOOL_RESULT_CACHE[key])

        if len(cached_data_fragments) == len(found_years) and len(found_years) > 0:
            print(f"[*] TURBO CACHE HIT: Bypassing reasoning for {found_years}")
            has_retrieved_data = True

            # --- THE FIX: USE THE SAME IRONCLAD PROTOCOL ---
            master_data_string = "\n\n".join(cached_data_fragments)

            # Generate the same instruction used in Part-5
            instruction = generate_ironclad_instruction(
                p_tier_fast, p_type_fast, header_line, separator_line, p_topic
            )

            fast_msgs = [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": f"Master Data:\n{master_data_string}\n\nOriginal Query: {query}",
                },
            ]

            final_synth = ollama.chat(
                model=LOCAL_MODEL,
                messages=fast_msgs,
                options={"temperature": 0.0, "num_ctx": 8192},
            )

            # --- THE IRON CURTAIN CLEANUP (Crucial for the PDF Download!) ---
            final_content = flatten_message_content(
                final_synth["message"].get("content", "")
            )
            if "|" in final_content:
                start = final_content.find("|")
                end = final_content.rfind("|")
                if end > start:
                    final_content = final_content[start : end + 1].strip()

            return final_content

        # --- 9. THE REASONING LOOP ---
        messages = []
        system_prompt = {
            "role": "system",
            "content": (
                "You are a specialized Insurance Assistant. Goal: 100% Accuracy."
                "\n\nSTRICT TOOL RULES:"
                "\n1. TOPIC ISOLATION: If the user switches Plan Types (e.g., from Medical to Dental), "
                "you MUST NOT use the Year or Tier from the previous topic. Forget the previous context."
                "\n2. DISCOVERY FIRST: For a new Plan Type, always call 'get_available_plans' "
                "before 'query_insurance_benefits' to confirm exactly which Years and Tiers exist."
                "\n3. COMPARISON: If asked to compare years (e.g., 2024 vs 2025), you MUST generate a "
                "SEPARATE tool call for EACH year mentioned."
                "\n4. NO GUESSING: Never provide a 'year' or 'plan_tier' in a tool call "
                "unless it was explicitly in the CURRENT query or confirmed via 'get_available_plans'."
                "\n5. SILENCE: Provide ONLY JSON tool blocks during planning. No conversational filler."
                "\n6. NO NARRATION: Do NOT tell the user you are calling a tool. Output ONLY JSON."
            ),
        }

        messages.append(system_prompt)

        if history:
            for turn in history[-2:]:
                messages.append(
                    {
                        "role": turn.get("role", "user"),
                        "content": flatten_message_content(turn.get("content", "")),
                    }
                )

        messages.append({"role": "user", "content": query})

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_available_plans",
                    "description": "PROBE DB index for available years/tiers",
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_insurance_benefits",
                    "description": "Retrieve benefit text. Only call this if you have a SPECIFIC Year and Tier.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "year": {
                                "type": "integer",
                                "description": "The 4-digit year. Use 0 if unknown.",
                            },
                            "plan_type": {"type": "string"},
                            "plan_tier": {
                                "type": "string",
                                "description": 'Gold, Silver, or Bronze. Use "Unknown" if not specified.',
                            },
                            "topic": {"type": "string"},
                        },
                        "required": [
                            "topic"
                        ],  # Remove year and tier from the 'required' list
                    },
                },
            },
        ]

        # --- THE REASONING LOOP ---
        turn_count = 0
        final_raw_context = ""
        # Tracker to prevent the LLM from asking for the same data twice in one turn
        fetched_keys_this_turn = []

        while turn_count < 3:
            turn_count += 1
            resp = ollama.chat(
                model=LOCAL_MODEL,
                messages=messages,
                tools=tools,
                options={"temperature": 0},
            )

            raw_msg = resp["message"]
            clean_content = flatten_message_content(raw_msg.get("content", ""))
            tool_calls = raw_msg.get("tool_calls", [])

            messages.append(
                {
                    "role": "assistant",
                    "content": clean_content,
                    "tool_calls": tool_calls if tool_calls else None,
                }
            )

            if not tool_calls:
                # Forces the script to reach the final synthesis/table logic
                break

            for call in tool_calls:
                func_name = call["function"]["name"]
                args = call["function"]["arguments"]

                if func_name == "get_available_plans":
                    result = get_available_plans()
                    messages.append(
                        {"role": "tool", "content": str(result), "name": func_name}
                    )

                elif func_name == "query_insurance_benefits":
                    all_args_blob = str(args).lower()
                    years_to_process = list(
                        set(
                            re.findall(r"202\d", all_args_blob)
                            or found_years
                            or [CURRENT_YEAR_INT]
                        )
                    )

                    query_clean = re.sub(r"[^\w\s]", "", query.lower())
                    query_words = query_clean.split()

                    results_list = []
                    for p_year in years_to_process:
                        p_type = p_type_fast or args.get("plan_type", "medical").lower()
                        p_tier = p_tier_fast or args.get("plan_tier", "").lower()
                        if p_tier == "unknown":
                            p_tier = None

                        # --- DYNAMIC TOPIC MAPPING (PRIORITIZED) ---
                        p_topic = resolve_insurance_topic(query_words, query.lower())

                        # --- THE DOUBLE-FETCH KILLER ---
                        fetch_id = f"{p_year}_{p_topic}"
                        if fetch_id in fetched_keys_this_turn:
                            print(f"[*] SKIPPING REDUNDANT FETCH: {fetch_id}")
                            continue

                        print(f"[*] SURGICAL FETCH: Year={p_year}, Topic={p_topic}")

                        data = query_insurance_benefits(
                            year=int(p_year),
                            plan_type=p_type,
                            plan_tier=p_tier.capitalize() if p_tier else None,
                            topic=p_topic,
                        )
                        # --- THE CACHE FIX: SAVE THE DATA FOR FUTURE TURBO HITS ---
                        if data and "ERROR" not in data:
                            # Create the key EXACTLY as Part-2 Step 8 expects it:
                            # {y}_{p_type_fast}_{p_tier_fast}_{p_topic}
                            cache_key = f"{p_year}_{p_type_fast}_{p_tier_fast}_{p_topic}".replace(
                                " ", ""
                            ).lower()
                            TOOL_RESULT_CACHE[cache_key] = data
                            print(f"[*] CACHE SAVED: {cache_key}")

                        results_list.append(data)
                        # Mark this combination as "done" for this turn
                        fetched_keys_this_turn.append(fetch_id)

                    if results_list:
                        final_raw_context = "\n\n".join(results_list)
                        messages.append(
                            {
                                "role": "tool",
                                "content": final_raw_context,
                                "name": func_name,
                            }
                        )
                        has_retrieved_data = True

        # --- THE HARD STOP TRIGGER (ADD THIS HERE) ---
        # This check looks at BOTH the new retrieved data AND the cache.
        if not has_retrieved_data and not cached_data_fragments:
            print(f"[*] NO DATA FOUND: Asking for clarification for {p_topic}")

            # Instead of returning a hard string, we give the LLM a 'Clarification' persona
            clarification_msgs = [
                {
                    "role": "system",
                    "content": "You are an Insurance Assistant. You found NO data for the requested topic. Ask the user politely which Year or Plan Tier they are interested in. Mention that you have 2024, 2025, and 2026 data available.",
                },
                {
                    "role": "user",
                    "content": f"I couldn't find {p_topic} info. Help the user narrow it down. Original query: {query}",
                },
            ]

            resp = ollama.chat(model=LOCAL_MODEL, messages=clarification_msgs)
            return flatten_message_content(resp["message"].get("content", ""))
        # --- THE FINAL SAFETY NET (STABILITY & SYNONYM FIX) ---

        print("\n" + "=" * 60)
        print("[*] DIAGNOSTIC: DATA PASSED TO LLM")
        print(f"[*] SYNTHESIZING: {p_tier_fast} {p_type_fast} - Topic: {p_topic}")

        # 10. DATA CONSOLIDATION (Clean & Deduplicated)
        # Combine fragments from both the 'Reasoning Loop' and the 'Turbo Cache'
        all_data_segments = []
        reasoning_data = [
            str(m["content"]) for m in messages if m.get("role") == "tool"
        ]
        if reasoning_data:
            all_data_segments.extend(reasoning_data)
        if cached_data_fragments:
            all_data_segments.extend(cached_data_fragments)

        # Deduplicate while preserving order (important for multi-year context)
        unique_segments = []
        for seg in all_data_segments:
            if seg not in unique_segments:
                unique_segments.append(seg)
        master_data_string = "\n\n".join(unique_segments)

        # 11. THE PREMERA-SPECIFIC PROTOCOL
        # We use f-strings to inject the 'LOCKED' variables directly into the prompt.
        # --- THE FLUID COMPARISON PROTOCOL ---
        instruction = generate_ironclad_instruction(
            p_tier_fast, p_type_fast, header_line, separator_line, p_topic
        )

        # 12. FINAL SYNTHESIS CALL
        final_messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": f"Master Data:\n{master_data_string}\n\nOriginal Query: {query}",
            },
        ]

        final_resp = ollama.chat(
            model=LOCAL_MODEL,
            messages=final_messages,
            options={"temperature": 0.0, "num_ctx": 8192},
        )

        # 13. THE IRON CURTAIN (Final String Cleanup)
        final_content = flatten_message_content(
            final_resp["message"].get("content", "")
        )
        if "|" in final_content:
            start_index = final_content.find("|")
            end_index = final_content.rfind("|")
            if end_index > start_index:
                final_content = final_content[start_index : end_index + 1].strip()

        return final_content

    except Exception as e:
        print(f"❌ SYNTHESIS ERROR: {e}")
        return f"⚠️ Client Logic Error: {str(e)}"
