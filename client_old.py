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

    # 4. IMAGING / DIAGNOSTIC (Splitting the logic)
    if any(w in query_words for w in ["xray", "x-ray", "blood", "diagnostic"]):
        return "diagnostic"

    if any(w in query_words for w in ["mri", "pet", "scan", "imaging"]):
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



def squeeze_insurance_data(raw_text):
    if not raw_text:
        return ""

    # 1. DELETE THE BARRIERS: Remove those '---' headers and empty pipe fragments
    # This specifically removes the lines splitting your rows in the logs.
    text = re.sub(r"\|? ---.*?--- \|?", " ", raw_text)
    text = re.sub(r"\[SECTION:.*?\]|##.*?|&amp;", " ", text)
    text = re.sub(r"\|\s*\|\s*\||\|\s*\|", " | ", text)  # Clean double/triple pipes

    # 2. TOKENIZE: Split into clean lines
    lines = text.split("\n")
    lines = [line.strip() for line in lines if line.strip()]

    stitched_lines = []
    i = 0
    while i < len(lines):
        curr = lines[i]

        # 3. FRAGMENT VACUUM: If a line has no numeric data, look ahead.
        if not re.search(r"\d|%", curr) and i + 1 < len(lines):
            lookahead = ""
            # Scan the next 2 lines to find the cost info ($ or %)
            for j in range(1, 3):
                if i + j < len(lines) and re.search(r"\d|%", lines[i + j]):
                    lookahead = lines[i + j]
                    i += j
                    break

            if lookahead:
                combined = f"{curr} | {lookahead}"
                combined = combined.replace(
                    "Freestanding center:", " || Freestanding center:"
                )
                stitched_lines.append(f"| {re.sub(r'\s{2,}', ' ', combined)} |")
            else:
                stitched_lines.append(f"| {curr} |")
        else:
            curr = curr.replace("Freestanding center:", " || Freestanding center:")
            stitched_lines.append(f"| {re.sub(r'\s{2,}', ' ', curr)} |")
        i += 1

    return "\n".join(stitched_lines)


# def squeeze_insurance_data(raw_text):
#     if not raw_text:
#         return ""

#     # 1. Standardize spacing: Replace tabs and multiple newlines with single spaces
#     # but keep the primary line breaks for each benefit
#     lines = raw_text.split("\n")
#     cleaned_lines = []

#     for line in lines:
#         # Skip empty lines or purely decorative separator lines
#         if not line.strip() or "-------" in line:
#             continue

#         # 2. Collapse massive whitespace (4+ spaces) into a single pipe separator
#         # This brings the Out-of-Network values closer to the In-Network values
#         line = re.sub(r"\s{4,}", " | [OON_ANCHOR] | ", line)

#         # 3. Remove leading/trailing pipes and extra spaces
#         line = line.strip().strip("|").strip()

#         if line:
#             cleaned_lines.append(f"| {line} |")

