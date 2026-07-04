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

import os
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

# Matches tier number at end of line, preceded by one or more spaces.
# Negative lookahead (?!\d) prevents matching dose numbers like "2.5 mg" or "100 mg, 2 tabs".
TIER_RE = re.compile(r"\s+(1|2|3|4|NF)(?!\d)(\s+.+)?$")

COLUMN_HEADER = "Drug Name Drug Tier Requirements / Limits"


# Pharmaceutical form/route words that appear in drug names but never in
# therapeutic category headers. Used to distinguish drug name fragment lines
# (e.g. "NOXAFIL ORAL SUSP, DELAYED RELEASE") from real category headers
# (e.g. "ANTIFUNGAL AGENTS") when both are all-caps with no digits.
_DRUG_FORM_WORDS = {
    "ORAL",
    "TABLET",
    "CAPSULE",
    "SUSPENSION",
    "SOLUTION",
    "SUSP",
    "SOLN",
    "INJECTION",
    "INJECTABLE",
    "INHALATION",
    "INHALER",
    "TOPICAL",
    "OPHTHALMIC",
    "OTIC",
    "NASAL",
    "RECTAL",
    "VAGINAL",
    "SUBLINGUAL",
    "TRANSDERMAL",
    "PATCH",
    "CREAM",
    "OINTMENT",
    "GEL",
    "LOTION",
    "FOAM",
    "SPRAY",
    "DROPS",
    "SYRUP",
    "ELIXIR",
    "POWDER",
    "GRANULES",
    "PELLETS",
    "FILM",
    "INSERT",
    "SUPPOSITORY",
    "ENEMA",
    "KIT",
    "RECON",
    "RECONSTITUTION",
    "SUBCUTANEOUS",
    "INTRAMUSCULAR",
    "INTRAVENOUS",
    "DELAYED",
    "EXTENDED",
    "RELEASE",
    "DISPERSION",
    "BUCCAL",
    "IMPLANT",
    "DEVICE",
    "CARTRIDGE",
    "SYRINGE",
    "PEN",
    "AUTOINJECTOR",
    "NEBULIZATION",
    "AEROSOL",
    "MIST",
    "LOZENGE",
    "DISKHALER",
    "PRESSAIR",
    "RESPIMAT",
    "HANDIHALER",
    "FLEXHALER",
    "TWISTHALER",
    "DISKUS",
    "ELLIPTA",
    # Qualifier words that appear in drug name parentheticals — must not be
    # mistaken for category headers (e.g. "HUMIRA (ONLY NDCS STARTING WITH")
    "ONLY",
    "STARTING",
    "WITH",
    "NDCS",
    "STRENGTH",
    "PACKET",
    "DOSE",
}


def is_category_header(line: str) -> bool:
    """
    Detects if a line is a therapeutic category or subcategory header.

    Headers are ALL CAPS lines with no tier number, no digits, and no
    pharmaceutical form/route words (which only appear in drug names).

    Examples that return True:
        "ANTI - INFECTIVES"          ← top-level category
        "ANTIFUNGAL AGENTS"          ← subcategory
        "CARDIOVASCULAR AGENTS"      ← subcategory

    Examples that return False:
        "VIVJOA ORAL CAPSULE 150 MG  4  PA"    ← drug entry (has tier)
        "NOXAFIL ORAL SUSP, DELAYED RELEASE"   ← drug name fragment (has ORAL/SUSP)
        "DIFLUCAN ORAL SUSPENSION FOR"         ← drug name fragment (has ORAL/SUSPENSION)
        "RECONSTITUTION 40 MG/ML"              ← fragment (has digits)
        "200 mg/5 ml (40 mg/ml)"               ← fragment (lowercase + digits)
    """
    if not line or TIER_RE.search(line):
        return False
    # Real category headers never contain digits
    if any(char.isdigit() for char in line):
        return False
    words = re.findall(r"[A-Za-z]+", line)
    if not words or not all(w.isupper() for w in words):
        return False
    # Drug name fragments contain pharmaceutical form words — categories never do
    if any(w in _DRUG_FORM_WORDS for w in words):
        return False
    return True


