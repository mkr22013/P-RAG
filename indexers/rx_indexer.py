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

import re, os
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

    Headers are ALL CAPS lines with no tier number at the end and no digits.
    Real category headers never contain numbers.

    Examples that return True:
        "ANTI - INFECTIVES"       ← top-level category
        "ANTIFUNGAL AGENTS"       ← subcategory
        "ANTIVIRALS"              ← subcategory

    Examples that return False:
        "VIVJOA ORAL CAPSULE 150 MG  4  PA"    ← drug entry (has tier)
        "RECONSTITUTION 40 MG/ML"              ← continuation line (has digits)
        "200 mg/5 ml (40 mg/ml)"               ← continuation line (lowercase + digits)
        "FOR RECON 300 MG"                      ← continuation line (has digits)
    """
    if not line or TIER_RE.search(line):
        return False
    # Real category headers never contain digits
    if any(char.isdigit() for char in line):
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

    Reads the variant (E1/E4, A1/A2 etc.) and effective year directly
    from page 1 of the PDF — no LLM needed.

    Page 1 always contains:
        Line 1: "Essentials (E1/E4)" or "Open A1/Preferred A2"
        Line 2: "Formulary Drug List"
        Line 3: "Effective MM-DD-YYYY"

    In production, group_number and group_name come from blob metadata.
    In local dev, we use hardcoded DEV_METADATA values for those fields.
    """
    variant = DEV_METADATA["variant"]  # fallback
    year = DEV_METADATA["year"]  # fallback
    plan = "Formulary Drug List"

    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
            lines = [l.strip() for l in first_page_text.split("\n") if l.strip()]

            if lines:
                # Line 1: "Essentials (E1/E4)" or "Open A1/Preferred A2"
                first_line = lines[0]

                # Extract variant — take the LAST code (member is on last listed variant)
                paren_match = re.search(r"\(([^)]+)\)", first_line)
                if paren_match:
                    # "Essentials (E1/E4)" → codes=["E1","E4"] → variant="E4"
                    codes = paren_match.group(1).split("/")
                    variant = codes[-1].strip()
                    # Plan name = text before parentheses + "Formulary Drug List"
                    plan_prefix = first_line[: paren_match.start()].strip()
                    plan = f"{plan_prefix} Formulary Drug List"
                else:
                    # "Open A1/Preferred A2" → take last code: "A2"
                    code_match = re.search(r"\b([A-Z]\d+)\s*$", first_line)
                    if code_match:
                        variant = code_match.group(1)
                    plan = f"{first_line} Formulary Drug List"

            # Extract year from "Effective MM-DD-YYYY"
            for line in lines[:5]:
                year_match = re.search(r"(\d{4})", line)
                if year_match:
                    year = year_match.group(1)
                    break

    except Exception as e:
        print(f"[!] classify_document failed to read PDF: {e} — using DEV_METADATA")

    return {
        **DEV_METADATA,
        "plan": plan,
        "variant": variant,
        "year": year,
    }


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

    # Build keywords from drug name + category words only.
    # tier_label is deliberately NOT included here — it's a coverage STATUS
    # ("Not on Formulary", "Preferred Generic" etc.), not a drug identity word.
    # Including it caused ~30% of drugs to share noisy keywords like "not",
    # "formulary", "preferred", "generic" purely based on their tier bucket,
    # unrelated to what the drug actually is. tier_label is still fully
    # available in content.tier_label for anything that needs it directly.
    kw_text = f"{drug_name} {drug_category} {drug_subcategory}".lower()
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


