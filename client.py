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


# def get_available_tiersFromDB(year, plan_type):
#     """
#     Infer the plan tier from the available plans schema for a given year and type.
#     """
#     from server import get_available_plans
#     import ast

#     raw_schema = _fetch_tiers_list(year, plan_type)
#     if "DATABASE INFO" in raw_schema:
#         return None

#     try:
#         schema_data = ast.literal_eval(
#             raw_schema.replace("DATA SOURCE SCHEMA (Year, Type, Tier): ", "")
#         )
#     except Exception:
#         return None

#     type_lower = str(plan_type or "").lower()
#     for y, t, tr in schema_data:
#         if str(y) == str(year) and str(t).lower() == type_lower:
#             return tr

#     return None

# # GOLDEN version with 6 PROMPT working
# def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # DYNAMIC FORBIDDEN LIST (Generic Sub-string Matching)
#     forbidden_map = {
#         "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy",
#         "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")
#     # STEP 4 SCOPE LOCK: Strictly define if mirroring is allowed
#     mirror_allowed = "TRUE" if "emergency" in p_topic.lower() else "FALSE"

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE ONLY. START IMMEDIATELY WITH '|'.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS. OUTPUT TABLE ONLY.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (ZERO TOLERANCE) ---\n"
#         f"1. HEADER TEMPLATE: You MUST strictly use this exact horizontal map for your columns:\n{header_line}\n"
#         "2. HORIZONTAL MAPPING: You are strictly FORBIDDEN from creating a separate row for 'Out-of-network'. "
#         "Every In-Network and Out-of-Network value MUST be on the SAME horizontal line as the benefit name."
#         "You must use BENEFIT as the only entry in the first column. Do NOT create separate rows for sub-benefits (e.g., MRI vs X-ray). "
#         # "--- STAGE 2: In-Network & Out-of-Network Rule for topic emergency (MANDATORY) ---\n"
#         # f"1. APPLY THIS RULE ONLY WHEN {p_topic.lower()} IS NOT 'emergency': For EVERY column in your header, scan the literal 'In-Network' & 'Out-of-Network' strings:\n"
#         # "   - In-Network MUST be same as shown in the master list. You Must not apply any other values.\n"
#         # "   - Out-of-Network MUST be 'Data Not Found' if missing else 'Not Covered'\n"
#         # "Failing to distinguish between In-Network and Out-of-Network values is a 100% SYSTEM FAILURE.\n\n"
#         "--- STAGE 2: THE SURGICAL ERASER (FILTERING) ---\n"
#         f"1. Physically DELETE any row containing forbidden words: {erase_list}. "
#         f"Show ONLY the row(s) exactly matching '{p_topic}'. showing any other topic listed in erase_list is considered as a SYSTEM FAILURE\n\n"
#         "--- STAGE 3: EMERGENCY RELATIONAL MIRROR (CONDITIONAL FIREWALL) ---\n"
#         f"1. ALLOW MIRRORING: {mirror_allowed}\n"
#         "2. LOGIC: If 'ALLOW MIRRORING' is FALSE, you are FORBIDDEN from mirroring columns. "
#         "If 'ALLOW MIRRORING' is TRUE, you MUST physically scan the "
#         "Out-of-Network fragment. If it is blank or says 'Same as In-Network', copy the "
#         "character string from the In-Network cell. Mirroring is ONLY for life-saving emergency care.\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE ---\n"
#         f"HEADER TEMPLATE:\n{header_line}\n{separator_line}\n"
#         "1. NO BLANKS. START WITH '|'. NO NARRATION. OUTPUT ONLY THE FINAL DATA ROW(S)."
#     )