# ---------------------------------------------------------------------------
# Drug-name normalization — strips trailing PDF extraction artifacts
# ---------------------------------------------------------------------------
# Trailing annotation tokens that bleed into the name field from the requirements column.
# These are requirement-column values (e.g. "days)", "MONTH); SP; LA", "FILL); NP") that
# run into the drug name field during PDF extraction.
_TRAILING_ANNOTATION_RE = re.compile(
    r"\s+(?:"
    r"(?:\d+\s+)?(?:days?\)|DAYS?\)|month[s]?\)|MONTH[S]?\)|fill\)|FILL\)|PER \d+ DAYS?\))"
    r"|(?:NP|OPT|LA|SP|OCh|ACA|ST|PA)(?:\b)(?:\s*;.*)?"
    r").*$",
    re.IGNORECASE,
)
# OCR column-header bleed: doubled characters from scanned column headers.
_OCR_HEADER_RE = re.compile(
    r"DDrruugg|NNaammee|TTiieerr|RReeqquuiirreemmeennttss", re.IGNORECASE
)

# Footnote/page-reference integer preceded by a known dose unit.
# Captures the trailing integer separately so callers can apply a
# minimum-value threshold (footnotes in this PDF are always >= 100;
# bare dose numbers like "40" or "80" at end of wrapped lines are < 100).
_TRAILING_FOOTNOTE_RE = re.compile(
    r"^(.*(?:MG|MCG|ML|GRAM|UNIT|MEQ|KCAL|LF|DU|BAU|INDX|AMB|ACTUATION|%)[^a-zA-Z0-9]*)\s+(\d{1,3})$",
    re.IGNORECASE | re.DOTALL,
)


def _is_footnote_line(line: str) -> bool:
    """
    Returns True if the line is a standalone drug name + footnote reference
    with no tier number — these are PDF artifacts that should be skipped
    during continuation accumulation.

    Key insight: footnote reference numbers in this PDF are always >= 100
    (e.g. 116, 141, 142, 143, 147, 172, 174, 118). Dose fragments that
    happen to end in a bare number (e.g. "LIPITOR ORAL TABLET 10 MG, 20 MG, 40"
    wrapping before "MG, 80 MG  4") always have small numbers (< 100).
    """
    m = _TRAILING_FOOTNOTE_RE.match(line)
    if not m:
        return False
    return int(m.group(2)) >= 100


def _normalize_drug_name(name: str) -> str:
    """
    Strips trailing PDF extraction artifacts from a raw drug name string.

    Handles three artifact types, applied in order:

    1. OCR column-header bleed — doubled characters from scanned page headers:
        "DDrruugg NNaammee DDrruugg TTiieerr..." → discard entry entirely (return "")

    2. Trailing annotation tokens — requirements-column values that bled into the name field:
        "LORBRENA ORAL TABLET 100 MG days)"             → "LORBRENA ORAL TABLET 100 MG"
        "CAYSTON ... 75 MG/ML MONTH); SP; LA"           → "CAYSTON ... 75 MG/ML"
        "EPIPEN ... 0.3 MG/0.3 ML FILL); NP 169"       → "EPIPEN ... 0.3 MG/0.3 ML"
        "FYCOMPA ORAL SUSPENSION 0.5 MG/ML 30 DAYS)"   → "FYCOMPA ORAL SUSPENSION 0.5 MG/ML"
        "HYMPAVZI PEN ... 150 MG/ML LA"                 → "HYMPAVZI PEN ... 150 MG/ML"

    3. Trailing footnote integers — page-reference numbers after a dose unit:
        "ACTOS ORAL TABLET 15 MG, 30 MG, 45 MG 116"   → "ACTOS ORAL TABLET 15 MG, 30 MG, 45 MG"
        "ACUVAIL ... DROPPERETTE 0.45 % 164"            → "ACUVAIL ... DROPPERETTE 0.45 %"
        NOT stripped when no unit precedes the integer:
        "FREESTYLE LIBRE 107"                           → kept as-is
        "LANCETS 33 GAUGE 108"                          → kept as-is
        "ERTACZO TOPICAL CREAM 91"                      → kept as-is
    """
    if _OCR_HEADER_RE.search(name):
        return ""

    # Step 2: strip trailing annotation tokens (may reveal a footnote integer afterward)
    name = _TRAILING_ANNOTATION_RE.sub("", name).strip()

    # Step 3: strip trailing footnote integer if preceded by a dose unit and >= 100
    m = _TRAILING_FOOTNOTE_RE.match(name)
    if m and int(m.group(2)) >= 100:
        name = m.group(1).strip()

    return name.strip()


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
    drug_name = _normalize_drug_name(drug_name)
    if not drug_name:
        return None
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