def parse_intro_pages(pdf, drug_list_start: int) -> list:
    """
    Parses the intro pages (before the drug list) as INFO chunks.
    These pages contain:
        - What is a formulary
        - How to use the drug list
        - Tier explanations
        - Requirement abbreviation definitions (PA, QL, ST etc.)
        - Special coverage rules (ACA, OCh, OPT)

    Each page becomes one INFO chunk so members can ask general questions
    like "what is a formulary?" or "what does tier 1 mean?" and get answers
    from the actual booklet — not from LLM generation.
    """
    info_chunks = []

    # Section headers that help identify what each paragraph is about
    INTRO_TOPICS = {
        "what is the list of covered drugs": "formulary drug list",
        "formulary drug list": "formulary drug list",
        "how does the formulary": "formulary drug list",
        "will the formulary drug list change": "formulary updates",
        "preferred generic": "drug tiers",
        "preferred brand": "drug tiers",
        "preferred specialty": "drug tiers",
        "non-preferred": "drug tiers",
        "prior authorization": "prior authorization",
        "quantity limit": "quantity limit",
        "step therapy": "step therapy",
        "affordable care act": "aca preventive drugs",
        "oral chemotherapy": "oral chemotherapy",
        "optional benefit": "optional benefits",
        "formulary exception": "formulary exception",
        "not on formulary": "not on formulary",
    }

    for page_num, page in enumerate(pdf.pages[: drug_list_start - 1], start=1):
        text = page.extract_text() or ""
        if not text.strip():
            continue

        # Determine the best topic for this page based on content
        text_lower = text.lower()
        topic = "formulary drug list"  # default
        for phrase, mapped_topic in INTRO_TOPICS.items():
            if phrase in text_lower:
                topic = mapped_topic
                break

        # Build keywords from meaningful words on this page
        words = re.sub(r"[^a-z\s]", "", text_lower).split()
        keywords = list(
            dict.fromkeys(
                w
                for w in words
                if len(w) > 3
                and w
                not in {
                    "this",
                    "your",
                    "that",
                    "with",
                    "from",
                    "will",
                    "have",
                    "plan",
                    "drug",
                    "drugs",
                    "page",
                    "list",
                    "covered",
                    "coverage",
                }
            )
        )[
            :20
        ]  # top 20 keywords

        info_chunks.append(
            {
                "topic": topic,
                "category": "info",
                "benefit_category": "rx",
                "content": {
                    "event": topic.title(),
                    "service": "Coverage Information",
                    "information": text.strip(),
                },
                "keywords": keywords,
                "page_number": page_num,
            }
        )

    print(f"[*] Rx indexer: {len(info_chunks)} intro info chunks indexed")
    return info_chunks


