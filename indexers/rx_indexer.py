"""
rx_indexer.py — Indexes Rx formulary drug list PDFs (E1/E4, A1/A2, any variant)

Booklet structure:
    Pages 1-5:  Intro, tier explanations, abbreviations (skipped)
    Page 6+:    Drug list
                CATEGORY (ALL CAPS)
                  SUBCATEGORY (ALL CAPS)
                    Drug Name    Tier    Requirements

One chunk per drug entry. No LLM needed.
"""

import re
import json
import pdfplumber

PLAN_CATEGORY = "rx"

# Dev metadata — prod gets this from blob metadata instead
DEV_METADATA = {
    "year": "2026",
    "group_number": "1000016",
    "group_name": "Premera Employees Health Plan",
    "plan": "Essentials Formulary Drug List",
    "plan_type": "",
    "plan_tier": "",
    "product_line": "",
    "variant": "E4",  # from member insurance card: Rx Formulary E4
    "network": "",
}

# Tier 1-4 for E4 plan, NF = Not on Formulary
TIER_LABELS = {
    "1": "Preferred Generic",
    "2": "Preferred Brand",
    "3": "Preferred Specialty",
    "4": "Non-Preferred",
    "NF": "Not on Formulary",
}

# Requirement abbreviation → readable text
REQUIREMENT_LABELS = {
    "PA": "Prior Authorization",
    "QL": "Quantity Limit",
    "SP": "Specialty Pharmacy",
    "NP": "Not Preferred",
    "LA": "Limited Access",
    "ACA": "Preventive No Cost",
    "ST": "Step Therapy",
    "OCh": "Oral Chemotherapy",
    "OPT": "Optional Benefit",
    "NF": "Not on Formulary",
}

# Matches lines ending with a tier number and optional requirements.
# \s+          → one or more spaces before the tier
# (1|2|3|4|NF) → tier: 1=Preferred Generic, 2=Brand, 3=Specialty, 4=Non-Preferred, NF=Not Covered
# (\s+.+)?     → optional: spaces + requirements text (e.g. "PA; SP")
# $            → end of line
#
# Examples:
#   "fluconazole oral tablet 100 mg  1"        ✓ tier=1, requirements=""
#   "VIVJOA ORAL CAPSULE 150 MG  4  PA"        ✓ tier=4, requirements="PA"
#   "ANTI - INFECTIVES"                         ✗ no match — category header
#   "200 mg/5 ml (40 mg/ml)"                   ✗ no match — continuation line
TIER_RE = re.compile(r"\s+(1|2|3|4|NF)(\s+.+)?$")

COLUMN_HEADER = "Drug Name Drug Tier Requirements / Limits"


def is_category_header(line: str) -> bool:
    """
    Detects if a line is a drug category or subcategory header.

    Headers are ALL CAPS lines with no tier number at the end.
    They group drug entries into therapeutic categories.

    Examples that return True:
        "ANTI - INFECTIVES"       ← top-level category
        "ANTIFUNGAL AGENTS"       ← subcategory
        "ANTIVIRALS"              ← subcategory

    Examples that return False:
        "VIVJOA ORAL CAPSULE 150 MG  4  PA"   ← drug entry (has tier)
        "200 mg/5 ml (40 mg/ml)"              ← continuation line (lowercase)
    """
    if not line or TIER_RE.search(line):
        return False
    words = re.findall(r"[A-Za-z]+", line)
    return bool(words) and all(w.isupper() for w in words) and len(words) <= 8