def generate_sub_index(
    output_path: str,
    pdf_path: str,
    classify_illness: bool = False,
    classify_synonyms: bool = False,
    force_reclassify: bool = False,
) -> list:
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
    current_drug_name = ""
    current_drug_tier = ""
    current_drug_requirements = ""
    current_drug_page = 0

    def finish_current_drug():
        nonlocal current_drug_name, current_drug_tier, current_drug_requirements, current_drug_page
        if current_drug_name and current_drug_tier:
            normalized = _normalize_drug_name(current_drug_name.strip())
            if normalized:
                chunks.append(
                    build_drug_chunk(
                        drug_name=normalized,
                        tier=current_drug_tier,
                        requirements=current_drug_requirements.strip(),
                        page=current_drug_page,
                        drug_category=current_category,
                        drug_subcategory=current_subcategory,
                    )
                )
        current_drug_name = ""
        current_drug_tier = ""
        current_drug_requirements = ""
        current_drug_page = 0

    with pdfplumber.open(pdf_path) as pdf:
        drug_list_start = find_drug_list_start(pdf)
        print(
            f"[*] Rx indexer: {len(pdf.pages)} pages, drug list starts page {drug_list_start}"
        )

        info_chunks = parse_intro_pages(pdf, drug_list_start)
        drug_list_end = find_drug_list_end(pdf, drug_list_start)

        for page_num, page in enumerate(pdf.pages, start=1):
            if page_num < drug_list_start:
                continue
            if page_num > drug_list_end:
                continue

            text = page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                line = line.strip()

                if not line or line == COLUMN_HEADER:
                    continue

                if re.search(r"\.{2,}", line):
                    continue

                if is_category_header(line):
                    finish_current_drug()
                    if not current_category or len(line.split()) <= 3:
                        current_category = line
                        current_subcategory = ""
                    else:
                        current_subcategory = line
                    continue

                parsed = parse_drug_line(line)
                if parsed:
                    finish_current_drug()
                    current_drug_name, current_drug_tier, current_drug_requirements = (
                        parsed
                    )
                    current_drug_page = page_num
                    continue

                # Continuation line — append to current drug name.
                # Skip lines that are a complete drug name + footnote number
                # (footnotes in this PDF are always >= 100; bare dose numbers
                # like "40" at end of a wrapped line are always < 100).
                if current_drug_name and not _is_footnote_line(line):
                    current_drug_name += " " + line

    finish_current_drug()

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
    # Set to False for fast structure verification (no LLM cost).
    # Flip to True for the real illness classification run.
    # Update the shared drug_names.json (Pass 1 + optional Pass 2)
    drug_names_data = update_drug_names_file(
        chunks,
        classify_illness=classify_illness,
        force_reclassify=force_reclassify,
    )

    # Pass 3: illness → synonyms (only if classify_synonyms=True)
    if classify_synonyms:
        update_condition_synonyms_file(
            drug_names_data,
            force_reclassify=force_reclassify,
        )

    return all_chunks