#     return "\n".join(cleaned_lines)


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
    # 1. DYNAMIC FORBIDDEN LIST (Isolation Guard)
    forbidden_map = {
        "imaging": "Preventive, Pharmacy, Specialist, PCP, Primary, Emergency, ER, Urgent care",
        "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy, Urgent care",
        "emergency": "Urgent care, Specialist, PCP, Primary, Outpatient surgery, Hospital stay, Imaging",
        "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER, Imaging",
        "specialist": "Primary, PCP, Preventive, Deductible, Imaging, Emergency",
        "dental": "Medical, Vision, Pharmacy, Surgery, Hospital",
    }
    erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")

    return (
        f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
        "### MANDATORY: ONE SINGLE MARKDOWN TABLE FOLLOWED BY AN AUDIT LOG.\n"
        "### SILENCE: NO NARRATION, NO EXPLANATIONS BEFORE THE TABLE.\n\n"
        "--- STAGE 1: THE ARCHITECTURAL ANCHOR (HORIZONTAL LOCK) ---\n"
        f"1. HEADER TEMPLATE: Use ONLY this exact map:\n{header_line}\n"
        f"2. TOPIC PRECISION: Show ONLY benefit(s) exactly matching '{p_topic}'.\n"
        "3. HORIZONTAL BINDING: A cost value MUST be bound to the benefit name on its IMMEDIATE LEFT.\n\n"
        "--- STAGE 2: THE SURGICAL ERASER (LITERAL KILL-SWITCH) ---\n"
        f"1. BLACKLIST: You are FORBIDDEN from reporting any row containing: {erase_list}.\n"
        "2. DYNAMIC STOP: Stop scanning the moment you hit a benefit name from the blacklist.\n\n"
        "--- STAGE 3: THE 7-POINT UNIVERSAL AUDIT (SECURITY FIRST) ---\n"
        "1. **MAPPING VALIDATION & SOT FIREWALL (CRITICAL)**: \n"
        "   - **OON VALIDATION**: If any text extracted for the Out-of-Network (OON) column explicitly contains the word 'In-network', you MUST reject it as a mapping error and report 'Data Not Found' for that OON cell.\n"
        f"   - **SOT FIREWALL**: The provided fragment is your ONLY Source of Truth. You are strictly FORBIDDEN from carrying over terminology or logic from previous prompts into a '{p_topic}' table.\n"
        "2. **OON BOOLEAN STRING RULE (URGENT CARE FIX)**: If the OON segment physically contains >15 characters OR contains the words 'urgent' or 'clinic', it is MATHEMATICALLY IMPOSSIBLE for it to be only 'Not covered'. You are FORBIDDEN from summarizing. You MUST capture and report every character in that segment exactly (e.g., include both Hospital-based and Freestanding center details).\n"
        "3. **GREEDY OON EXTRACTION**: Do NOT stop your OON scan at the first keyword. Pull every character until the final closing pipe '|'.\n"
        "4. **MECHANICAL BINDING**: Copy-paste the raw block exactly. Do NOT apply internal logic or 'guess' based on history.\n"
        "5. **FRAGMENT STITCHING**: Scan all blocks. If a row is cut across lines, merge the pieces before reporting.\n"
        "6. **LITERAL MATCHING**: If the text says 'Same as In-Network', copy the In-Network string.\n"
        "7. **EVIDENCE PRIORITY**: If text exists in the OON segment, use it. 'Data Not Found' is ONLY for empty segments (unless Rule 1 applies).\n\n"
        "--- FINAL OUTPUT ARCHITECTURE (MANDATORY COMPLETION) ---\n"
        "1. START IMMEDIATELY WITH THE MARKDOWN TABLE.\n"
        f"{header_line}\n"
        f"{separator_line}\n"
        "2. MANDATORY COMPLETION: After the table, you MUST provide a section titled '### AUDIT RULES APPLIED'.\n"
        "3. YOU MUST LIST ALL SEVEN RULES (1-7) FROM STAGE 3. SPECIFICALLY CONFIRM 'OON BOOLEAN STRING RULE' WAS USED.\n"
        "4. NO OTHER TEXT. THE RESPONSE IS INCOMPLETE WITHOUT THE FULL 7-RULE AUDIT LOG."
    )