def find_drug_list_end(pdf, drug_list_start: int) -> int:
    """
    Finds the last page of the actual drug list, before the alphabetical
    index section begins. The alphabetical index (typically at the end of
    the document) lists drug names with page-number references in a dense,
    multi-column format — this is NOT structured drug data and must be
    excluded from indexing, or it produces garbage chunks (multiple unrelated
    drug names crammed into one "drug_name" field).

    Detection signature: alphabetical index pages have a very high ratio of
    extracted text length to actual drug entries — the page is dominated by
    dozens of short "drugname ... pagenum" references rather than the normal
    one-or-two-line drug entries with dosage/tier/requirements columns.

    We detect this by checking if a page's extracted text, when split into
    drug-line candidates, produces lines that are abnormally long (multiple
    drug names per line) AND contain many bare numbers (page references).
    Once 2 consecutive pages match this signature, we treat that as the
    start of the index section and stop processing pages there.

    Returns the page number (1-based) of the LAST page to include.
    Falls back to len(pdf.pages) (process everything) if no index section
    is detected, so this is a safe, conservative addition — worst case,
    behavior is identical to before this function existed.
    """
    consecutive_index_like_pages = 0
    first_index_page = None

    for page_num in range(drug_list_start, len(pdf.pages) + 1):
        page = pdf.pages[page_num - 1]
        text = page.extract_text() or ""
        if not text.strip():
            continue

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            continue

        # Primary signal — the alphabetical index section typically starts
        # with a page literally titled "Index"
        starts_with_index_header = lines[0].strip().lower() == "index"

        # Backup signal — dense alphabetical index lines (many drug names +
        # page-number references packed into very long lines)
        long_dense_lines = sum(
            1
            for line in lines
            if len(line) > 100 and len(re.findall(r"\b\d{1,3}\b", line)) >= 3
        )
        is_dense_index_like = long_dense_lines >= max(1, len(lines) // 3)

        is_index_like = starts_with_index_header or is_dense_index_like

        if is_index_like:
            consecutive_index_like_pages += 1
            if first_index_page is None:
                first_index_page = page_num
            # "Index" header alone is reliable enough to trust immediately —
            # only require 2 consecutive pages for the weaker dense-lines signal
            if starts_with_index_header or consecutive_index_like_pages >= 2:
                end_page = first_index_page - 1
                print(
                    f"[*] Rx indexer: alphabetical index detected starting "
                    f"page {first_index_page} — drug list ends at page {end_page}"
                )
                return end_page
        else:
            consecutive_index_like_pages = 0
            first_index_page = None

    # No index section detected — process all pages (safe fallback)
    return len(pdf.pages)


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

        # Parse intro pages as INFO chunks first
        info_chunks = parse_intro_pages(pdf, drug_list_start)

        # Find where the drug list ends (before the alphabetical index section)
        drug_list_end = find_drug_list_end(pdf, drug_list_start)

        for page_num, page in enumerate(pdf.pages, start=1):
            # Skip intro pages before the drug list
            if page_num < drug_list_start:
                continue
            # Skip alphabetical index pages after the drug list ends —
            # these produce garbage chunks (multiple drug names + page
            # numbers crammed into one field) and have no useful structured data
            if page_num > drug_list_end:
                continue

            text = page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                line = line.strip()

                # Skip empty lines and the repeating column header
                if not line or line == COLUMN_HEADER:
                    continue

                # Skip alphabetical drug index lines — they use dot leaders
                # e.g. "fluconazole.......... 6  DIFLUCAN .... NF"
                # Catches 2+ dots (was 6+ before, too narrow — missed some index lines)
                if re.search(r"\.{2,}", line):
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

    # Combine intro info chunks + drug chunks
    all_chunks = info_chunks + chunks

    print(
        f"[*] Rx indexer: {len(chunks)} drug entries + {len(info_chunks)} info chunks = {len(all_chunks)} total"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    # Update the shared, plan-agnostic drug name word list used for category
    # detection and spelling correction at query time (see category.py).
    # This is intentionally a SEPARATE, lightweight file from the full index —
    # category detection only needs to know "is this word a drug name", not
    # which plan/tier/cost it belongs to. That lookup happens later, correctly,
    # via member_info once category=rx is already confirmed.
    # SAFE FIRST RUN: classify_illness=False to verify the garbage-page fix
    # and dict structure work correctly with zero LLM cost. Flip to True in
    # a separate, deliberate run once confirmed stable — see
    # RX_INDEXER_FLOW.md for the full staged rollout plan and cost warning.
    update_drug_names_file(chunks, classify_illness=False)

    return all_chunks


# Same stoplist used in category.py — kept as a separate copy here rather
# than importing from category.py, since the indexer and the runtime query
# pipeline are intentionally separate layers with no cross-dependency.
_DRUG_WORD_STOPLIST_FOR_NAMES_FILE = {
    "oral",
    "tablet",
    "tablets",
    "capsule",
    "capsules",
    "injection",
    "injectable",
    "solution",
    "suspension",
    "cream",
    "ointment",
    "gel",
    "patch",
    "spray",
    "drops",
    "extended",
    "release",
    "delayed",
    "for",
    "and",
    "with",
    "the",
    "mg",
    "ml",
    "mcg",
    "unit",
    "units",
    "per",
    "reconstitution",
    "dispersion",
    "topical",
    "inhalation",
    "nasal",
    "vaginal",
    "rectal",
    "subcutaneous",
    "intramuscular",
    "intravenous",
    "dental",
    "paste",
    "sensitive",
    "defense",
    "protect",
    "booster",
    "kids",
    "daily",
    "plus",
    "starter",
    "package",
    "kit",
    "complete",
    "implant",
    "maintenance",
    "emergency",
    "fluoride",
    "nicotine",
    # Dosage-form / administration descriptor words — found via a real
    # false-positive: "breast" (0.83 char similarity) → "breath" (from
    # "...AEROSOL POWDR BREATH ACTIVATED..." — a dosage-form descriptor,
    # not a drug identity word, exactly like "oral"/"tablet"/"capsule").
    "breath",
    "activated",
    "powder",
    "powdr",
    "aerosol",
    "dispersed",
    "dispersible",
    "chewable",
    "effervescent",
    "sublingual",
    "buccal",
    "transdermal",
    "ophthalmic",
    "otic",
    "lozenge",
    "film",
    "strip",
}

DRUG_NAMES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "drug_names.json",
)

# Common connector/category words that appear in illness descriptions but
# carry no discriminating search value on their own — e.g. "type 2 diabetes"
# should contribute "diabetes" as a keyword, not "type". Same discipline as
# the tier_label keyword-pollution fix earlier — anticipate generic words
# before they pollute the keyword space, not after finding them in production.
_ILLNESS_TERM_STOPLIST = {
    "type",
    "disease",
    "disorder",
    "condition",
    "syndrome",
    "chronic",
    "acute",
    "common",
    "general",
    "related",
}


def _classify_illness_terms(drug_word: str) -> list:
    """
    One-time mini-LLM call to classify what illness/condition a drug treats,
    in plain layman terms. Only called for drug words not already in the
    cached drug_names.json — repeat indexer runs, or other booklets
    containing the same drug, never re-classify it.

    Returns a short list of layman illness terms, filtered through the
    illness stoplist. Returns an empty list on any failure — a missing
    illness classification is not a correctness problem (see design
    discussion: this whole feature is a cost-optimization layer, not a
    correctness-critical one — worst case, the query falls through to the
    LLM category fallback at query time instead, same as for any unmapped word).
    """
    from utility.llm import llm_chat

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical reference assistant. Given a drug name, "
                    "list 1-3 common everyday conditions or illnesses it treats, "
                    "in plain layman language a patient would use (e.g. 'diabetes', "
                    "'high cholesterol', 'fungal infection'). Return ONLY a comma-"
                    "separated list, no explanation, no extra text."
                ),
            },
            {"role": "user", "content": f"Drug: {drug_word}"},
        ]
        response = llm_chat(messages=messages, max_tokens=40)
        if not response:
            return []

        terms = [t.strip().lower() for t in response.split(",") if t.strip()]
        # Filter through stoplist and length check, same discipline as
        # drug-name keyword extraction elsewhere in this file
        clean_terms = [
            t for t in terms if len(t) > 2 and t not in _ILLNESS_TERM_STOPLIST
        ]
        return clean_terms[:3]  # cap at 3 terms per drug
    except Exception as e:
        print(f"[!] Illness classification failed for {drug_word!r}: {e}")
        return []