# Same stoplist used in category.py — kept as a separate copy here rather
# than importing from category.py, since the indexer and the runtime query
# pipeline are intentionally separate layers with no cross-dependency.
_DRUG_WORD_STOPLIST_FOR_NAMES_FILE = {
    # Dosage form / route of administration
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
    # Dosage-form / administration descriptor words — confirmed false-positive
    # sources (e.g. "breast" → "breath" collision from "BREATH ACTIVATED")
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
    # Delivery device / packaging words — appear in drug names but are not
    # drug identity words (e.g. "AIMOVIG AUTOINJECTOR", "APOKYN CARTRIDGE")
    "cartridge",
    "autoinjector",
    "applicator",
    "blister",
    "device",
    "diskhaler",
    "twisthaler",
    "inhaler",
    "injector",
    "syringe",
    "prefilled",
    "refill",
    "vial",
    "ampule",
    "ampoule",
    "prefill",
    # Dosage/measurement descriptors
    "actuation",
    "actuations",
    "gram",
    "grams",
    "percent",
    # Formulation type descriptors
    "biphasic",
    "biphase",
    "multiphase",
    "monophasic",
    "modified",
    "immediate",
    "sustained",
    "controlled",
    "enteric",
    "coated",
    "triphasic",
    "releasing",
    # Preparation / kit descriptors
    "bowel",
    "prep",
    "regimen",
    "pack",
    "combo",
    "combination",
    "augmented",
    "concentrated",
    "diluted",
    "buffered",
    # Common English words appearing in/near drug names
    "adult",
    "adults",
    "children",
    "child",
    "infant",
    "infants",
    "after",
    "extra",
    "strength",
    "maximum",
    "regular",
    "original",
    "advanced",
    "special",
    "senior",
    "junior",
    # Bacteriostat / preservative descriptors
    "bacteriostat",
    "bacteriostatic",
    "preservative",
    # Generic chemical suffixes that aren't drug identity words
    "sodium",
    "chloride",
    "phosphate",
    "sulfate",
    "sulphate",
    "hydrochloride",
    "bitartrate",
    "besylate",
    "mesylate",
    "maleate",
    "fumarate",
    "acetate",
    "tartrate",
    "citrate",
    "gluconate",
    "succinate",
    "benzoate",
    "valerate",
    "butyrate",
    "propionate",
    "decanoate",
    "undecanoate",
    "cypionate",
}

DRUG_NAMES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "drug_names.json",
)

CONDITION_SYNONYMS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "condition_synonyms.json",
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