## 9 prompts working out of 10 - Urgent care out of network is wrong - We need to add a rule that if the OON segment contains the word 'urgent' or 'clinic', it cannot be 'Not covered' and we have to pull the full string.
## def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # 1. DYNAMIC FORBIDDEN LIST (Isolation Guard)
#     forbidden_map = {
#         "imaging": "Preventive, Pharmacy, Specialist, PCP, Primary, Emergency",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy, Urgent care",
#         "emergency": "Urgent care, Specialist, PCP, Primary, Outpatient surgery, Hospital stay, Imaging",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER, Imaging",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging, Emergency",
#         "dental": "Medical, Vision, Pharmacy, Surgery, Hospital",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE FOLLOWED BY AN AUDIT LOG.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS BEFORE THE TABLE.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (HORIZONTAL LOCK) ---\n"
#         f"1. HEADER TEMPLATE: Use ONLY this exact map:\n{header_line}\n"
#         f"2. TOPIC PRECISION: Show ONLY benefit(s) exactly matching '{p_topic}'.\n"
#         "3. HORIZONTAL BINDING: A cost value MUST be bound to the benefit name on its IMMEDIATE LEFT.\n\n"
#         "--- STAGE 2: THE SURGICAL ERASER (LITERAL KILL-SWITCH) ---\n"
#         f"1. BLACKLIST: You are FORBIDDEN from reporting any row containing: {erase_list}.\n"
#         "2. DYNAMIC STOP: Stop scanning the moment you hit a benefit name from the blacklist.\n\n"
#         "--- STAGE 3: THE 7-POINT UNIVERSAL AUDIT (SECURITY FIRST) ---\n"
#         "1. **MAPPING VALIDATION & SOT FIREWALL (CRITICAL)**: \n"
#         "   - **OON VALIDATION**: If any text extracted for the Out-of-Network (OON) column explicitly contains the word 'In-network', you MUST reject it as a mapping error and report 'Data Not Found' for that OON cell.\n"
#         f"   - **SOT FIREWALL**: The provided fragment is your ONLY Source of Truth. You are strictly FORBIDDEN from carrying over terminology or logic from previous prompts into a '{p_topic}' table. Including data not in the current fragment is a 100% SECURITY FAILURE.\n"
#         "2. **OON BOOLEAN STRING RULE**: If the OON segment physically contains >15 characters, it is MATHEMATICALLY IMPOSSIBLE for it to be only 'Not covered'. You MUST capture every character in the segment exactly. DO NOT SUMMARIZE.\n"
#         "3. **GREEDY OON EXTRACTION**: Do NOT stop your OON scan at the first keyword. Pull every character until the final closing pipe '|'.\n"
#         "4. **MECHANICAL BINDING**: Copy-paste the raw block exactly. Do NOT apply internal logic or 'guess' based on history.\n"
#         "5. **FRAGMENT STITCHING**: Scan all blocks. If a row is cut across lines, merge the pieces before reporting.\n"
#         "6. **LITERAL MATCHING**: If the text says 'Same as In-Network', copy the In-Network string.\n"
#         "7. **EVIDENCE PRIORITY**: If text exists in the OON segment, use it. 'Data Not Found' is ONLY for empty segments (unless Rule 1 applies).\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE (MANDATORY COMPLETION) ---\n"
#         "1. START IMMEDIATELY WITH THE MARKDOWN TABLE.\n"
#         f"{header_line}\n"
#         f"{separator_line}\n"
#         "2. MANDATORY COMPLETION: After the table, you MUST provide a section titled '### AUDIT RULES APPLIED'.\n"
#         "3. YOU MUST LIST ALL SEVEN RULES (1-7) FROM STAGE 3. SPECIFICALLY CONFIRM 'SOT FIREWALL' WAS USED.\n"
#         "4. NO OTHER TEXT. THE RESPONSE IS INCOMPLETE WITHOUT THE FULL 7-RULE AUDIT LOG."
#     )