def update_drug_names_file(
    drug_chunks: list, file_path: str = DRUG_NAMES_FILE, classify_illness: bool = True
) -> dict:
    """
    Merges newly-found drug names from this indexing run into the shared,
    plan-agnostic drug_names.json file — now structured as a dict mapping
    each drug word to its illness/condition terms, not just a flat list.

        {"metformin": ["diabetes", "blood sugar"], "ozempic": [...], ...}

    Deduplicated and classified at WRITE time (index time), so the runtime
    loader (category.py's _load_drug_name_words) never needs to do dedup
    or LLM classification — just load the file directly.

    Called once at the end of every rx_indexer.py run, for ANY booklet.
    Safe to call repeatedly — a drug word already present in the file is
    NEVER re-classified, even across different booklets or re-indexing runs,
    so the one-time LLM cost stays bounded by the true number of unique
    drug words across your entire system, not the number of indexing runs.

    classify_illness=False skips the LLM step entirely (useful for fast
    re-indexing during testing, or if illness mapping isn't needed yet) —
    drug words are still added to the file with an empty illness list,
    and can be backfilled by a later run with classify_illness=True.
    """
    new_words = set()
    for chunk in drug_chunks:
        content = chunk.get("content", {})
        drug_name = content.get("drug_name", "")
        if not drug_name:
            continue
        for word in re.findall(r"[a-zA-Z]+", drug_name.lower()):
            if len(word) > 4 and word not in _DRUG_WORD_STOPLIST_FOR_NAMES_FILE:
                new_words.add(word)

    existing_data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            print(f"[!] Failed to load existing drug_names.json: {e}")

    truly_new_words = new_words - set(existing_data.keys())

    if not truly_new_words:
        print(
            f"[*] drug_names.json unchanged: {len(existing_data)} words, no new entries"
        )
        return existing_data

    print(
        f"[*] drug_names.json: classifying {len(truly_new_words)} new words "
        f"({'with' if classify_illness else 'without'} illness mapping)..."
    )

    for word in truly_new_words:
        illness_terms = _classify_illness_terms(word) if classify_illness else []
        existing_data[word] = illness_terms

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, indent=2, sort_keys=True)

    print(
        f"[*] drug_names.json updated: +{len(truly_new_words)} new words, "
        f"{len(existing_data)} total"
    )

    return existing_data


if __name__ == "__main__":
    from indexers.run_indexer import run

    run(PLAN_CATEGORY, classify_document, generate_sub_index)

# # =============================================Previously working code before adding Rx keywords in blob================================
# # """
# # rx_indexer.py — Indexes Rx formulary drug list PDFs (E1/E4, A1/A2, any variant)

# # Booklet structure:
# #     Pages 1-5:  Intro, tier explanations, abbreviations (skipped)
# #     Page 6+:    Drug list
# #                 CATEGORY (ALL CAPS)
# #                   SUBCATEGORY (ALL CAPS)
# #                     Drug Name    Tier    Requirements

# # One chunk per drug entry. No LLM needed.
# # """

# # import re
# # import json
# # import pdfplumber

# # PLAN_CATEGORY = "rx"

# # # Dev metadata — prod gets this from blob metadata instead
# # DEV_METADATA = {
# #     "year": "2026",
# #     "group_number": "1000016",
# #     "group_name": "Premera Employees Health Plan",
# #     "plan": "Essentials Formulary Drug List",
# #     "plan_type": "",
# #     "plan_tier": "",
# #     "product_line": "",
# #     "variant": "E4",  # from member insurance card: Rx Formulary E4
# #     "network": "",
# # }