def _classify_illness_terms_batch(drug_words: list, batch_size: int = 25) -> dict:
    """
    Batched version of illness classification — sends up to batch_size drug
    words per LLM call using a self-identifying output format, making
    misalignment detectable rather than silently corrupting the mapping.

    Output format enforced by prompt:
        metformin → diabetes, blood sugar
        fluconazole → fungal infection, yeast infection

    Each line explicitly names the drug — if LLM skips or merges entries,
    the drug name validation catches it immediately. Any word not found in
    the response simply gets an empty list (safe fallback, never corrupts).

    Roughly 25x fewer API round-trips than single-word classification,
    with identical accuracy since the self-identifying format prevents
    the misalignment risk that makes naive batching unsafe.
    """
    from utility.llm import llm_chat

    results = {word: [] for word in drug_words}

    for i in range(0, len(drug_words), batch_size):
        batch = drug_words[i : i + batch_size]
        drug_list = "\n".join(batch)

        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical terminology assistant. "
                        "For each drug name listed, return ONE line in this exact format:\n"
                        "drug_name → condition1, condition2\n\n"
                        "Rules:\n"
                        "- Use everyday patient language, NOT medical jargon\n"
                        "- Maximum 3 words per condition term\n"
                        "- Return ONLY these lines, one per drug, same order as input\n"
                        "- If unsure or not a drug name, return: drug_name → \n\n"
                        "Examples:\n"
                        "metformin → diabetes, blood sugar\n"
                        "atorvastatin → high cholesterol\n"
                        "fluconazole → fungal infection, yeast infection\n"
                        "amoxicillin → bacterial infection\n"
                        "lisinopril → high blood pressure, heart failure"
                    ),
                },
                {"role": "user", "content": drug_list},
            ]

            max_tokens = batch_size * 20  # ~20 tokens per drug in response
            response = llm_chat(messages=messages, max_tokens=max_tokens)
            if not response:
                continue

            # Parse self-identifying response lines
            for line in response.strip().split("\n"):
                if "→" not in line and "->" not in line:
                    continue
                separator = "→" if "→" in line else "->"
                parts = line.split(separator, 1)
                if len(parts) != 2:
                    continue

                drug_word = parts[0].strip().lower()
                terms_raw = parts[1].strip()

                # Verify this drug word was in our batch (prevents hallucination)
                if drug_word not in [w.lower() for w in batch]:
                    continue

                if not terms_raw:
                    continue

                terms = [t.strip().lower() for t in terms_raw.split(",") if t.strip()]
                clean_terms = [
                    t for t in terms if len(t) > 2 and t not in _ILLNESS_TERM_STOPLIST
                ]
                # Find the original-case key
                for original_word in batch:
                    if original_word.lower() == drug_word:
                        results[original_word] = clean_terms[:3]
                        break

            print(
                f"[*] Classified batch {i//batch_size + 1}/{(len(drug_words)-1)//batch_size + 1} "
                f"({min(i+batch_size, len(drug_words))}/{len(drug_words)} words)"
            )

        except Exception as e:
            print(f"[!] Batch classification failed for batch {i//batch_size + 1}: {e}")
            # Individual words in this batch keep their empty list — safe fallback

    return results


def _classify_synonyms_batch(illness_terms: list, batch_size: int = 25) -> dict:
    """
    Pass 3: For each unique illness term, generate patient-friendly synonyms.

    Batched LLM call using self-identifying format:
        diabetes → type 2, blood sugar, high blood sugar, sugar levels, T2D
        hypertension → blood pressure, high blood pressure, high bp, bp

    Returns dict: {illness_term: [synonym1, synonym2, ...]}
    Empty list = LLM returned no synonyms (safe fallback).
    """
    from utility.llm import llm_chat

    results = {term: [] for term in illness_terms}

    for i in range(0, len(illness_terms), batch_size):
        batch = illness_terms[i : i + batch_size]
        illness_list = "\n".join(batch)

        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical terminology assistant. "
                        "For each medical condition listed, return ONE line with ALL the ways "
                        "a patient might describe it — including abbreviations, common names, "
                        "and related terms.\n\n"
                        "Format: condition → synonym1, synonym2, synonym3\n\n"
                        "Rules:\n"
                        "- Use everyday patient language\n"
                        "- Include abbreviations (bp, T2D, GERD etc.)\n"
                        "- Maximum 6 synonyms per condition\n"
                        "- Return ONLY these lines, one per condition\n"
                        "- If unsure, return: condition → \n\n"
                        "Examples:\n"
                        "diabetes → blood sugar, type 2, high blood sugar, sugar levels, T2D\n"
                        "hypertension → blood pressure, high blood pressure, high bp, bp\n"
                        "high cholesterol → cholesterol, bad cholesterol, ldl, lipids\n"
                        "acid reflux → heartburn, GERD, stomach acid, indigestion"
                    ),
                },
                {"role": "user", "content": illness_list},
            ]

            max_tokens = batch_size * 30
            response = llm_chat(messages=messages, max_tokens=max_tokens)
            if not response:
                continue

            for line in response.strip().split("\n"):
                if "→" not in line and "->" not in line:
                    continue
                separator = "→" if "→" in line else "->"
                parts = line.split(separator, 1)
                if len(parts) != 2:
                    continue

                term = parts[0].strip().lower()
                synonyms_raw = parts[1].strip()

                if term not in [t.lower() for t in batch]:
                    continue

                if not synonyms_raw:
                    continue

                synonyms = [
                    s.strip().lower() for s in synonyms_raw.split(",") if s.strip()
                ]
                clean_synonyms = [s for s in synonyms if len(s) >= 2 and s != term]

                for original_term in batch:
                    if original_term.lower() == term:
                        results[original_term] = clean_synonyms[:6]
                        break

            print(
                f"[*] Synonym batch {i//batch_size + 1}/{(len(illness_terms)-1)//batch_size + 1} "
                f"({min(i+batch_size, len(illness_terms))}/{len(illness_terms)} terms)"
            )

        except Exception as e:
            print(f"[!] Synonym batch failed for batch {i//batch_size + 1}: {e}")

    return results


