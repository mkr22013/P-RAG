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
    "variant": "E4",
    "network": "",
}

TIER_LABELS = {
    "1": "Preferred Generic",
    "2": "Preferred Brand",
    "3": "Preferred Specialty",
    "4": "Non-Preferred",
    "NF": "Not on Formulary",
}

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

TIER_RE = re.compile(r"\s+(1|2|3|4|NF)(?!\d)(\s+.+)?$")

COLUMN_HEADER = "Drug Name Drug Tier Requirements / Limits"


# ── Font-size based line classification ──────────────────────────────────────
MAJOR_CATEGORY_SIZE = 14.5
SUBCATEGORY_SIZE = 12.5


def _get_line_size(words: list) -> float:
    sizes = [w.get("size", 12.0) for w in words if w.get("size", 0) > 0]
    return sum(sizes) / len(sizes) if sizes else 12.0


def _words_to_text(words: list) -> str:
    return " ".join(w["text"] for w in words).strip()


def _classify_entry_type_from_category(category: str) -> str:
    """
    Determines entry type from the subcategory name.
    No LLM needed — the booklet explicitly names device/vaccine/vitamin sections.
    """
    cat_upper = category.upper()

    if any(
        w in cat_upper
        for w in [
            "DEVICE",
            "DEVICES",
            "SUPPLIES",
            "SUPPLY",
            "EQUIPMENT",
            "MONITORS",
            "MONITORING",
        ]
    ):
        return "device"

    if any(
        w in cat_upper
        for w in [
            "VACCINE",
            "VACCINES",
            "IMMUNOLOG",
            "IMMUNIZ",
        ]
    ):
        return "vaccine"

    if any(
        w in cat_upper
        for w in [
            "VITAMIN",
            "VITAMINS",
            "HEMATINIC",
            "HEMATINICS",
            "PRENATAL",
            "SUPPLEMENT",
        ]
    ):
        return "vitamin"

    return "drug"


# ── Drug-name normalization ───────────────────────────────────────────────────

_TRAILING_ANNOTATION_RE = re.compile(
    r"\s+(?:"
    r"(?:\d+\s+)?(?:days?\)|DAYS?\)|month[s]?\)|MONTH[S]?\)|fill\)|FILL\)|PER \d+ DAYS?\))"
    r"|(?:NP|OPT|LA|SP|OCh|ACA|ST|PA)(?:\b)(?:\s*;.*)?"
    r").*$",
    re.IGNORECASE,
)

_OCR_HEADER_RE = re.compile(
    r"DDrruugg|NNaammee|TTiieerr|RReeqquuiirreemmeennttss", re.IGNORECASE
)

_TRAILING_FOOTNOTE_RE = re.compile(
    r"^(.*(?:MG|MCG|ML|GRAM|UNIT|MEQ|KCAL|LF|DU|BAU|INDX|AMB|ACTUATION|%)[^a-zA-Z0-9]*)\s+(\d{1,3})$",
    re.IGNORECASE | re.DOTALL,
)


def _is_footnote_line(line: str) -> bool:
    m = _TRAILING_FOOTNOTE_RE.match(line)
    if not m:
        return False
    return int(m.group(2)) >= 100


def _normalize_drug_name(name: str) -> str:
    if _OCR_HEADER_RE.search(name):
        return ""
    name = _TRAILING_ANNOTATION_RE.sub("", name).strip()
    m = _TRAILING_FOOTNOTE_RE.match(name)
    if m and int(m.group(2)) >= 100:
        name = m.group(1).strip()
    return name.strip()


def parse_drug_line(line: str) -> tuple | None:
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
    if not req_str.strip():
        return ""
    parts = []
    for part in req_str.split(";"):
        part = part.strip()
        abbr = part.split("(")[0].strip()
        detail = part[len(abbr) :].strip()
        label = REQUIREMENT_LABELS.get(abbr, abbr)
        parts.append(f"{label} {detail}".strip())
    return "; ".join(parts)


def find_drug_list_start(pdf) -> int:
    for page_num, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""
        if COLUMN_HEADER in text:
            return page_num
    return 6


