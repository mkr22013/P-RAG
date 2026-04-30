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
from utils import get_smart_keywords

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
    Extract plan identity from the Dental booklet cover pages using pdfplumber + LLM.
    """

    try:
        with pdfplumber.open(pdf_path) as pdf:
            header_text = ""
            for page in pdf.pages[:3]:
                header_text += (page.extract_text() or "") + "\n"
                if len(header_text) > 3000:
                    break

        prompt = f"""
            ACT AS A STRICT STRUCTURED DATA EXTRACTOR.

            Extract ONLY if explicitly present in the text.

            Rules:
            1. year: Extract from "Effective Date" or "January 1, YYYY"
            2. type: Look for plan type embedded in the name (e.g. "Dental" → DENTAL).
                Return "DENTAL" if this is a dental plan.
            3. tier: Extract Gold/Silver/Bronze if present, else return null.
            4. product_line: Full plan name as written (e.g. "Willamette Dental Plan").
            5. variant: Modifiers like "Retiree", "Standard". Else return "Standard".
            6. network: Network name if explicitly stated (e.g. "Willamette Dental Group").
                Else return null.

            RETURN STRICT JSON ONLY. Example:
            {{"year": 2024, "type": "DENTAL", "tier": null, "product_line": "Willamette Dental Plan", "variant": "Standard", "network": "Willamette Dental Group"}}

            TEXT:
            {header_text[:3000].strip()}
            """

        response = ollama.generate(
            model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            prompt=prompt,
            format="json",
            options={"temperature": 0},
        )
        data = json_lib.loads(response["response"])
        return {
            "year": int(re.sub(r"\D", "", str(data.get("year", CURRENT_YEAR_INT)))),
            "type": str(data.get("type", "DENTAL")).strip().upper(),
            "tier": str(data.get("tier", "")).strip().capitalize(),
            "product_line": str(data.get("product_line", "Dental Plan")).strip(),
            "variant": str(data.get("variant", "Standard")).strip(),
            "network": str(data.get("network", "")).strip(),
        }
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