def parse_drug_line(line: str) -> tuple | None:
    """
    Extracts drug entry fields from a single line.
    Returns (drug_name, tier, requirements) or None if not a drug entry.

    The tier number always appears near the end of the line, preceded by spaces.
    Requirements (PA, QL, SP etc.) appear after the tier if present.

    Examples:
        "fluconazole oral tablet 100 mg  1"
            → ("fluconazole oral tablet 100 mg", "1", "")

        "VIVJOA ORAL CAPSULE 150 MG  4  PA"
            → ("VIVJOA ORAL CAPSULE 150 MG", "4", "PA")

        "EDURANT ORAL TABLET 25 MG  2  QL (30 per 30 days)"
            → ("EDURANT ORAL TABLET 25 MG", "2", "QL (30 per 30 days)")

        "ANCOBON ORAL CAPSULE 250 MG, 500 MG  NF"
            → ("ANCOBON ORAL CAPSULE 250 MG, 500 MG", "NF", "")

        "200 mg/5 ml (40 mg/ml)"
            → None  (continuation line, no tier)
    """
    match = TIER_RE.search(line)
    if not match:
        return None
    tier = match.group(1)
    requirements = (match.group(2) or "").strip()
    drug_name = line[: match.start()].strip()
    return drug_name, tier, requirements


def expand_requirements(req_str: str) -> str:
    """
    Converts requirement abbreviations to readable text for display.
    Preserves any parenthetical quantity details after the abbreviation.

    Examples:
        "PA"                    → "Prior Authorization"
        "PA; SP"                → "Prior Authorization; Specialty Pharmacy"
        "QL (30 per 30 days)"  → "Quantity Limit (30 per 30 days)"
        "PA; LA"                → "Prior Authorization; Limited Access"
        ""                      → ""
    """
    if not req_str.strip():
        return ""
    parts = []
    for part in req_str.split(";"):
        part = part.strip()
        abbr = part.split("(")[0].strip()  # "QL" from "QL (30 per 30 days)"
        detail = part[len(abbr) :].strip()  # "(30 per 30 days)"
        label = REQUIREMENT_LABELS.get(abbr, abbr)  # "Quantity Limit"
        parts.append(f"{label} {detail}".strip())
    return "; ".join(parts)


def find_drug_list_start(pdf) -> int:
    """
    Finds the first page where the drug list begins by looking for the column header.
    This is safer than hardcoding a page number since different booklets
    may have different numbers of intro pages.

    Returns the page number (1-based) of the first drug list page.
    Falls back to page 6 if the header is not found.
    """
    for page_num, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""
        if COLUMN_HEADER in text:
            return page_num
    return 6  # fallback if header not found


def classify_document(pdf_path: str) -> dict:
    """
    Returns plan metadata for this Rx formulary.

    Unlike medical/dental/vision indexers, Rx formularies contain no
    plan-specific details (no group number, plan name, or year in the PDF).
    In production, metadata comes from Azure Blob metadata attached to the file.
    In local development, we return hardcoded DEV_METADATA.

    The run_indexer.py will skip this function entirely in production
    when blob metadata is already available.
    """
    return DEV_METADATA.copy()


def build_drug_chunk(
    drug_name: str,
    tier: str,
    requirements: str,
    page: int,
    drug_category: str,
    drug_subcategory: str,
) -> dict:
    """
    Builds a single drug chunk dict from all the fields we have collected.

    Called once per drug entry — after we have accumulated all wrapped lines
    into a complete drug name and are ready to move on to the next entry.

    Returns a chunk dict ready to be appended to the index.
    """
    tier_label = TIER_LABELS.get(tier, tier)

    # Build keywords from drug name words + category + tier label
    # Skip very short words, punctuation, and pure numbers
    kw_text = f"{drug_name} {drug_category} {drug_subcategory} {tier_label}".lower()
    keywords = list(
        dict.fromkeys(
            w
            for w in re.sub(r"[^a-z\s]", "", kw_text).split()
            if len(w) > 2 and not w.isdigit()
        )
    )

    return {
        "topic": drug_name.lower(),
        "category": "rx",
        "benefit_category": "rx",
        "content": {
            "drug_name": drug_name,
            "tier": tier,
            "tier_label": tier_label,
            "requirements": requirements,
            "requirements_text": expand_requirements(requirements),
            "drug_category": drug_category,
            "drug_subcategory": drug_subcategory,
        },
        "keywords": keywords,
        "page_number": page,
    }