def _is_valid_drug_name(drug_name: str) -> bool:
    """
    Returns True only for genuine drug name strings from the formulary.
    Filters out PDF parser artifacts — dosage fragments, page numbers,
    strength continuations, packaging words — that get extracted as
    standalone drug names due to unusual PDF table layout.

    Rules (checked in order):
    1. Must start with a letter (filters "0.25 MG...", "(11)-", "3 DAY")
    2. Must contain at least one alphabetic word of 4+ characters
    3. First word must not be a known route/form/device descriptor
    4. Single-word entries must not be a known standalone artifact
    5. Must contain at least one meaningful word beyond pure descriptors
    """
    if not drug_name or not drug_name[0].isalpha():
        return False

    words = re.findall(r"[a-zA-Z]+", drug_name)
    if not words:
        return False

    # Rule 2
    if max(len(w) for w in words) < 4:
        return False

    # Rule 3
    _FIRST_WORD_ARTIFACTS = {
        "extended",
        "subcutaneous",
        "intramuscular",
        "suspension",
        "tablets",
        "rectal",
        "sprinkle",
        "soln",
        "over",
        "injection",
        "inhalation",
        "topical",
        "ophthalmic",
        "otic",
        "vaginal",
        "sublingual",
        "transdermal",
        "fluorid",
        "auto",
        "oral",
        "nasal",
        "buccal",
        "intravenous",
        "percutaneous",
        "irrigation",
        "insert",
        "pen",
        "mg",
        "mcg",
        "ml",
        "unit",
        "gram",
        "intracavernosal",
        "intrauterine",
        "mucous",
        "dental",
    }
    if words[0].lower() in _FIRST_WORD_ARTIFACTS:
        return False

    # Rule 4: single-word entries that are dosage forms or device words
    _STANDALONE_ARTIFACTS = {
        "chewable",
        "dropperette",
        "drops",
        "release",
        "reconstitution",
        "pack",
        "packet",
        "insert",
        "injector",
        "inhaler",
        "spacer",
        "device",
        "strip",
        "sensor",
        "receiver",
        "transmitter",
        "guardian",
        "lancets",
        "syringe",
        "cartridge",
        "solution",
        "suspension",
        "cream",
        "ointment",
        "gel",
        "foam",
        "lotion",
        "shampoo",
        "cleanser",
        "applicator",
        "tablet",
        "capsule",
    }
    if len(words) == 1 and words[0].lower() in _STANDALONE_ARTIFACTS:
        return False

    # Rule 5: all words are pure descriptors with no actual drug identity
    _PURE_DESCRIPTOR_WORDS = {
        "for",
        "nebulization",
        "in",
        "packet",
        "extended",
        "release",
        "delayed",
        "sustained",
        "controlled",
        "modified",
        "immediate",
        "hour",
        "hours",
        "day",
        "days",
        "week",
        "the",
        "and",
        "with",
        "iron",
        "mg",
        "mcg",
        "ml",
        "gram",
        "unit",
        "units",
        "per",
    }
    meaningful_words = [
        w
        for w in words
        if w.lower() not in _PURE_DESCRIPTOR_WORDS
        and w.lower() not in _STANDALONE_ARTIFACTS
        and w.lower() not in _FIRST_WORD_ARTIFACTS
        and len(w) >= 3
    ]
    if not meaningful_words:
        return False

    return True


