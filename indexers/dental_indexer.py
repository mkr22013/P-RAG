"""
Dental Benefits booklet indexer.

Structure: Schedule of Covered Services — flat rows with:
    - D-code (e.g. D0120)
    - Procedure description
    - Copayment ("Covered In Full", "$25 copay", "Covered Under Office Visit Copay", etc.)

Uses pdfplumber text extraction — the schedule is plain text, not a proper table.
Each D-code line is parsed with its copay value; wrapped continuation lines are
assembled before emitting an entry.
"""

import os
import re
import json as json_lib
import ollama
import pdfplumber

from datetime import datetime
from dotenv import load_dotenv
from utility.utils import get_smart_keywords

load_dotenv()

CURRENT_YEAR_INT = datetime.now().year

# Regex to detect the start of a new procedure line (D-code at start)
CODE_RE = re.compile(r"^(D\d{4}(?:\s*&\s*D\d{4})?)\s+(.+)", re.I)

# Copay patterns that mark the END of a procedure line
COPAY_RE = re.compile(
    r"(Covered\s+In\s+Full[\w\s,$]*|"
    r"Covered\s+Under\s+Office\s+Visit\s+Copay|"
    r"\$[\d,]+(?:\.\d+)?\s+(?:Copay|copay)[^|]*|"
    r"All\s+charges\s+in\s+excess\s+of\s+\$[\d,]+)",
    re.I,
)

# Section headers — lines with no D-code that label a service group
SECTION_HDR_RE = re.compile(
    r"^(Diagnostic|Restorative|Endodontics|Periodontics|Prosthodontics|"
    r"Oral\s+Surgery|Adjunctive|Crowns|Implants|Orthodontic|"
    r"Out\s+of\s+Area)",
    re.I,
)

# Lines to skip entirely
SKIP_RE = re.compile(
    r"^(\d+\s+Willamette|January\s+\d|^\d{7}$|^Code\s+Procedure|"
    r"^Provider\s+Network|^Office\s+Visit\s+Copay|^General\s+Office|"
    r"^Specialist\s+Office|SCHEDULE\s+OF|WHAT\s+ARE\s+MY|"
    r"^Premera\s+Employee|^Willamette\s+Dental\s+Plan$)",
    re.I,
)


def n(v):
    """Normalise whitespace."""
    return re.sub(r"\s+", " ", str(v or "")).strip()


