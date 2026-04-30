"""
Medical Benefits booklet indexer.
Parses the 3-column 'YOUR SHARE OF THE ALLOWED AMOUNT' table using
pdfplumber with word-level bold detection to resolve the benefit tree structure.
No docling required.
"""

import re, os
import pdfplumber
import json as json_lib
import ollama

from datetime import datetime
from dotenv import load_dotenv
from utils import get_smart_keywords

load_dotenv()

CURRENT_YEAR_INT = datetime.now().year


def classify_document(pdf_path):
    """
    Read the first few pages of the Medical booklet PDF and ask the LLM
    to extract plan identity: year, type (PPO/HMO), tier, product name,
    variant, and network.
    """
    import pdfplumber

    try:
        with pdfplumber.open(pdf_path) as pdf:
            header_text = ""
            for page in pdf.pages[:3]:
                header_text += (page.extract_text() or "") + "\n"
                if len(header_text) > 4000:
                    break

        prompt = f"""
            ACT AS A STRICT STRUCTURED DATA EXTRACTOR.
            Extract ONLY if explicitly present in the text.

            Rules:
            1. year: From "Coverage Period", "Effective Date", or "January 1, YYYY"
            2. type: From explicit label OR embedded in plan name (e.g. "Standard PPO" → PPO)
                Allowed: HMO, PPO, EPO, HSA
            3. tier: Gold/Silver/Bronze if present, else null
            4. product_line: Full plan name as written
            5. variant: Modifiers like Standard, Retiree. Else "Standard"
            6. network: Only if explicitly labeled, else null

            RETURN STRICT JSON ONLY.
            Example: {"year":2025,"type":"PPO","tier":null,"product_line":"Premera Employees Health Plan","variant":"Retiree","network":null}

            TEXT: {header_text[:4000].strip()}
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
            "type": str(data.get("type", "")).strip().upper(),
            "tier": str(data.get("tier", "Gold")).strip().capitalize(),
            "product_line": str(data.get("product_line", "Plan")).strip(),
            "variant": str(data.get("variant", "Standard")).strip(),
            "network": str(data.get("network", "Standard Network")).strip(),
        }
    except Exception as e:
        print(f"[!] Medical classification failed: {e}")
        return None


def clean(text):
    """Collapse all whitespace in a string to a single space."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_benefit_cell(cell_text):
    """
    Parse the benefit column (col 0) of a table row.

    Each cell contains multi-line text. Non-bullet lines form the benefit name.
    Bullet lines (starting with •) are individual services.
    Wrapped continuation lines (no bullet) are appended to the service above them.

    Cross-references like "(See X benefit for details.)" are stripped
    because they add noise without adding meaning.

    Notes and descriptions appended to service names are also stripped —
    e.g. "Facility charges You may have additional costs..." → "Facility charges"

    Returns:
        benefit  : the top-level benefit name, e.g. "Emergency Room"
        services : list of clean service names, e.g. ["Facility charges", "Professional services"]
    """
    CROSS_REF = re.compile(r"\s*\(See.*", re.I)

    # Lines that begin a description — stop adding to benefit name when seen
    BENEFIT_STOP = re.compile(
        r"^(you\s+may|the\s+copay|this\s+plan|see\s+the\s+|covers\s+routine|"
        r"includes\s+|for\s+permanent|care\s+during|calendar\s+year|day\s+limit|"
        r"visit\s+limit|lifetime\s+limit|limited\s+as|for\s+member|travel\s+and|"
        r"interactive\s+audio|benefits\s+are\s+limited)",
        re.I,
    )

    # Patterns to strip from the END of a service name
    SERVICE_STOP = re.compile(
        r"\s+(calendar\s+year|no\s+limit|day\s+limit|visit\s+limit|you\s+may|"
        r"the\s+copay|this\s+plan|see\s+those|no\s+charge\s+on|\*all\s+approved|"
        r"also\s+covered|limit\s+per|limit\s+\$|special\s+criteria|"
        r"for\s+coverage\s+details|virtual\s+pediatric|for\s+members\s+\d).*",
        re.I,
    )

    benefit = ""
    benefit_done = (
        False  # once we hit a bullet or description, stop adding to benefit name
    )
    services = []
    lines = cell_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        if line.startswith("•"):
            # --- BULLET LINE: start of a new service item ---
            benefit_done = True
            item = re.sub(r"^•\s*", "", line)  # strip the bullet character
            i += 1

            # Absorb continuation lines that belong to this bullet
            # (lines that don't start a new bullet)
            while i < len(lines) and not lines[i].strip().startswith("•"):
                cont = lines[i].strip()
                # Skip cross-reference lines — they are noise
                if cont and not CROSS_REF.match(cont):
                    item += " " + cont
                i += 1

            # Strip "(See ...)" cross-references from anywhere in the item
            item = CROSS_REF.sub("", item).strip()

            # Strip trailing description notes from the service name
            item = SERVICE_STOP.sub("", item).strip()

            if item:
                services.append(clean(item))

        else:
            # --- NON-BULLET LINE: part of the benefit name (until a description starts) ---
            if not benefit_done and not BENEFIT_STOP.match(line):
                benefit = (benefit + " " + line).strip()
            i += 1

    return benefit, services