# # # Tier 1-4 for E4 plan, NF = Not on Formulary
# # TIER_LABELS = {
# #     "1": "Preferred Generic",
# #     "2": "Preferred Brand",
# #     "3": "Preferred Specialty",
# #     "4": "Non-Preferred",
# #     "NF": "Not on Formulary",
# # }

# # # Requirement abbreviation → readable text
# # REQUIREMENT_LABELS = {
# #     "PA": "Prior Authorization",
# #     "QL": "Quantity Limit",
# #     "SP": "Specialty Pharmacy",
# #     "NP": "Not Preferred",
# #     "LA": "Limited Access",
# #     "ACA": "Preventive No Cost",
# #     "ST": "Step Therapy",
# #     "OCh": "Oral Chemotherapy",
# #     "OPT": "Optional Benefit",
# #     "NF": "Not on Formulary",
# # }

# # # Matches lines ending with a tier number and optional requirements.
# # # \s+          → one or more spaces before the tier
# # # (1|2|3|4|NF) → tier: 1=Preferred Generic, 2=Brand, 3=Specialty, 4=Non-Preferred, NF=Not Covered
# # # (\s+.+)?     → optional: spaces + requirements text (e.g. "PA; SP")
# # # $            → end of line
# # #
# # # Examples:
# # #   "fluconazole oral tablet 100 mg  1"        ✓ tier=1, requirements=""
# # #   "VIVJOA ORAL CAPSULE 150 MG  4  PA"        ✓ tier=4, requirements="PA"
# # #   "ANTI - INFECTIVES"                         ✗ no match — category header
# # #   "200 mg/5 ml (40 mg/ml)"                   ✗ no match — continuation line
# # TIER_RE = re.compile(r'\s+(1|2|3|4|NF)(\s+.+)?$')

# # COLUMN_HEADER = "Drug Name Drug Tier Requirements / Limits"


# # def is_category_header(line: str) -> bool:
# #     """
# #     Detects if a line is a drug category or subcategory header.

# #     Headers are ALL CAPS lines with no tier number at the end and no digits.
# #     Real category headers never contain numbers.

# #     Examples that return True:
# #         "ANTI - INFECTIVES"       ← top-level category
# #         "ANTIFUNGAL AGENTS"       ← subcategory
# #         "ANTIVIRALS"              ← subcategory

# #     Examples that return False:
# #         "VIVJOA ORAL CAPSULE 150 MG  4  PA"    ← drug entry (has tier)
# #         "RECONSTITUTION 40 MG/ML"              ← continuation line (has digits)
# #         "200 mg/5 ml (40 mg/ml)"               ← continuation line (lowercase + digits)
# #         "FOR RECON 300 MG"                      ← continuation line (has digits)
# #     """
# #     if not line or TIER_RE.search(line):
# #         return False
# #     # Real category headers never contain digits
# #     if any(char.isdigit() for char in line):
# #         return False
# #     words = re.findall(r'[A-Za-z]+', line)
# #     return bool(words) and all(w.isupper() for w in words) and len(words) <= 8


# # def parse_drug_line(line: str) -> tuple | None:
# #     """
# #     Extracts drug entry fields from a single line.
# #     Returns (drug_name, tier, requirements) or None if not a drug entry.

# #     The tier number always appears near the end of the line, preceded by spaces.
# #     Requirements (PA, QL, SP etc.) appear after the tier if present.

# #     Examples:
# #         "fluconazole oral tablet 100 mg  1"
# #             → ("fluconazole oral tablet 100 mg", "1", "")

# #         "VIVJOA ORAL CAPSULE 150 MG  4  PA"
# #             → ("VIVJOA ORAL CAPSULE 150 MG", "4", "PA")

# #         "EDURANT ORAL TABLET 25 MG  2  QL (30 per 30 days)"
# #             → ("EDURANT ORAL TABLET 25 MG", "2", "QL (30 per 30 days)")

# #         "ANCOBON ORAL CAPSULE 250 MG, 500 MG  NF"
# #             → ("ANCOBON ORAL CAPSULE 250 MG, 500 MG", "NF", "")

# #         "200 mg/5 ml (40 mg/ml)"
# #             → None  (continuation line, no tier)
# #     """
# #     match = TIER_RE.search(line)
# #     if not match:
# #         return None
# #     tier = match.group(1)
# #     requirements = (match.group(2) or "").strip()
# #     drug_name = line[:match.start()].strip()
# #     return drug_name, tier, requirements


# # def expand_requirements(req_str: str) -> str:
# #     """
# #     Converts requirement abbreviations to readable text for display.
# #     Preserves any parenthetical quantity details after the abbreviation.