def generate_sub_index(output_path: str, pdf_path: str) -> list:
    """
    Parses the Rx formulary PDF and creates one chunk per drug entry.
    Writes the result as a JSON array to output_path.

    Key challenge: drug names often wrap across multiple lines in the PDF.
    We solve this by tracking a "current drug" that we build up line by line:
        - When we detect a new drug line → save the current drug → start tracking the new one
        - When we see a continuation line → append it to the current drug name
        - At end of file → save the last drug

    Each chunk contains:
        - drug_name: full name including strength/form (e.g. "fluconazole oral tablet 100 mg")
        - tier: "1", "2", "3", "4", or "NF"
        - tier_label: human readable tier name (e.g. "Preferred Generic")
        - requirements: raw abbreviations (e.g. "PA; SP")
        - requirements_text: expanded requirements (e.g. "Prior Authorization; Specialty Pharmacy")
        - drug_category: therapeutic category (e.g. "ANTI - INFECTIVES")
        - drug_subcategory: subcategory (e.g. "ANTIFUNGAL AGENTS")
        - keywords: searchable terms for scoring
        - page_number: PDF page where this drug appears
    """
    chunks = []
    current_category = ""
    current_subcategory = ""

    # Current drug being accumulated across potentially multiple lines
    current_drug_name = ""
    current_drug_tier = ""
    current_drug_requirements = ""
    current_drug_page = 0

    def finish_current_drug():
        """
        If we have a complete drug accumulated, build a chunk and add it to the list.
        Then reset the current drug fields ready for the next entry.
        """
        nonlocal current_drug_name, current_drug_tier, current_drug_requirements, current_drug_page

        if current_drug_name and current_drug_tier:
            chunk = build_drug_chunk(
                drug_name=current_drug_name.strip(),
                tier=current_drug_tier,
                requirements=current_drug_requirements.strip(),
                page=current_drug_page,
                drug_category=current_category,
                drug_subcategory=current_subcategory,
            )
            chunks.append(chunk)

        # Reset for next drug
        current_drug_name = ""
        current_drug_tier = ""
        current_drug_requirements = ""
        current_drug_page = 0

    with pdfplumber.open(pdf_path) as pdf:
        drug_list_start = find_drug_list_start(pdf)
        print(
            f"[*] Rx indexer: {len(pdf.pages)} pages, drug list starts page {drug_list_start}"
        )

        for page_num, page in enumerate(pdf.pages, start=1):
            # Skip intro pages before the drug list
            if page_num < drug_list_start:
                continue

            text = page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                line = line.strip()

                # Skip empty lines and the repeating column header
                if not line or line == COLUMN_HEADER:
                    continue

                # Category or subcategory header (ALL CAPS, no tier)
                if is_category_header(line):
                    finish_current_drug()
                    # Short lines (≤3 words) = new top-level category
                    # Longer lines = subcategory within current category
                    if not current_category or len(line.split()) <= 3:
                        current_category = line
                        current_subcategory = ""
                    else:
                        current_subcategory = line
                    continue

                # Drug entry line — has tier number at end
                parsed = parse_drug_line(line)
                if parsed:
                    finish_current_drug()
                    current_drug_name, current_drug_tier, current_drug_requirements = (
                        parsed
                    )
                    current_drug_page = page_num
                    continue

                # Continuation line — drug name wrapped to next line
                # Append to current drug name to complete it
                if current_drug_name:
                    current_drug_name += " " + line

    # Save the last drug entry after loop ends
    finish_current_drug()

    print(f"[*] Rx indexer: {len(chunks)} drug entries indexed")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    return chunks


if __name__ == "__main__":
    from indexers.run_indexer import run

    run(PLAN_CATEGORY, classify_document, generate_sub_index)