def parse_cost_column(cell_text):
    """
    Split a cost cell into an ordered list of individual cost values.

    Each new cost starts with a recognisable token like "$25 copay",
    "Deductible, then 20%", "No charge", "Not covered", "Kinwell", etc.
    Lines that don't start a new cost are wrapped continuations and
    are appended to the current cost value.

    Kinwell / All Other tier lines are merged into the preceding cost
    entry because they describe pricing tiers for the SAME service.

    Example:
        Input  → "Kinwell Clinics: $0 copay\ndeductible waived\nAll Other: $25 copay"
        Output → ["Kinwell Clinics: $0 copay deductible waived  All Other: $25 copay"]
    """
    COST_START = re.compile(
        r"^(\$\d|\d+%|no\s+charge|not\s+covered|no\s+cost|"
        r"deductible,\s*then|kinwell|all\s+other)",
        re.I,
    )
    TIER_LINE = re.compile(r"^(kinwell|all\s+other)", re.I)

    costs = []
    current = ""

    for line in cell_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if COST_START.match(line):
            if current:
                costs.append(current)
            current = line
        elif current:
            current += " " + line  # wrapped continuation

    if current:
        costs.append(current)

    # Merge consecutive Kinwell / All Other tier entries into one string
    # so tiered pricing appears as a single readable cost value
    merged = []
    for cost in costs:
        if merged and TIER_LINE.match(cost) and TIER_LINE.match(merged[-1]):
            merged[-1] += "  " + cost
        else:
            merged.append(cost)

    return merged