def update_drug_names_file(
    drug_chunks: list,
    file_path: str = DRUG_NAMES_FILE,
    classify_illness: bool = False,
    force_reclassify: bool = False,
) -> dict:
    """
    Builds and maintains the shared, plan-agnostic drug_names.json file.

    Simple structure — full drug name as key, illness terms as value:
        {
            "ANCOBON ORAL CAPSULE 250 MG, 500 MG": ["fungal infection"],
            "fluconazole oral tablet 100 mg, 150 mg": ["fungal infection", "yeast infection"],
            "ABILIFY MAINTENA INTRAMUSCULAR SUSPENSION...": ["schizophrenia"]
        }

    Key = exact drug name string from booklet, zero manipulation.
    Value = layman illness/condition terms, classified once per entry.

    Each drug entry gets its own LLM classification with full name context
    — maximum accuracy, LLM sees the complete drug description.
    Dedup is natural — same full name string = same dict key, never duplicated.
    """
    # Extract primary drug identifier words from drug names.
    # Keys are the first meaningful word of each drug name (lowercased) —
    # e.g. "VIVJOA ORAL CAPSULE 150 MG" → "vivjoa"
    # This matches what category.py expects: individual drug words as keys.
    new_drug_names = set()
    for chunk in drug_chunks:
        content = chunk.get("content", {})
        drug_name = content.get("drug_name", "").strip()
        if drug_name and _is_valid_drug_name(drug_name):
            first_word = re.match(r"[a-zA-Z][a-zA-Z0-9\-]*", drug_name)
            if first_word:
                word = first_word.group(0).lower().rstrip("-")
                if len(word) > 2 and word not in _DRUG_WORD_STOPLIST_FOR_NAMES_FILE:
                    new_drug_names.add(word)
                    # Also add the base name before the first hyphen so
                    # "adalimumab" matches "adalimumab-aacf", "adalimumab-fkjp" etc.
                    if "-" in word:
                        base = word.split("-")[0]
                        if (
                            len(base) >= 5
                            and base not in _DRUG_WORD_STOPLIST_FOR_NAMES_FILE
                        ):
                            new_drug_names.add(base)

    # Load existing data
    existing_data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                raw = json.load(f)
            # Backward-compat: handle old formats
            if isinstance(raw, list):
                existing_data = {}  # old flat list — start fresh
            elif raw and isinstance(list(raw.values())[0], dict):
                existing_data = {}  # old grouped format — start fresh
            else:
                existing_data = raw  # current format {full_name: [illness_terms]}
        except Exception as e:
            print(f"[!] Failed to load existing drug_names.json: {e}")

    # Find truly new entries (not in file at all)
    truly_new = new_drug_names - set(existing_data.keys())

    # Find entries needing backfill (in file but empty illness terms)
    needs_backfill = set()
    if classify_illness:
        if force_reclassify:
            # Force mode — reclassify ALL existing entries too
            needs_backfill = set(new_drug_names) - truly_new
        else:
            # Incremental mode — only backfill entries with empty illness terms
            needs_backfill = {
                name
                for name in new_drug_names
                if name in existing_data and not existing_data[name]
            }

    to_classify = truly_new | needs_backfill

    if not truly_new and not needs_backfill:
        print(
            f"[*] drug_names.json unchanged: {len(existing_data)} entries, "
            f"no new drugs and no empty entries to backfill"
        )
        return existing_data

    # Add new entries with empty illness terms first
    for name in truly_new:
        existing_data[name] = []

    if classify_illness and to_classify:
        print(
            f"[*] drug_names.json: classifying {len(to_classify)} drug entries "
            f"({len(truly_new)} new, {len(needs_backfill)} backfill)..."
        )
        word_list = list(to_classify)
        batch_size = 25
        for i in range(0, len(word_list), batch_size):
            batch = word_list[i : i + batch_size]
            batch_results = _classify_illness_terms_batch(batch, batch_size=batch_size)
            for drug_name, illness_terms in batch_results.items():
                existing_data[drug_name] = illness_terms
            # Progressive save after every batch
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2, sort_keys=True)
            print(
                f"[*] Classified batch {i//batch_size + 1}/"
                f"{(len(word_list)-1)//batch_size + 1} "
                f"({min(i+batch_size, len(word_list))}/{len(word_list)} entries)"
            )
    else:
        print(
            f"[*] drug_names.json: {len(truly_new)} new entries added "
            f"(without illness mapping)"
        )

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, indent=2, sort_keys=True)

    print(
        f"[*] drug_names.json updated: +{len(truly_new)} new, "
        f"{len(needs_backfill)} backfilled, "
        f"{len(existing_data)} total entries"
    )

    return existing_data