def classify_document(pdf_path):
    """
    Read the first few pages of the Dental booklet PDF and extract:
    - year
    - group_number
    - group_name
    - plan
    - type
    - tier
    - variant
    - network
    """

    try:
        with pdfplumber.open(pdf_path) as pdf:
            header_text = ""

            # ---------------------------------------------------------
            # 🔥 READ FIRST FEW PAGES
            # ---------------------------------------------------------
            for page in pdf.pages[:3]:
                header_text += (page.extract_text() or "") + "\n"

                if len(header_text) > 4000:
                    break

        # ---------------------------------------------------------
        # 🔥 EXTRACTION PROMPT
        # ---------------------------------------------------------
        prompt = f"""
        ACT AS A STRICT STRUCTURED DATA EXTRACTOR.
        Extract ONLY if explicitly present in the text. DO NOT GUESS.

        ----------------------------------------
        🎯 FIELDS TO EXTRACT
        ----------------------------------------

        1. year
        - Extract from:
            • "Effective Date"
            • "Coverage Period"
            • Any date like "January 1, YYYY"
        - Return only the year as integer (e.g. 2025)

        2. group_number
        - Extract from:
            • "Group Number"
            • Standalone number near header
        - Return as string

        3. group_name
        - Extract from:
            • "Group Name"
        - Return full text exactly

        4. plan
        - Extract FULL plan name exactly as written
        - Examples:
            "Premera Employees Health Plan – Standard PPO Retiree Plan"
            "Heritage Prime HMO Gold 2500"

        5. type
        - Extract from explicit label OR plan name
        - Allowed values ONLY:
            HMO, PPO, EPO, HSA

        6. tier
        - Extract ONLY if explicitly present:
            Gold / Silver / Bronze / Platinum
        - Else return null

        7. variant
        - Extract plan modifiers such as:
            Standard
            Retiree
            Basic
            Plus
            HDHP
            Advantage
        - Return as string
        - If none found → return "Standard"

        8. network
        - Extract ONLY if explicitly mentioned
        - Examples:
            BlueCard Network
            National Network
            Heritage Network
            In-Network Only
        - Else return null

        ----------------------------------------
        🚫 STRICT RULES
        ----------------------------------------

        - DO NOT infer missing fields
        - DO NOT guess network from PPO/HMO
        - DO NOT fabricate tier
        - DO NOT merge unrelated fields
        - If missing → return null
        - Return STRICT JSON ONLY

        ----------------------------------------
        ✅ OUTPUT FORMAT
        ----------------------------------------

        {{
            "year": 2025,
            "group_number": "1000016",
            "group_name": "Premera Employees Health Plan",
            "plan": "Premera Employees Health Plan – Standard PPO Retiree Plan",
            "type": "PPO",
            "tier": null,
            "variant": "Retiree",
            "network": null
        }}

        ----------------------------------------
        TEXT:
        {header_text[:4000].strip()}
        """

        # ---------------------------------------------------------
        # 🔥 CALL LLM
        # ---------------------------------------------------------
        response = ollama.generate(
            model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            prompt=prompt,
            format="json",
            options={
                "temperature": 0,
            },
        )

        raw_response = response.get("response", "{}")

        print(f"[*] RAW DOCUMENT CLASSIFICATION RESPONSE: {raw_response}")

        data = json_lib.loads(raw_response)

        # ---------------------------------------------------------
        # 🔥 SAFE NORMALIZATION
        # ---------------------------------------------------------

        # YEAR
        raw_year = str(data.get("year", "")).strip()
        year_match = re.search(r"\d{4}", raw_year)

        year = int(year_match.group()) if year_match else CURRENT_YEAR_INT

        # TYPE
        plan_type = str(data.get("type", "")).strip().upper()

        allowed_types = {"HMO", "PPO", "EPO", "HSA"}

        if plan_type not in allowed_types:
            plan_type = "UNKNOWN"

        # TIER
        tier = data.get("tier")

        if tier:
            tier = str(tier).strip().capitalize()
        else:
            tier = None

        # GROUP NUMBER
        group_number = data.get("group_number")

        if group_number:
            group_number = str(group_number).strip()
        else:
            group_number = None

        # GROUP NAME
        group_name = data.get("group_name")

        if group_name:
            group_name = str(group_name).strip()
        else:
            group_name = None

        # PLAN
        plan = data.get("plan")

        if plan:
            plan = str(plan).strip()
        else:
            plan = "Unknown Plan"

        # VARIANT
        variant = data.get("variant")

        if variant:
            variant = str(variant).strip()
        else:
            variant = "Standard"

        # NETWORK
        network = data.get("network")

        if network:
            network = str(network).strip()
        else:
            network = None

        # ---------------------------------------------------------
        # 🔥 FINAL STRUCTURED OUTPUT
        # ---------------------------------------------------------
        final_data = {
            "year": year,
            "group_number": group_number,
            "group_name": group_name,
            "plan": plan,
            "type": plan_type,
            "tier": tier,
            "variant": variant,
            "network": network,
        }

        print(f"[*] FINAL DOCUMENT CLASSIFICATION: {final_data}")

        return final_data

    except Exception as e:
        print(f"[!] Dental classification failed: {e}")
        return None


