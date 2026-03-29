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
            ]
        ):
            new_type = "medical"

        # --- 4. THE TOPIC GUARD ---
        if new_type != p_type_fast:
            print(f"[*] TOPIC SHIFT: {p_type_fast} -> {new_type}. Wiping tier.")
            p_tier_fast = None

        p_type_fast = new_type

        # --- 5. YEAR EXTRACTION ---
        found_years = re.findall(r"202\d", query)
        if not found_years:
            found_years = sorted(list(set(re.findall(r"202\d", full_context_query))))
            if not found_years:
                found_years = [CURRENT_YEAR_INT]

        # --- 6. TIER PERSISTENCE ---
        p_tier_match = re.search(r"(gold|silver|bronze)", query_lower)
        if p_tier_match:
            p_tier_fast = p_tier_match.group()
        elif not p_tier_fast:
            p_tier_match = re.search(r"(gold|silver|bronze)", full_context_query)
            if p_tier_match:
                p_tier_fast = p_tier_match.group()

        # --- 7. EMERGENCY & PCP ROUTING (UPDATED) ---
        # We start with a fallback and only print ONCE the final decision is made
        p_topic = "benefit"
        # Prioritize 'emergency' for ER queries and 'professional services' for PCP
        if any(
            w in query_words for w in ["emergency", "er", "urgent", "ambulance", "room"]
        ):
            p_topic = "emergency"
        elif any(
            w in query_words
            for w in ["imaging", "xray", "x-ray" "mri", "scan", "blood"]
        ):
            p_topic = "imaging"
            print(f"[*] TOPIC ROUTER: Diagnostic/Imaging detected -> 'imaging'")
        elif any(w in query_words for w in ["dental", "ortho", "braces"]):
            p_topic = "orthodontia"

        # Priority 4: Professional Services
        elif (
            any(
                w in query_words
                for w in ["pcp", "doctor", "physician", "copay", "specialist"]
            )
            or "primary care" in query_lower
        ):
            # Sub-routing for index keywords
            if "specialist" in query_words:
                p_topic = "specialist"
            elif (
                any(w in query_words for w in ["pcp", "primary"])
                or "primary care" in query_lower
            ):
                p_topic = "pcp"
            else:
                p_topic = "pcp"  # Default to PCP for professional queries

        print(
            f"[*] RESOLVED CONTEXT: Years={found_years}, Type={p_type_fast}, Tier={p_tier_fast or 'Unknown'}, Topic={p_topic}"
        )

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
            instruction = (
                f"You are a professional Insurance Advisor. Analyzing {p_tier_fast} {p_type_fast} plans. "
                "Synthesize the data into a clear Markdown Table. Compare years if multiple are provided. "
                "Always include 'In-Network' and 'Out-of-Network' costs."
            )
            fast_msgs = [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": f"Synthesize this data: {str(cached_data_fragments)} for query: {query}",
                },
            ]
            final_synth = ollama.chat(
                model=LOCAL_MODEL, messages=fast_msgs, options={"temperature": 0.0}
            )
            return flatten_message_content(final_synth["message"].get("content", ""))

        # --- 9. INITIAL DISCOVERY CHECK ---
        called_discovery = True if not p_tier_fast else False

        # --- 5. THE REASONING LOOP ---
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
                        # 1. Emergency (Specific)
                        if any(
                            w in query_words
                            for w in ["emergency", "er", "urgent", "ambulance", "room"]
                        ):
                            p_topic = "emergency"
                        # 2. Imaging & Diagnostics (Specific)
                        elif any(
                            w in query_words
                            for w in [
                                "xray",
                                "x-ray",
                                "imaging",
                                "mri",
                                "scan",
                                "blood",
                            ]
                        ):
                            p_topic = "imaging"
                        # 3. Dental
                        elif any(
                            w in query_words for w in ["dental", "ortho", "braces"]
                        ):
                            p_topic = "dental"
                        # 4. Specialist
                        elif "specialist" in query_words:
                            p_topic = "specialist"
                        # 5. PCP / Primary Care
                        elif (
                            any(
                                w in query_words for w in ["pcp", "doctor", "physician"]
                            )
                            or "primary care" in query.lower()
                        ):
                            p_topic = "pcp"
                        # 6. Fallback
                        else:
                            p_topic = "benefit"

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

        # --- THE FINAL SAFETY NET (STABILITY & SYNONYM FIX) ---
        print("\n" + "=" * 60)
        print("[*] DIAGNOSTIC: DATA PASSED TO LLM")

        # 1. CONSOLIDATE DATA (Clean & Deduplicated)
        all_data_segments = []
        # Extract content from tool messages
        reasoning_data = [
            str(m["content"]) for m in messages if m.get("role") == "tool"
        ]
        if reasoning_data:
            all_data_segments.extend(reasoning_data)
        if cached_data_fragments:
            all_data_segments.extend(cached_data_fragments)

        # FIX: Use a simple list join. 'set()' can reorder and hide data randomly.
        master_data_string = ""
        unique_segments = []
        for seg in all_data_segments:
            if seg not in unique_segments:
                unique_segments.append(seg)
        master_data_string = "\n\n".join(unique_segments)

        print(f"{master_data_string}")
        print("=" * 60 + "\n")

        # 2. DYNAMIC INSTRUCTION: THE IRONCLAD PROTOCOL
        instruction = (
            f"### ROLE: Specialized Insurance Data Analyst for {p_tier_fast} {p_type_fast}.\n"
            "### MANDATORY OUTPUT FORMAT: MARKDOWN TABLE ONLY.\n"
            "0. START IMMEDIATELY: Your response MUST begin with the '|' character. NO INTRO TEXT.\n"
            "1. SYNONYM MAPPING: Treat 'Specialist visit' and 'Specialist Physician' as the same. "
            "Crucially, treat 'Primary care visit' and 'Primary Care Physician (PCP)' as the SAME BENEFIT.\n"
            "2. STRICT BENEFIT ISOLATION: Identify the specific benefit requested (e.g., 'Deductible', 'PCP', 'ER', 'X-ray'). "
            "You MUST ONLY include the row(s) for that exact benefit. "
            "Physically EXCLUDE neighboring rows (like Specialist if asking for PCP) from the table.\n"
            "3. ATOMIC VALUE NORMALIZATION: Use ONLY the specific cost-sharing values found in the MASTER DATA. "
            "If the text says '20% coinsurance', report ONLY that. DO NOT add '$0' or 'No Charge' to that same cell. "
            "Do not mix values from different plan types (e.g., do not use a 'Medical' value for a 'Dental' benefit).\n"
            "4. NO NARRATION: Do NOT explain results or add 'Note' sections at the bottom.\n"
            "5. NETWORK COLUMNS: You MUST include 'In-Network' and 'Out-of-Network' status for each year. "
            "STRICT EXCEPTION: ONLY use 'Same as In-Network' for the Out-of-Network column if the benefit is 'Emergency room care'. "
            "For all other benefits in an HMO, you must report 'Not Covered' for Out-of-Network if that is what the text says.\n"
            f"6. PLAN TYPE STRICTNESS: You are currently analyzing {p_type_fast} benefits. "
            f"If the plan type is 'Dental', you MUST physically ignore any Medical values found in the Master Data. "
            f"Filter out all data that does not belong to the '{p_type_fast}' category.\n"
            "7. ROW DEDUPLICATION: Ensure you do not create separate rows for different years of the same benefit. "
            "Merge them into a single row with multiple year columns. Normalize values (e.g., '$25/visit' and '$25' should just be '$25').\n"
            f"8. YEAR FILTER: ONLY create columns for: {', '.join(str(y) for y in found_years)}."
        )

        # 3. CLEAN SLATE: Focus exclusively on the fetched data
        final_messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": f"Master Data:\n{master_data_string}\n\nOriginal Query: {query}",
            },
        ]

        # 4. FINAL EXECUTION (Forced 0.0 temperature)
        final_resp = ollama.chat(
            model=LOCAL_MODEL,
            messages=final_messages,
            options={
                "temperature": 0.0,
                "num_ctx": 8192,
            },  # Increased context to handle 2026 table
        )

        # 5. POST-PROCESSING: THE IRON CURTAIN
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