def update_condition_synonyms_file(
    drug_names_data: dict,
    file_path: str = CONDITION_SYNONYMS_FILE,
    force_reclassify: bool = False,
) -> dict:
    """
    Pass 3: Builds and maintains condition_synonyms.json.

    Collects all unique illness terms from drug_names_data, then for each
    unique term generates patient-friendly synonyms via batched LLM calls.

    Incremental by default — only classifies terms with empty synonym lists.
    Set force_reclassify=True to redo all terms.

    Structure:
        {
            "diabetes": ["blood sugar", "type 2", "high blood sugar", "T2D"],
            "hypertension": ["blood pressure", "high blood pressure", "high bp", "bp"],
            ...
        }
    """
    # Collect all unique illness terms across all drugs
    all_illness_terms = set()
    for illness_list in drug_names_data.values():
        for term in illness_list:
            if term and len(term) >= 3:
                all_illness_terms.add(term.lower())

    if not all_illness_terms:
        print(
            "[*] condition_synonyms: no illness terms found — run with classify_illness=True first"
        )
        return {}

    print(
        f"[*] condition_synonyms: {len(all_illness_terms)} unique illness terms found"
    )

    # Load existing data
    existing_data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            print(f"[!] Failed to load existing condition_synonyms.json: {e}")

    # Determine what needs classification
    if force_reclassify:
        to_classify = list(all_illness_terms)
        print(
            f"[*] condition_synonyms: force reclassify — processing all {len(to_classify)} terms"
        )
    else:
        to_classify = [
            term
            for term in all_illness_terms
            if term not in existing_data or not existing_data[term]
        ]
        already_done = len(all_illness_terms) - len(to_classify)
        print(
            f"[*] condition_synonyms: {already_done} already classified, "
            f"{len(to_classify)} new/empty to process"
        )

    if not to_classify:
        print(
            f"[*] condition_synonyms: nothing to classify — all {len(existing_data)} terms up to date"
        )
        return existing_data

    # Classify in batches
    batch_size = 25
    for i in range(0, len(to_classify), batch_size):
        batch = to_classify[i : i + batch_size]
        batch_results = _classify_synonyms_batch(batch, batch_size=batch_size)

        for term, synonyms in batch_results.items():
            existing_data[term] = synonyms

        # Progressive save after every batch
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2, sort_keys=True)

    print(
        f"[*] condition_synonyms: complete — {len(existing_data)} conditions with synonyms"
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