# #     Examples:
# #         "PA"                    → "Prior Authorization"
# #         "PA; SP"                → "Prior Authorization; Specialty Pharmacy"
# #         "QL (30 per 30 days)"  → "Quantity Limit (30 per 30 days)"
# #         "PA; LA"                → "Prior Authorization; Limited Access"
# #         ""                      → ""
# #     """
# #     if not req_str.strip():
# #         return ""
# #     parts = []
# #     for part in req_str.split(";"):
# #         part = part.strip()
# #         abbr = part.split("(")[0].strip()           # "QL" from "QL (30 per 30 days)"
# #         detail = part[len(abbr):].strip()            # "(30 per 30 days)"
# #         label = REQUIREMENT_LABELS.get(abbr, abbr)   # "Quantity Limit"
# #         parts.append(f"{label} {detail}".strip())
# #     return "; ".join(parts)


# # def find_drug_list_start(pdf) -> int:
# #     """
# #     Finds the first page where the drug list begins by looking for the column header.
# #     This is safer than hardcoding a page number since different booklets
# #     may have different numbers of intro pages.

# #     Returns the page number (1-based) of the first drug list page.
# #     Falls back to page 6 if the header is not found.
# #     """
# #     for page_num, page in enumerate(pdf.pages, start=1):
# #         text = page.extract_text() or ""
# #         if COLUMN_HEADER in text:
# #             return page_num
# #     return 6  # fallback if header not found


# # def classify_document(pdf_path: str) -> dict:
# #     """
# #     Returns plan metadata for this Rx formulary.

# #     Reads the variant (E1/E4, A1/A2 etc.) and effective year directly
# #     from page 1 of the PDF — no LLM needed.

# #     Page 1 always contains:
# #         Line 1: "Essentials (E1/E4)" or "Open A1/Preferred A2"
# #         Line 2: "Formulary Drug List"
# #         Line 3: "Effective MM-DD-YYYY"

# #     In production, group_number and group_name come from blob metadata.
# #     In local dev, we use hardcoded DEV_METADATA values for those fields.
# #     """
# #     variant = DEV_METADATA["variant"]  # fallback
# #     year = DEV_METADATA["year"]        # fallback
# #     plan = "Formulary Drug List"

# #     try:
# #         with pdfplumber.open(pdf_path) as pdf:
# #             first_page_text = pdf.pages[0].extract_text() or ""
# #             lines = [l.strip() for l in first_page_text.split("\n") if l.strip()]

# #             if lines:
# #                 # Line 1: "Essentials (E1/E4)" or "Open A1/Preferred A2"
# #                 first_line = lines[0]

# #                 # Extract variant — take the LAST code (member is on last listed variant)
# #                 paren_match = re.search(r'\(([^)]+)\)', first_line)
# #                 if paren_match:
# #                     # "Essentials (E1/E4)" → codes=["E1","E4"] → variant="E4"
# #                     codes = paren_match.group(1).split("/")
# #                     variant = codes[-1].strip()
# #                     # Plan name = text before parentheses + "Formulary Drug List"
# #                     plan_prefix = first_line[:paren_match.start()].strip()
# #                     plan = f"{plan_prefix} Formulary Drug List"
# #                 else:
# #                     # "Open A1/Preferred A2" → take last code: "A2"
# #                     code_match = re.search(r'\b([A-Z]\d+)\s*$', first_line)
# #                     if code_match:
# #                         variant = code_match.group(1)
# #                     plan = f"{first_line} Formulary Drug List"

# #             # Extract year from "Effective MM-DD-YYYY"
# #             for line in lines[:5]:
# #                 year_match = re.search(r'(\d{4})', line)
# #                 if year_match:
# #                     year = year_match.group(1)
# #                     break

# #     except Exception as e:
# #         print(f"[!] classify_document failed to read PDF: {e} — using DEV_METADATA")

# #     return {
# #         **DEV_METADATA,
# #         "plan":    plan,
# #         "variant": variant,
# #         "year":    year,
# #     }


# # def build_drug_chunk(drug_name: str, tier: str, requirements: str, page: int,
# #                      drug_category: str, drug_subcategory: str) -> dict:
# #     """
# #     Builds a single drug chunk dict from all the fields we have collected.

# #     Called once per drug entry — after we have accumulated all wrapped lines
# #     into a complete drug name and are ready to move on to the next entry.

# #     Returns a chunk dict ready to be appended to the index.
# #     """
# #     tier_label = TIER_LABELS.get(tier, tier)