def generate_sub_index(sub_index_path, pdf_path):
    """
    Parse the Medical Benefits booklet and write a structured index file.

    Only pages containing "YOUR SHARE OF THE ALLOWED AMOUNT" are processed —
    these are the benefit cost table pages (pages 11-23 in a typical booklet).

    Table structure:
        Col 0 : Benefit name + bullet services (multi-line text)
        Col 3 : In-network costs   (9-col layout)
        Col 6 : Out-of-network costs
        Col 1/2 used as fallback for simpler 3-col layouts

    For each data row:
        1. Parse col 0 → benefit name + list of services
        2. Parse col 3 and col 6 → ordered cost lists
        3. Pair each service with its cost by position
            If fewer costs than services, the last cost is reused (inherited)

    Page-continuation rows: when col 0 has no benefit name (PDF splits a row
    across pages), the last seen benefit name is reused.
    """

    sub_index = []
    seen = set()

    def add(topic, content):
        """Add an entry to the index, skipping duplicates."""
        key = json_lib.dumps(content, sort_keys=True)
        if key not in seen:
            seen.add(key)
            sub_index.append(
                {
                    "topic": topic,
                    "category": "cost",
                    "benefit_category": "medical",
                    "content": content,
                    "keywords": get_smart_keywords(content),
                }
            )

    last_benefit = ""  # carries the benefit name across page-continuation rows

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []

            # Skip pages that are not benefit cost tables
            all_cell_text = " ".join(
                clean(str(c or "")) for t in tables for r in t for c in r
            ).upper()
            page_text = (page.extract_text() or "").upper()
            if (
                "YOUR SHARE OF THE ALLOWED AMOUNT" not in all_cell_text
                and "YOUR SHARE OF THE ALLOWED AMOUNT" not in page_text
            ):
                continue

            for table in tables:
                for row_idx, row in enumerate(table):
                    # Rows 0 and 1 are column headers — skip them
                    if row_idx < 2:
                        continue

                    # --- EXTRACT THE THREE DATA COLUMNS ---
                    # 9-col layout (most pages): data at positions 0, 3, 6
                    # 3-col layout (some pages): data at positions 0, 1, 2
                    ncols = len(row)
                    benefit_cell = str(row[0] or "")
                    in_net_cell = str(
                        row[3] if ncols > 6 else (row[1] if ncols > 1 else "")
                    )
                    out_net_cell = str(
                        row[6] if ncols > 6 else (row[2] if ncols > 2 else "")
                    )

                    # Skip rows with no content
                    if not benefit_cell.strip():
                        continue
                    if not in_net_cell.strip() and not out_net_cell.strip():
                        continue

                    # --- PARSE THE BENEFIT COLUMN ---
                    benefit, services = parse_benefit_cell(benefit_cell)

                    # Handle page-continuation rows where the benefit name is missing.
                    # The PDF sometimes splits a benefit across two pages, putting the
                    # name only on the first page.
                    if not benefit and last_benefit:
                        benefit = last_benefit
                    if benefit:
                        last_benefit = benefit

                    # --- INDEX ROWS WITH NO BULLET SERVICES ---
                    # e.g. "Allergy Testing And Treatment" — a single-service benefit
                    # with no sub-items. The benefit itself is the service.
                    if not services:
                        if benefit:
                            # Strip trailing limit notes from the benefit name
                            TRAIL = re.compile(
                                r"\s+(calendar\s+year|lifetime\s+limit|day\s+limit|"
                                r"visit\s+limit|you\s+may|the\s+copay|see\s+the).*",
                                re.I,
                            )
                            svc = TRAIL.sub("", benefit).strip()
                            in_c = parse_cost_column(in_net_cell)
                            out_c = parse_cost_column(out_net_cell)
                            add(
                                svc,
                                {
                                    "event": svc,
                                    "service": svc,
                                    "in_network": (
                                        in_c[0] if in_c else clean(in_net_cell)
                                    ),
                                    "out_of_network": (
                                        out_c[0] if out_c else clean(out_net_cell)
                                    ),
                                    "limitations": "",
                                },
                            )
                        continue

                    # --- INDEX ROWS WITH BULLET SERVICES ---
                    in_costs = parse_cost_column(in_net_cell)
                    out_costs = parse_cost_column(out_net_cell)

                    # Pair each service with its cost by index.
                    # If there are fewer costs than services (e.g. two services share
                    # one cost row in the PDF), the last seen cost is inherited.
                    last_in = ""
                    last_out = ""
                    for idx, service in enumerate(services):
                        if idx < len(in_costs):
                            last_in = in_costs[idx]
                        if idx < len(out_costs):
                            last_out = out_costs[idx]
                        add(
                            f"{benefit} \u2014 {service}",
                            {
                                "event": benefit,
                                "service": service,
                                "in_network": last_in,
                                "out_of_network": last_out,
                                "limitations": "",
                            },
                        )

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# BUILD ALL — folder-aware pivot
# ═══════════════════════════════════════════════════════