# GOLDEN version with 6 PROMPT working
def generate_ironclad_instruction(
    p_tier_fast, p_type_fast, header_line, separator_line, p_topic
):
    forbidden_map = {
        "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
        "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy",
        "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
        "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
        "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
    }
    erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")
    mirror_allowed = "TRUE" if "emergency" in p_topic.lower() else "FALSE"

    return (
        f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
        "### MANDATORY: ONE SINGLE MARKDOWN TABLE ONLY. START IMMEDIATELY WITH '|'.\n"
        "### SILENCE: NO NARRATION, NO EXPLANATIONS. OUTPUT TABLE ONLY.\n\n"
        "--- STAGE 1: THE ARCHITECTURAL ANCHOR ---\n"
        f"1. HEADER TEMPLATE: Use ONLY this exact map:\n{header_line}\n"
        f"2. TOPIC LOCK: You MUST show ONLY the benefit exactly matching '{p_topic}'.\n"
        "3. HORIZONTAL MAPPING: Every In-Network and Out-of-Network value MUST be on the SAME horizontal line.\n\n"
        "--- STAGE 2: THE DATA SANITIZER (NO LEAKAGE) ---\n"
        f"1. GHOST WORDS: The following terms are GHOSTS. You are FORBIDDEN from typing them in any cell: {erase_list}.\n"
        "2. If a row contains a ghost word, that row is NULL. Do NOT report it. Not even a fragment of it.\n"
        "3. ABSOLUTE FILTER: If your output table contains any ghost word, it is a 100% SECURITY FAILURE.\n\n"
        "--- STAGE 3: EMERGENCY RELATIONAL MIRROR ---\n"
        f"1. ALLOW MIRRORING STATUS: {mirror_allowed}\n"
        "2. LOGIC SWITCH: Execute ONLY the rule matching the status above.\n\n"
        "   IF ALLOW MIRRORING STATUS IS FALSE:\n"
        "   - NO MIRRORING. Scan trailing lines for 'Not covered' or prices.\n"
        "   - If 'Not covered' is visible, report it. If blank, use 'Data Not Found'.\n\n"
        "   IF ALLOW MIRRORING STATUS IS TRUE:\n"
        "   - If Out-of-Network is blank, copy the exact In-Network string.\n"
        "   - Priority: Visible 'Not covered' strings always override mirroring.\n\n"
        "--- FINAL OUTPUT ARCHITECTURE ---\n"
        f"HEADER TEMPLATE:\n{header_line}\n{separator_line}\n"
        "1. NO NARRATION. START WITH '|'. OUTPUT ONLY THE CLEANED DATA ROW."
    )


# # BELOW 6 PROMPTS WORKING
# # 1. Create a table comparing the individual annual deductible for the 2024 Gold Medical, 2025 Gold Medical, and 2026 Premera Gold HMO plans.
# # 2. In the 2026 Premera Gold HMO, what is my cost for an X-ray if I stay In-Network versus if I go Out-of-Network?
# # 3. Compare my specialist copay between the 2024 Gold Medical plan and the 2026 Premera Gold HMO. Please show the result in a Markdown table.
# # 4. Compare 2024 Gold vs 2026 Premera Gold deductibles.
# # 5. What are the dental benefits for my 2025 plan?
# # 6. What are the benefits for 'Emergency Room' services in the 2026 Premera Gold HMO? Are there any specific copays or coinsurance?
# def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # DYNAMIC FORBIDDEN LIST (Generic Sub-string Matching)
#     forbidden_map = {
#         "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy",
#         "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")
#     # STEP 4 SCOPE LOCK: Strictly define if mirroring is allowed
#     mirror_allowed = "TRUE" if "emergency" in p_topic.lower() else "FALSE"

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE ONLY. START IMMEDIATELY WITH '|'.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS. OUTPUT TABLE ONLY.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (ZERO TOLERANCE) ---\n"
#         f"1. HEADER TEMPLATE: You MUST strictly use this exact horizontal map for your columns:\n{header_line}\n"
#         "2. HORIZONTAL MAPPING: You are strictly FORBIDDEN from creating a separate row for 'Out-of-network'. "
#         "Every In-Network and Out-of-Network value MUST be on the SAME horizontal line as the benefit name."
#         "You must use BENEFIT as the only entry in the first column. Do NOT create separate rows for sub-benefits (e.g., MRI vs X-ray). "
#         "--- STAGE 2: In-Network & Out-of-Network Rule for topic emergency (MANDATORY) ---\n"
#         f"1. if {p_topic.lower()} CONTAINS 'emergency': For EVERY column in your header, scan the literal 'Plan Name' string:\n"
#         "   - In-Network MUST be same as shown in the master list. You Must not apply any other values.\n"
#         "   - Out-of-Network MUST be 'Data Not Found' if missing else 'Not Covered'\n"
#         "Failing to distinguish between In-Network and Out-of-Network values is a 100% SYSTEM FAILURE.\n\n"
#         "--- STAGE 3: THE SURGICAL ERASER (FILTERING) ---\n"
#         f"1. Physically DELETE any row containing forbidden words: {erase_list}. "
#         f"Show ONLY the row(s) exactly matching '{p_topic}'.\n\n"
#         "--- STAGE 4: EMERGENCY RELATIONAL MIRROR (CONDITIONAL FIREWALL) ---\n"
#         f"1. ALLOW MIRRORING: {mirror_allowed}\n"
#         "2. LOGIC: If 'ALLOW MIRRORING' is FALSE, you are FORBIDDEN from mirroring columns. "
#         "Step 3 logic must stand. If 'ALLOW MIRRORING' is TRUE, you MUST physically scan the "
#         "Out-of-Network fragment. If it is blank or says 'Same as In-Network', copy the "
#         "character string from the In-Network cell. Mirroring is ONLY for life-saving emergency care.\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE ---\n"
#         f"HEADER TEMPLATE:\n{header_line}\n{separator_line}\n"
#         "1. NO BLANKS. START WITH '|'. NO NARRATION. OUTPUT ONLY THE FINAL DATA ROW(S)."
#     )