# # Use this prompt when you want to see what rules are applied by LLM to extract the data and to make sure it is following the protocol.
# def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # 1. DYNAMIC FORBIDDEN LIST (Strict Exclusion - Unchanged)
#     forbidden_map = {
#         "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy, Visit, Physician",
#         "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
#         "dental": "Medical, Vision, Pharmacy, Surgery, Hospital",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE FOLLOWED BY AN AUDIT LOG.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS BEFORE THE TABLE.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (HORIZONTAL LOCK) ---\n"
#         f"1. HEADER TEMPLATE: Use ONLY this exact map:\n{header_line}\n"
#         f"2. TOPIC PRECISION: Show ONLY the benefit(s) exactly matching '{p_topic}'.\n"
#         "3. HORIZONTAL BINDING: A cost value MUST be bound to the benefit name on its IMMEDIATE LEFT.\n\n"
#         "--- STAGE 2: THE SURGICAL ERASER (LITERAL KILL-SWITCH) ---\n"
#         f"1. BLACKLIST: You are FORBIDDEN from reporting any row containing: {erase_list}.\n"
#         "2. DYNAMIC STOP: Stop scanning the moment you hit a benefit name from the blacklist.\n\n"
#         "--- STAGE 3: THE 6-POINT UNIVERSAL AUDIT (OON SEGMENT LOCK) ---\n"
#         "1. **OON BOOLEAN STRING RULE (CRITICAL)**: This rule applies ONLY to the Out-of-Network column segment. \n"
#         "   - **OON LENGTH CHECK**: Scan the OON segment between pipes '|'. If the segment physically contains more than 15 characters, it is MATHEMATICALLY IMPOSSIBLE for it to be only 'Not covered'. \n"
#         "   - **FULL-STRING CAPTURE**: If the OON segment is long (>15 chars), you are FORBIDDEN from summarizing. You MUST capture every character (e.g., 'Hospital-based... Freestanding...'). Reporting only 'Not covered' for a long OON string is a 100% SECURITY FAILURE.\n"
#         "2. **GREEDY OON EXTRACTION**: Do NOT stop your OON scan at the first keyword. You MUST pull every character until the final closing pipe '|' of that segment.\n"
#         "3. **MECHANICAL BINDING**: Copy-paste the raw block. Do NOT apply logic. If you see 'Hospital-based' in the OON segment, it MUST be in the table.\n"
#         "4. **FRAGMENT STITCHING**: Scan all blocks. If a row is cut, merge the pieces before reporting.\n"
#         "5. **LITERAL MATCHING**: If the text says 'Same as In-Network', copy the In-Network string.\n"
#         "6. **EVIDENCE PRIORITY**: If text exists in the OON segment, use it. 'Data Not Found' is only for empty segments.\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE (MANDATORY COMPLETION) ---\n"
#         "1. START IMMEDIATELY WITH THE MARKDOWN TABLE.\n"
#         f"   - {header_line}\n"
#         f"   - {separator_line}\n"
#         "2. MANDATORY COMPLETION: After the table, you MUST provide a section titled '### AUDIT RULES APPLIED'.\n"
#         "3. LIST ALL RULES (1-6) FROM STAGE 3. SPECIFICALLY CONFIRM THE 'BOOLEAN STRING RULE' WAS USED.\n"
#         "4. NO OTHER TEXT. THE RESPONSE IS INCOMPLETE WITHOUT THE AUDIT LOG."
#     )


# # Last stable version with 8 prompt working - Urgent care out of network is wrong
# def generate_ironclad_instruction(
#     p_tier_fast, p_type_fast, header_line, separator_line, p_topic
# ):
#     # DYNAMIC FORBIDDEN LIST (Strict Exclusion)
#     forbidden_map = {
#         "imaging": "Preventive ($0), No charge, Pharmacy, Generic, Brand, Deductible, Specialist, PCP, Primary",
#         "deductible": "Coinsurance, Specialist, PCP, Primary, Preventive, Imaging, ER, Pharmacy, Visit, Physician",
#         "emergency": "Urgent, Specialist, PCP, Primary, Preventive, Surgery, Bariatric, Weight, Cosmetic",
#         "primary": "Specialist, Preventive, Dental, Ortho, Deductible, ER",
#         "specialist": "Primary, PCP, Preventive, Deductible, Imaging",
#         "dental": "Medical, Vision, Pharmacy, Surgery, Hospital",
#     }
#     erase_list = forbidden_map.get(p_topic.lower(), "unrelated benefits")