def classify_document(pdf_path: str) -> dict:
    variant = DEV_METADATA["variant"]
    year = DEV_METADATA["year"]
    plan = "Formulary Drug List"

    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
            lines = [l.strip() for l in first_page_text.split("\n") if l.strip()]

            if lines:
                first_line = lines[0]
                paren_match = re.search(r"\(([^)]+)\)", first_line)
                if paren_match:
                    codes = paren_match.group(1).split("/")
                    variant = codes[-1].strip()
                    plan_prefix = first_line[: paren_match.start()].strip()
                    plan = f"{plan_prefix} Formulary Drug List"
                else:
                    code_match = re.search(r"\b([A-Z]\d+)\s*$", first_line)
                    if code_match:
                        variant = code_match.group(1)
                    plan = f"{first_line} Formulary Drug List"

            for line in lines[:5]:
                year_match = re.search(r"(\d{4})", line)
                if year_match:
                    year = year_match.group(1)
                    break

    except Exception as e:
        print(f"[!] classify_document failed: {e} — using DEV_METADATA")

    return {**DEV_METADATA, "plan": plan, "variant": variant, "year": year}


def build_drug_chunk(
    drug_name: str,
    tier: str,
    requirements: str,
    page: int,
    drug_category: str,
    drug_subcategory: str,
) -> dict:
    tier_label = TIER_LABELS.get(tier, tier)
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
    info_chunks = []

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

        text_lower = text.lower()
        topic = "formulary drug list"
        for phrase, mapped_topic in INTRO_TOPICS.items():
            if phrase in text_lower:
                topic = mapped_topic
                break

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
        )[:20]

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

        starts_with_index_header = lines[0].strip().lower() == "index"
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
            if starts_with_index_header or consecutive_index_like_pages >= 2:
                end_page = first_index_page - 1
                print(
                    f"[*] Rx indexer: index detected at page {first_index_page} "
                    f"— drug list ends at page {end_page}"
                )
                return end_page
        else:
            consecutive_index_like_pages = 0
            first_index_page = None

    return len(pdf.pages)


def generate_sub_index(
    output_path: str,
    pdf_path: str,
    classify_illness: bool = False,
    classify_synonyms: bool = False,
    force_reclassify: bool = False,
) -> list:
    """
    Parses the Rx formulary PDF using font-size based line classification.
    No LLM needed. Illness classification is done separately by build_rxclass_lookup.py.
    """
    chunks = []
    current_category = ""
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
                        drug_subcategory="",
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

            words = page.extract_words(extra_attrs=["size"])
            if not words:
                continue

            lines_by_y: dict = {}
            for word in words:
                y_key = round(word["top"] / 3) * 3
                if y_key not in lines_by_y:
                    lines_by_y[y_key] = []
                lines_by_y[y_key].append(word)

            for y_key in sorted(lines_by_y.keys()):
                line_words = sorted(lines_by_y[y_key], key=lambda w: w["x0"])
                line_text = _words_to_text(line_words)
                line_size = _get_line_size(line_words)

                if not line_text or line_text == COLUMN_HEADER:
                    continue
                if re.search(r"\.{2,}", line_text):
                    continue
                if line_size >= MAJOR_CATEGORY_SIZE:
                    finish_current_drug()
                    continue
                if line_size >= SUBCATEGORY_SIZE:
                    finish_current_drug()
                    current_category = line_text
                    continue

                parsed = parse_drug_line(line_text)
                if parsed:
                    finish_current_drug()
                    current_drug_name, current_drug_tier, current_drug_requirements = (
                        parsed
                    )
                    current_drug_page = page_num
                    continue

                if current_drug_name and not _is_footnote_line(line_text):
                    current_drug_name += " " + line_text

    finish_current_drug()

    all_chunks = info_chunks + chunks
    print(
        f"[*] Rx indexer: {len(chunks)} drug entries + {len(info_chunks)} info chunks "
        f"= {len(all_chunks)} total"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    update_drug_words_file(chunks)

    return all_chunks


# ── Stoplist ──────────────────────────────────────────────────────────────────

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
    "actuation",
    "actuations",
    "gram",
    "grams",
    "percent",
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
    "bacteriostat",
    "bacteriostatic",
    "preservative",
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

# ── File paths ────────────────────────────────────────────────────────────────

DRUG_WORDS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "drug_words.json",
)

CONDITION_SYNONYMS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "condition_synonyms.json",
)

# ── Entry type helpers ────────────────────────────────────────────────────────