# #     # Build keywords from drug name + category words only.
# #     # tier_label is deliberately NOT included here — it's a coverage STATUS
# #     # ("Not on Formulary", "Preferred Generic" etc.), not a drug identity word.
# #     # Including it caused ~30% of drugs to share noisy keywords like "not",
# #     # "formulary", "preferred", "generic" purely based on their tier bucket,
# #     # unrelated to what the drug actually is. tier_label is still fully
# #     # available in content.tier_label for anything that needs it directly.
# #     kw_text = f"{drug_name} {drug_category} {drug_subcategory}".lower()
# #     keywords = list(dict.fromkeys(
# #         w for w in re.sub(r'[^a-z\s]', '', kw_text).split()
# #         if len(w) > 2 and not w.isdigit()
# #     ))

# #     return {
# #         "topic": drug_name.lower(),
# #         "category": "rx",
# #         "benefit_category": "rx",
# #         "content": {
# #             "drug_name":         drug_name,
# #             "tier":              tier,
# #             "tier_label":        tier_label,
# #             "requirements":      requirements,
# #             "requirements_text": expand_requirements(requirements),
# #             "drug_category":     drug_category,
# #             "drug_subcategory":  drug_subcategory,
# #         },
# #         "keywords": keywords,
# #         "page_number": page,
# #     }


# # def parse_intro_pages(pdf, drug_list_start: int) -> list:
# #     """
# #     Parses the intro pages (before the drug list) as INFO chunks.
# #     These pages contain:
# #         - What is a formulary
# #         - How to use the drug list
# #         - Tier explanations
# #         - Requirement abbreviation definitions (PA, QL, ST etc.)
# #         - Special coverage rules (ACA, OCh, OPT)

# #     Each page becomes one INFO chunk so members can ask general questions
# #     like "what is a formulary?" or "what does tier 1 mean?" and get answers
# #     from the actual booklet — not from LLM generation.
# #     """
# #     info_chunks = []

# #     # Section headers that help identify what each paragraph is about
# #     INTRO_TOPICS = {
# #         "what is the list of covered drugs":    "formulary drug list",
# #         "formulary drug list":                  "formulary drug list",
# #         "how does the formulary":               "formulary drug list",
# #         "will the formulary drug list change":  "formulary updates",
# #         "preferred generic":                    "drug tiers",
# #         "preferred brand":                      "drug tiers",
# #         "preferred specialty":                  "drug tiers",
# #         "non-preferred":                        "drug tiers",
# #         "prior authorization":                  "prior authorization",
# #         "quantity limit":                       "quantity limit",
# #         "step therapy":                         "step therapy",
# #         "affordable care act":                  "aca preventive drugs",
# #         "oral chemotherapy":                    "oral chemotherapy",
# #         "optional benefit":                     "optional benefits",
# #         "formulary exception":                  "formulary exception",
# #         "not on formulary":                     "not on formulary",
# #     }

# #     for page_num, page in enumerate(pdf.pages[:drug_list_start - 1], start=1):
# #         text = page.extract_text() or ""
# #         if not text.strip():
# #             continue

# #         # Determine the best topic for this page based on content
# #         text_lower = text.lower()
# #         topic = "formulary drug list"  # default
# #         for phrase, mapped_topic in INTRO_TOPICS.items():
# #             if phrase in text_lower:
# #                 topic = mapped_topic
# #                 break

# #         # Build keywords from meaningful words on this page
# #         words = re.sub(r'[^a-z\s]', '', text_lower).split()
# #         keywords = list(dict.fromkeys(
# #             w for w in words
# #             if len(w) > 3 and w not in {
# #                 "this", "your", "that", "with", "from", "will", "have",
# #                 "plan", "drug", "drugs", "page", "list", "covered", "coverage"
# #             }
# #         ))[:20]  # top 20 keywords

# #         info_chunks.append({
# #             "topic":            topic,
# #             "category":         "info",
# #             "benefit_category": "rx",
# #             "content": {
# #                 "event":       topic.title(),
# #                 "service":     "Coverage Information",
# #                 "information": text.strip(),
# #             },
# #             "keywords":    keywords,
# #             "page_number": page_num,
# #         })

# #     print(f"[*] Rx indexer: {len(info_chunks)} intro info chunks indexed")
# #     return info_chunks


# # def generate_sub_index(output_path: str, pdf_path: str) -> list:
# #     """
# #     Parses the Rx formulary PDF and creates one chunk per drug entry.
# #     Writes the result as a JSON array to output_path.

# #     Key challenge: drug names often wrap across multiple lines in the PDF.
# #     We solve this by tracking a "current drug" that we build up line by line:
# #         - When we detect a new drug line → save the current drug → start tracking the new one
# #         - When we see a continuation line → append it to the current drug name
# #         - At end of file → save the last drug