def generate_sub_index(sub_index_path, pdf_path):
    """
    Parse the Dental Benefits booklet schedule into indexed entries.

    Each entry represents one D-code procedure with:
        - code        : ADA procedure code (e.g. "D0120")
        - service     : procedure description
        - category    : section header (e.g. "Diagnostic and Preventive Services")
        - copay       : what the member pays ("Covered In Full", "$25 copay", etc.)

    The schedule is in plain text — pdfplumber's extract_text() is used.
    Lines starting with a D-code are parsed; wrapped continuations are assembled
    before emitting an entry. Section headers are tracked as context.

    Output schema matches other indexers:
        event=category, service=procedure, in_network=copay, out_of_network="", limitations=""
    """

    sub_index = []
    seen = set()

    def add(code, procedure, category, copay):
        topic = f"{category} — {procedure}" if category else procedure
        content = {
            "event": category,
            "service": f"{code} {procedure}" if code else procedure,
            "in_network": copay,
            "out_of_network": "Not covered",
            "limitations": "Data not found",
        }
        key = json_lib.dumps(content, sort_keys=True)
        if key not in seen:
            seen.add(key)
            sub_index.append(
                {
                    "topic": topic,
                    "category": "cost",
                    "benefit_category": "dental",
                    "content": content,
                    "keywords": get_smart_keywords(content),
                }
            )

    # ── Extract office visit copay amounts from the schedule header ──────────
    # "General Office Visit Copayment: $15" / "Specialist Office Visit Copayment: $30"
    # These are used later to replace the phrase "Covered Under Office Visit Copay"
    # with the actual dollar amount so members see a real number, not a cross-reference.
    OFFICE_VISIT_RE = re.compile(
        r"^(General|Specialist)\s+Office\s+Visit\s+Copayment:\s+(\$[\d,]+)", re.I
    )
    office_visit_amounts = {}  # e.g. {"general": "$15", "specialist": "$30"}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Office Visit Copayment" not in text:
                continue
            for line in text.split("\n"):
                m = OFFICE_VISIT_RE.match(n(line))
                if m:
                    visit_type = m.group(1).lower()  # "general" / "specialist"
                    copay_val = m.group(2)  # "$15" / "$30"
                    office_visit_amounts[visit_type] = copay_val
                    add(
                        code="",
                        procedure=f"{m.group(1).capitalize()} Office Visit",
                        category="Office Visit Copayments",
                        copay=copay_val,
                    )
            break  # office visit copays only appear once on the schedule header page

    def resolve_copay(raw_copay):
        """
        Replace 'Covered Under Office Visit Copay' with the actual dollar amount.
        The plan uses this phrase for procedures that fall under the general ($15)
        or specialist ($30) visit copay — members need to see the real number.
        """
        if re.search(r"covered\s+under\s+office\s+visit\s+copay", raw_copay, re.I):
            # Specialist procedures are in Endodontics, Periodontics, Oral Surgery,
            # Prosthodontics — identified by the current section. For simplicity
            # we return both amounts clearly so there is no ambiguity.
            general = office_visit_amounts.get("general", "$15")
            specialist = office_visit_amounts.get("specialist", "$30")
            return f"{general} (General Visit) / {specialist} (Specialist Visit)"
        return raw_copay

    with pdfplumber.open(pdf_path) as pdf:
        current_section = ""
        pending_code = ""
        pending_proc = ""  # procedure text being assembled (may span lines)

        # Walk every page and collect all text lines
        all_lines = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.split("\n"))

        for raw_line in all_lines:
            line = n(raw_line)
            if not line:
                continue

            # Skip noise lines
            if SKIP_RE.match(line):
                if pending_code and pending_proc:
                    # Flush any in-progress entry before skipping
                    add(pending_code, n(pending_proc), current_section, "")
                    pending_code = pending_proc = ""
                continue

            # Section header — update context, flush pending
            if SECTION_HDR_RE.match(line) and not CODE_RE.match(line):
                if pending_code and pending_proc:
                    add(pending_code, n(pending_proc), current_section, "")
                    pending_code = pending_proc = ""
                current_section = line
                continue

            # Check if this line contains a D-code
            code_match = CODE_RE.match(line)

            if code_match:
                # Flush the previous pending entry
                if pending_code and pending_proc:
                    add(pending_code, n(pending_proc), current_section, "")

                code = code_match.group(1).strip()
                rest = code_match.group(2).strip()

                # Check if copay is on the same line
                copay_match = COPAY_RE.search(rest)
                if copay_match:
                    proc = rest[: copay_match.start()].strip()
                    copay = resolve_copay(copay_match.group(0).strip())
                    add(code, n(proc), current_section, copay)
                    pending_code = pending_proc = ""
                else:
                    # Copay not yet found — start accumulating
                    pending_code = code
                    pending_proc = rest

            elif pending_code:
                # Continuation of a wrapped procedure line
                combined = pending_proc + " " + line
                copay_match = COPAY_RE.search(combined)
                if copay_match:
                    proc = combined[: copay_match.start()].strip()
                    copay = resolve_copay(copay_match.group(0).strip())
                    add(pending_code, n(proc), current_section, copay)
                    pending_code = pending_proc = ""
                else:
                    # Still accumulating
                    pending_proc = combined

        # Flush any remaining entry
        if pending_code and pending_proc:
            add(pending_code, n(pending_proc), current_section, "")

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index