_DEVICE_NAME_PATTERNS = {
    "test strip",
    "lancet",
    "lancets",
    "spacer",
    "condom",
    "sensor",
    "receiver",
    "transmitter",
    "lancing device",
    "diaphragm",
    "holding chamber",
    "cgm",
    "glucometer",
    "glucose meter",
    "insulin pump",
    "infusion set",
}

_VACCINE_NAME_PATTERNS = {"vaccine", "live attenuated", "inactivated virus"}

_VITAMIN_NAME_PATTERNS = {
    "prenatal",
    "natal",
    "folic acid",
    "fluoride dental",
    "multivitamin",
    "multi-vitamin",
    "vitamin k",
    "vitamin k1",
    "prenate",
    "ob complete",
    "vitafol",
    "nestabs",
}


def _classify_entry_type_from_name(drug_name: str) -> str | None:
    """Fallback entry type from drug name patterns when drug_category is unavailable."""
    name_lower = drug_name.lower()
    if any(p in name_lower for p in _DEVICE_NAME_PATTERNS):
        return "device"
    if any(p in name_lower for p in _VACCINE_NAME_PATTERNS):
        return "vaccine"
    if any(p in name_lower for p in _VITAMIN_NAME_PATTERNS):
        return "vitamin"
    return None


def _extract_drug_word(drug_name: str) -> str | None:
    """Extracts primary drug identifier word from full drug name. Must be >= 5 chars."""
    first_word = re.match(r"[a-zA-Z][a-zA-Z0-9\-]*", drug_name)
    if not first_word:
        return None

    word = first_word.group(0).lower().rstrip("-")

    if "-" in word:
        base = word.split("-")[0]
        if len(base) >= 5 and base not in _DRUG_WORD_STOPLIST_FOR_NAMES_FILE:
            word = base

    if len(word) < 5 or word in _DRUG_WORD_STOPLIST_FOR_NAMES_FILE:
        return None

    return word


def _is_valid_drug_name(drug_name: str) -> bool:
    """Returns True only for genuine drug name strings from the formulary."""
    if not drug_name or not drug_name[0].isalpha():
        return False

    words = re.findall(r"[a-zA-Z]+", drug_name)
    if not words:
        return False
    if max(len(w) for w in words) < 4:
        return False

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


def update_drug_words_file(drug_chunks: list, file_path: str = DRUG_WORDS_FILE) -> dict:
    """
    Builds and maintains drug_words.json — single source of truth for drug intelligence.

    Schema:
        {
          "metformin": {
            "entry_type": "drug",
            "full_names": ["metformin oral tablet 500 mg", ...],
            "illnesses": []    ← filled by build_rxclass_lookup.py
          }
        }

    entry_type from subcategory name — no LLM needed.
    Drug wins rule: never downgrade a word from drug to device/vitamin.
    illnesses[] preserved across re-index runs.
    """
    existing = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as e:
            print(f"[!] Failed to load drug_words.json: {e}")

    for chunk in drug_chunks:
        content = chunk.get("content", {})
        drug_name = content.get("drug_name", "").strip().lower()
        drug_category = content.get("drug_category", "").strip()

        if not drug_name or not _is_valid_drug_name(drug_name):
            continue

        drug_word = _extract_drug_word(drug_name)
        if not drug_word:
            continue

        entry_type = _classify_entry_type_from_category(drug_category)
        if entry_type == "drug":
            name_type = _classify_entry_type_from_name(drug_name)
            if name_type and name_type != "drug":
                entry_type = name_type

        if drug_word not in existing:
            existing[drug_word] = {
                "entry_type": entry_type,
                "full_names": [],
                "illnesses": [],
            }
        else:
            # Drug wins — never downgrade
            if entry_type == "drug" and existing[drug_word]["entry_type"] != "drug":
                existing[drug_word]["entry_type"] = "drug"
            if "illnesses" not in existing[drug_word]:
                existing[drug_word]["illnesses"] = []

        if drug_name not in existing[drug_word]["full_names"]:
            existing[drug_word]["full_names"].append(drug_name)

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, sort_keys=True)

    by_type: dict = {}
    for entry in existing.values():
        t = entry.get("entry_type", "drug")
        by_type[t] = by_type.get(t, 0) + 1

    print(f"[*] drug_words.json updated: {len(existing)} unique drug words")
    for t, count in sorted(by_type.items()):
        print(f"    {t}: {count}")

    return existing


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