# #     Each chunk contains:
# #         - drug_name: full name including strength/form (e.g. "fluconazole oral tablet 100 mg")
# #         - tier: "1", "2", "3", "4", or "NF"
# #         - tier_label: human readable tier name (e.g. "Preferred Generic")
# #         - requirements: raw abbreviations (e.g. "PA; SP")
# #         - requirements_text: expanded requirements (e.g. "Prior Authorization; Specialty Pharmacy")
# #         - drug_category: therapeutic category (e.g. "ANTI - INFECTIVES")
# #         - drug_subcategory: subcategory (e.g. "ANTIFUNGAL AGENTS")
# #         - keywords: searchable terms for scoring
# #         - page_number: PDF page where this drug appears
# #     """
# #     chunks = []
# #     current_category = ""
# #     current_subcategory = ""

# #     # Current drug being accumulated across potentially multiple lines
# #     current_drug_name = ""
# #     current_drug_tier = ""
# #     current_drug_requirements = ""
# #     current_drug_page = 0

# #     def finish_current_drug():
# #         """
# #         If we have a complete drug accumulated, build a chunk and add it to the list.
# #         Then reset the current drug fields ready for the next entry.
# #         """
# #         nonlocal current_drug_name, current_drug_tier, current_drug_requirements, current_drug_page

# #         if current_drug_name and current_drug_tier:
# #             chunk = build_drug_chunk(
# #                 drug_name=current_drug_name.strip(),
# #                 tier=current_drug_tier,
# #                 requirements=current_drug_requirements.strip(),
# #                 page=current_drug_page,
# #                 drug_category=current_category,
# #                 drug_subcategory=current_subcategory,
# #             )
# #             chunks.append(chunk)

# #         # Reset for next drug
# #         current_drug_name = ""
# #         current_drug_tier = ""
# #         current_drug_requirements = ""
# #         current_drug_page = 0

# #     with pdfplumber.open(pdf_path) as pdf:
# #         drug_list_start = find_drug_list_start(pdf)
# #         print(f"[*] Rx indexer: {len(pdf.pages)} pages, drug list starts page {drug_list_start}")

# #         # Parse intro pages as INFO chunks first
# #         info_chunks = parse_intro_pages(pdf, drug_list_start)

# #         for page_num, page in enumerate(pdf.pages, start=1):
# #             # Skip intro pages before the drug list
# #             if page_num < drug_list_start:
# #                 continue

# #             text = page.extract_text()
# #             if not text:
# #                 continue

# #             for line in text.split("\n"):
# #                 line = line.strip()

# #                 # Skip empty lines and the repeating column header
# #                 if not line or line == COLUMN_HEADER:
# #                     continue

# #                 # Skip alphabetical drug index lines — they use dot leaders
# #                 # e.g. "fluconazole.......... 6  DIFLUCAN .... NF"
# #                 # Catches 2+ dots (was 6+ before, too narrow — missed some index lines)
# #                 if re.search(r'\.{2,}', line):
# #                     continue

# #                 # Category or subcategory header (ALL CAPS, no tier)
# #                 if is_category_header(line):
# #                     finish_current_drug()
# #                     # Short lines (≤3 words) = new top-level category
# #                     # Longer lines = subcategory within current category
# #                     if not current_category or len(line.split()) <= 3:
# #                         current_category = line
# #                         current_subcategory = ""
# #                     else:
# #                         current_subcategory = line
# #                     continue

# #                 # Drug entry line — has tier number at end
# #                 parsed = parse_drug_line(line)
# #                 if parsed:
# #                     finish_current_drug()
# #                     current_drug_name, current_drug_tier, current_drug_requirements = parsed
# #                     current_drug_page = page_num
# #                     continue

# #                 # Continuation line — drug name wrapped to next line
# #                 # Append to current drug name to complete it
# #                 if current_drug_name:
# #                     current_drug_name += " " + line

# #     # Save the last drug entry after loop ends
# #     finish_current_drug()

# #     # Combine intro info chunks + drug chunks
# #     all_chunks = info_chunks + chunks

# #     print(f"[*] Rx indexer: {len(chunks)} drug entries + {len(info_chunks)} info chunks = {len(all_chunks)} total")

# #     with open(output_path, "w", encoding="utf-8") as f:
# #         json.dump(all_chunks, f, indent=2, ensure_ascii=False)

# #     return all_chunks


# # if __name__ == "__main__":
# #     from indexers.run_indexer import run
# #     run(PLAN_CATEGORY, classify_document, generate_sub_index)