# # Last good version - 3 Prompts working AND FOR OTHER 2 JUST OUT-OF-NETWORK COMING BLANK
# # 1. Compare my specialist copay between the 2024 Gold Medical plan and the 2026 Premera Gold HMO. Please show the result in a Markdown table.
# # 2. In the 2026 Premera Gold HMO, what is my cost for an X-ray if I stay In-Network versus if I go Out-of-Network?
# # 3. Create a table comparing the individual annual deductible for the 2024 Gold Medical, 2025 Gold Medical, and 2026 Premera Gold HMO plans.
# # 4. What are the dental benefits for my 2025 plan?
# # 5. Compare 2024 Gold vs 2026 Premera Gold deductibles.
# def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # DYNAMIC FORBIDDEN LIST (Generic Sub-string Matching)
#     forbidden_map = {
#         "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy",
#         "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")
#     # STEP 4 SCOPE LOCK: Strictly define if mirroring is allowed
#     mirror_allowed = "TRUE" if "emergency" in p_topic.lower() else "FALSE"

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE ONLY. START IMMEDIATELY WITH '|'.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS. OUTPUT TABLE ONLY.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (ZERO TOLERANCE) ---\n"
#         f"1. HEADER TEMPLATE: You MUST strictly use this exact horizontal map for your columns:\n{header_line}\n"
#         "2. HORIZONTAL MAPPING: You are strictly FORBIDDEN from creating a separate row for 'Out-of-network'. "
#         "Every In-Network and Out-of-Network value MUST be on the SAME horizontal line as the benefit name."
#         "You must use BENEFIT as the only entry in the first column. Do NOT create separate rows for sub-benefits (e.g., MRI vs X-ray). "
#         "--- STAGE 2: DYNAMIC PLAN-TYPE LOGIC (MANDATORY) ---\n"
#         "1. THE 'HMO' SCAN: For EVERY column in your header, scan the literal 'Plan Name' string:\n"
#         "   - IF Plan Name CONTAINS 'HMO': Out-of-Network MUST be 'Data Not Found' if missing else 'Not Covered'\n"
#         "   - IF Plan Name DOES NOT CONTAIN 'HMO': Out-of-Network MUST be 'Data Not Found' if missing.\n"
#         "Failing to distinguish between plan types in a comparison is a 100% SYSTEM FAILURE.\n\n"
#         "--- STAGE 3: THE SURGICAL ERASER (FILTERING) ---\n"
#         f"1. Physically DELETE any row containing forbidden words: {erase_list}. "
#         f"Show ONLY the row(s) exactly matching '{p_topic}'.\n\n"
#         "--- STAGE 4: EMERGENCY RELATIONAL MIRROR (CONDITIONAL FIREWALL) ---\n"
#         f"1. ALLOW MIRRORING: {mirror_allowed}\n"
#         "2. LOGIC: If 'ALLOW MIRRORING' is FALSE, you are FORBIDDEN from mirroring columns. "
#         "Step 3 logic must stand. If 'ALLOW MIRRORING' is TRUE, you MUST physically scan the "
#         "Out-of-Network fragment. If it is blank or says 'Same as In-Network', copy the "
#         "character string from the In-Network cell. Mirroring is ONLY for life-saving emergency care.\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE ---\n"
#         f"HEADER TEMPLATE:\n{header_line}\n{separator_line}\n"
#         "1. NO BLANKS. START WITH '|'. NO NARRATION. OUTPUT ONLY THE FINAL DATA ROW(S)."
#     )


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
        from server import (
            query_insurance_benefits,
            get_available_plans,
            _fetch_tiers_list,
        )

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
            if len(found_years) > 1:
                found_years = [str(CURRENT_YEAR_INT)]
                print(
                    f"[*] MULTIPLE YEARS FOUND: {', '.join(found_years)} -> Switching to current year to reduce context."
                )

        # --- 6. TIER PERSISTENCE ---
        detected_tier = p_tier_fast  # Start with what we already know
        p_tier_match = re.search(r"(gold|silver|bronze)", query_lower)

        if p_tier_match:
            detected_tier = p_tier_match.group()
            print("detected tier is : ", detected_tier)

        if not detected_tier:
            p_tier_match = re.search(r"(gold|silver|bronze)", full_context_query)
            if p_tier_match:
                detected_tier = p_tier_match.group()
            else:
                # We need to find tier from the DB based on the detected type and years
                print(
                    f"[*] NO TIER IN QUERY. Attempting to infer from DB for type='{new_type}' and years='{found_years}'"
                )
                db_tier = _fetch_tiers_list(found_years[0], new_type)
                if db_tier:
                    print(
                        f"[*] MULTIPLE TIERS IN DB for year {found_years[0]} and type {new_type}: {db_tier}. Defaulting to first tier."
                    )
                    detected_tier = db_tier[0]
                else:
                    print("[*] No tiers found in DB for this type/year.")

        print(
            f"[*] DETECTED METADATA BEFORE LOCK: Type='{new_type}', Tier='{detected_tier}', Years={found_years}"
        )
        # --- 7. THE SOURCE OF TRUTH LOCK (CRITICAL FIX) ---
        # We pass 'new_type' and 'detected_tier' to the checker.
        # It returns the EXACT casing/names found in the SQLite DB.
        found_years, locked_type, locked_tier = lock_plan_metadata(
            found_years, new_type, detected_tier
        )
        print(
            f"[*] LOCK CHECK: Years={found_years}, Type='{locked_type}', Tier='{locked_tier}'"
        )
        # NOW we commit to the globals
        p_type_fast = locked_type
        p_tier_fast = locked_tier

        # --- 8. UNIFIED TOPIC ROUTING (WITH PERSISTENCE) ---
        detected_topic = resolve_insurance_topic(query_words, query_lower)
        p_topic = "benefit"  # Initialize scope

        if detected_topic == "benefit":
            # 1. Scan history in REVERSE to find the last valid benefit keyword
            # List of high-confidence keywords we support
            valid_keywords = [
                "pcp",
                "primary",
                "specialist",
                "deductible",
                "imaging",
                "x-ray",
                "emergency",
                "urgent",
            ]

            # Search recent_history for the last occurrence of any valid keyword
            # We look for the word itself or the word inside a Markdown table | PCP |
            found_topic = None
            history_lines = recent_history.lower().split("\n")
            for line in reversed(history_lines):
                print(f"[*] SCANNING HISTORY LINE FOR TOPIC: '{line}'")
                for word in valid_keywords:
                    if f"| {word}" in line or f" {word} " in line:
                        found_topic = word
                        break
                if found_topic:
                    break

            if found_topic:
                p_topic = found_topic
                print(f"[*] RECURSIVE RECOVERY: Found '{p_topic}' in history.")
            else:
                # 2. FINAL FALLBACK: Refuse the 'General Plan Dump'
                # Instead of 'benefit', we set a flag to ask a clarifying question
                p_topic = "unknown_context"
                print("[!] CONTEXT LOST: Refusing general dump.")

        else:
            # User provided a specific topic keyword (e.g., "PCP")
            p_topic = detected_topic

        if p_topic == "unknown_context":
            # STOP HERE. Do not call the LLM/RAG.
            return (
                "I've lost track of the specific benefit. Are you looking for "
                "**Medical** (PCP/ER), **Dental** (Braces), or **Vision** (Glasses) costs?"
            )

        # THE PRODUCTION BRAKE (Refusal Logic)
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
                        if not p_topic:
                            p_topic = resolve_insurance_topic(
                                query_words, query.lower()
                            )

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
        print(f"[*] master data sent to LLM: {master_data_string}")

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