#     return (
#         f"### ROLE: Lead Administrative Auditor ({p_tier_fast} {p_type_fast}).\n"
#         "### MANDATORY: ONE SINGLE MARKDOWN TABLE ONLY. START IMMEDIATELY WITH '|'.\n"
#         "### SILENCE: NO NARRATION, NO EXPLANATIONS. OUTPUT TABLE ONLY.\n\n"
#         "--- STAGE 1: THE ARCHITECTURAL ANCHOR (HORIZONTAL LOCK) ---\n"
#         f"1. HEADER TEMPLATE: Use ONLY this exact map:\n{header_line}\n"
#         f"2. TOPIC PRECISION: You MUST show ONLY the benefit(s) exactly matching '{p_topic}'.\n"
#         "3. HORIZONTAL BINDING: A cost value MUST be bound to the benefit name on its IMMEDIATE LEFT. "
#         "Shifting values vertically from rows above or below is a 100% SYSTEM FAILURE.\n\n"
#         "--- STAGE 2: THE SURGICAL ERASER (LITERAL KILL-SWITCH) ---\n"
#         f"1. BLACKLIST: You are strictly FORBIDDEN from reporting any row containing these words: {erase_list}.\n"
#         "2. DYNAMIC STOP: You MUST stop scanning the source text the moment you hit a benefit name from the blacklist (e.g., 'Urgent care'). "
#         "Including data from a blacklisted row is a 100% SYSTEM FAILURE.\n\n"
#         "--- STAGE 3: THE 5-POINT UNIVERSAL AUDIT (FRAGMENT RECOVERY) ---\n"
#         "1. **FRAGMENT REJECTION**: If a benefit name (e.g., 'Emergency medical transportation') appears without cost values on the same line, you MUST treat it as an incomplete fragment and ignore it.\n"
#         "2. **STITCHING PRIORITY**: You MUST scan subsequent blocks to find the complete row. Once you find the benefit name WITH its costs (e.g., '20% coinsurance'), use that full line.\n"
#         "3. **HORIZONTAL BINDING**: A value MUST be on the SAME horizontal line as the benefit name. Pulling a value from a row above is a 100% SYSTEM FAILURE.\n"
#         "4. **DYNAMIC BOUNDARY STOP**: You MUST stop the moment you hit a benefit name in the blacklist: {erase_list}. No data from that row can enter the table.\n"
#         "5. **LITERAL MATCHING**: If the text says 'Same as In-Network', copy the In-Network string for better readability.\n\n"
#         "--- FINAL OUTPUT ARCHITECTURE ---\n"
#         f"HEADER TEMPLATE:\n{header_line}\n{separator_line}\n"
#         "1. NO NARRATION. NO BLANKS. START WITH '|'. NO ASSUMPTIONS. OUTPUT ONLY THE FINAL DATA ROW(S)."
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
           
        )

        # --- 2. CONTEXT MERGING (MEMORY) ---
        recent_history = " ".join(
            [flatten_message_content(m["content"]) for m in history]
        )
        query_lower = query.lower()
        full_context_query = f"{recent_history} {query_lower}"

        # Clean words for surgical matching
        query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]
        print(f"[*] Query Words for Matching: {query_words}")

        # --- 3. SMART TOPIC & TYPE DETECTION ---
        new_type = p_type_fast  # Default to current session type

        if any(w in query_words for w in ["dental", "ortho", "braces"]):
            new_type = "dental"
            print("Detected dental keywords. Setting type to 'dental'.")
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
        else:
            new_type = "medical"  # Default fallback

        print(f"[*] Detected type based on query: {new_type}")
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
            print(
                f"[*] CACHE HIT : MASTER DATA STRING FOR SYNTHESIS:\n{master_data_string}"
            )

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
        print(f"[*] STARTING REASONING LOOP with query: '{query}'")
        print(f"[*] INITIAL MESSAGES: {messages}")
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
            print(
                f"[*] TURN {turn_count} TOOL CALLS: {tool_calls} | Clean Content Length: {len(clean_content)}"
            )
            print(f"raw_msg: {raw_msg}")

            messages.append(
                {
                    "role": "assistant",
                    "content": clean_content,
                    "tool_calls": tool_calls if tool_calls else None,
                }
            )

            if not tool_calls:
                print("[*] NO TOOL CALLS. BREAKING REASONING LOOP.")
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

        master_data_string = squeeze_insurance_data(master_data_string)
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
