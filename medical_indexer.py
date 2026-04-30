"""
Medical Benefits booklet indexer.
Parses the 3-column 'YOUR SHARE OF THE ALLOWED AMOUNT' table using
pdfplumber with word-level bold detection to resolve the benefit tree structure.
No docling required.
"""

import re, os
import json as json_lib
import ollama
import pdfplumber

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
            Example: {{"year":2025,"type":"PPO","tier":null,"product_line":"Premera Employees Health Plan","variant":"Retiree","network":null}}

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


def get_subsection_headers(page):
    """
    Return the set of first-words that follow bold bullets (•) in the benefit
    column. Bold bullets are sub-section headers, not leaf services.

    Uses page.chars for bullet y-positions (reliable) and extract_words for
    bold font detection (reliable). Combining both works around pdfplumber
    not always tokenizing '•' as a separate word.

    Returns empty set on pages with no bold bullets (flat structure).
    """
    # Find y-positions of all bullets in the benefit column
    bullet_ys = [c["top"] for c in page.chars if c["text"] == "•" and c["x0"] < 200]
    if not bullet_ys:
        return set()

    # Find bold words at those y-positions
    starters = set()
    for word in page.extract_words(extra_attrs=["fontname"]):
        if word["x0"] >= 200:
            continue
        for by in bullet_ys:
            if abs(word["top"] - by) <= 4 and "Bold" in word.get("fontname", ""):
                starters.add(word["text"])
                break
    return starters


def parse_benefit_cell(cell_text, bold_headers=None):
    """
    Extract the benefit name and flat list of leaf services from col 0.

    BENEFIT NAME  — non-bullet lines at the top, until a bullet or
                    a description line (starts with "See ", "Includes ", etc.)

    LEAF SERVICES — bullet lines that are NOT headers. A bullet is a header
                    (and gets skipped) when ANY of these are true:
                    1. Contains "(See ...)"       — cross-reference
                    2. First word is in bold_headers — bold font on page
                    3. Starts with "For "         — sub-group label
                    4. Has a limit-note continuation (calendar year, day limit...)
                        AND the next item is also a bullet
                        AND the bullet text is a generic category word
                        (e.g. "Outpatient care", "Inpatient care")

    NON-BULLET LINES with "$" after the benefit name are also collected as
    services — they are bold service labels like "For transplants: $7,500".
    """
    if bold_headers is None:
        bold_headers = set()

    # --- Patterns ---
    CROSS_REF = re.compile(r"\s*\(See.*", re.I)
    LIMIT_NOTE = re.compile(
        r"^(calendar\s+year|day\s+limit|visit\s+limit|no\s+limit|limited\s+to)", re.I
    )
    DESC_LINE = re.compile(
        r"^(see\s+the\s+|includes\s+|you\s+may|for\s+hip|for\s+therapies|"
        r"such\s+as|benefits\s+are|travel\s+\(|travel\s+and|special\s+criteria|"
        r"for\s+coverage|for\s+permanent|care\s+during|for\s+member)",
        re.I,
    )
    GENERIC_CAT = re.compile(
        r"^(outpatient\s+care|inpatient\s+care|outpatient\s+services|"
        r"home\s+care|office\s+care)$",
        re.I,
    )
    SERVICE_STOP = re.compile(
        r"\s+(you\s+may|the\s+copay|this\s+plan|see\s+those|"
        r"\*all\s+approved|special\s+criteria|"
        r"for\s+coverage\s+details|virtual\s+pediatric|"
        r"for\s+members\s+\d).*",
        re.I,
    )

    def is_header(item, has_limit_cont, next_is_bullet):
        """Return True if this bullet should be skipped as a sub-section header."""
        first_word = item.split()[0] if item.split() else ""
        return (
            bool(re.search(r"\(See", item, re.I))  # cross-reference
            or (bool(bold_headers) and first_word in bold_headers)  # bold font
            or item.lower().startswith("for ")  # sub-group label
            or (
                has_limit_cont and next_is_bullet and GENERIC_CAT.match(item)
            )  # generic category
        )

    lines_ = cell_text.split("\n")
    benefit = ""
    services = []
    i = 0

    # Step 1: collect benefit name
    while i < len(lines_):
        line = lines_[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("•") or DESC_LINE.match(line):
            break
        benefit = (benefit + " " + line).strip()
        i += 1

    # Step 2: collect services
    while i < len(lines_):
        line = lines_[i].strip()
        i += 1
        if not line:
            continue

        if line.startswith("•"):
            item = re.sub(r"^•\s*", "", line)

            # Collect continuation lines, flag if any are limit text
            has_limit = False
            while i < len(lines_) and not lines_[i].strip().startswith("•"):
                cont = lines_[i].strip()
                peek = lines_[i + 1].strip() if i + 1 < len(lines_) else ""
                if "$" in cont or "$" in peek:
                    break  # next $ line = new service
                if LIMIT_NOTE.match(cont):
                    has_limit = True  # note: don't append to name
                elif cont and not CROSS_REF.match(cont):
                    item += " " + cont
                i += 1

            next_is_bullet = i < len(lines_) and lines_[i].strip().startswith("•")

            if is_header(item, has_limit, next_is_bullet):
                continue  # skip sub-section headers

            item = CROSS_REF.sub("", item).strip()
            item = SERVICE_STOP.sub("", item).strip()
            if item:
                services.append(clean(item))

        elif "$" in line or (i < len(lines_) and "$" in lines_[i]):
            # Non-bullet bold service label, possibly split across two lines
            item = line
            if "$" not in line and "$" in lines_[i]:
                item += " " + lines_[i].strip()
                i += 1
            while i < len(lines_):
                nl = lines_[i].strip()
                if not nl or nl.startswith("•") or CROSS_REF.match(nl):
                    break
                if re.match(
                    r"^(special|travel|benefits|for\s+surgeries|lodging)", nl, re.I
                ):
                    break
                item += " " + nl
                i += 1
            item = SERVICE_STOP.sub("", item).strip()
            if item:
                services.append(clean(item))

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

            # Detect bold bullet headers (sub-section labels) on this page
            bold_headers = get_subsection_headers(page)

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
                    benefit, services = parse_benefit_cell(benefit_cell, bold_headers)

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
                                    "limitations": "Data not found",
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
                                "limitations": "Data not found",
                            },
                        )

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# BUILD ALL — folder-aware pivot
# ═══════════════════════════════════════════════════════

# # """
# # Medical Benefits booklet indexer.
# # Parses the 3-column 'YOUR SHARE OF THE ALLOWED AMOUNT' table using
# # pdfplumber with word-level bold detection to resolve the benefit tree structure.
# # No docling required.
# # """

# # import re, os
# # import json as json_lib
# # import ollama
# # import pdfplumber

# # from datetime import datetime
# # from dotenv import load_dotenv
# # from utils import get_smart_keywords

# # load_dotenv()

# # CURRENT_YEAR_INT = datetime.now().year


# # def classify_document(pdf_path):
# #     """
# #     Reads the first few pages of the PDF and asks the LLM to extract
# #     plan identity: year, type (PPO/HMO), tier, product name, variant, network.
# #     Uses pdfplumber text extraction — no docling needed for this booklet.
# #     """

# #     try:
# #         with pdfplumber.open(pdf_path) as pdf:
# #             # Read up to 3 pages to capture the cover and intro
# #             header_text = ""
# #             for page in pdf.pages[:3]:
# #                 header_text += (page.extract_text() or "") + "\n"
# #                 if len(header_text) > 4000:
# #                     break

# #         prompt = f"""
# #             ACT AS A STRICT STRUCTURED DATA EXTRACTOR.
# #             Extract ONLY if explicitly present in the text.

# #             Rules:
# #             1. year: Extract from "Coverage Period", "Effective Date", or "January 1, YYYY"
# #             2. type: Look for plan type in TWO ways:
# #                 - Explicit label: "Plan Type: <VALUE>"
# #                 - Embedded in plan name: e.g. "Standard PPO Retiree Plan" -> PPO
# #                 Allowed values: HMO, PPO, EPO, HSA
# #             3. tier: Extract from plan title (Gold, Silver, Bronze). Return null if not present.
# #             4. product_line: Full plan name as written.
# #             5. variant: Modifiers like Standard, Retiree, Non-Grandfathered. Else return "Standard".
# #             6. network: Only if explicitly labeled. If not found return null.

# #             RETURN STRICT JSON ONLY. Example:
# #             {{"year": 2025, "type": "PPO", "tier": null,
# #                 "product_line": "Premera Employees Health Plan Standard PPO Retiree Plan",
# #                 "variant": "Retiree", "network": null}}

# #             TEXT:
# #             {header_text[:4000].strip()}
# #             """

# #         response = ollama.generate(
# #             model=os.getenv("OLLAMA_MODEL", "llama3.1"),
# #             prompt=prompt,
# #             format="json",
# #             options={"temperature": 0},
# #         )
# #         data = json_lib.loads(response["response"])
# #         return {
# #             "year": int(re.sub(r"\D", "", str(data.get("year", CURRENT_YEAR_INT)))),
# #             "type": str(data.get("type", "")).strip().upper(),
# #             "tier": str(data.get("tier", "Gold")).strip().capitalize(),
# #             "product_line": str(data.get("product_line", "Plan")).strip(),
# #             "variant": str(data.get("variant", "Standard")).strip(),
# #             "network": str(data.get("network", "Standard Network")).strip(),
# #         }
# #     except Exception as e:
# #         print(f"[!] Medical classification failed: {e}")
# #         return None


# # def clean(text):
# #     """Collapse all whitespace to a single space."""
# #     return re.sub(r"\s+", " ", str(text or "")).strip()


# # def get_subsection_headers(page):
# #     """
# #     Find sub-section header bullets by combining two pdfplumber APIs:
# #         - page.chars    : reliable for finding bullet (•) y-positions
# #         - extract_words : reliable for fontname / bold detection

# #     We get the y-position of every bullet in the benefit column from chars,
# #     then find the first word on that same line from extract_words.
# #     If that word is bold → it is a sub-section header.

# #     This works around two known pdfplumber quirks:
# #         - extract_words does not always tokenize '•' as a separate word
# #         - page.chars has fontname info but requires char-by-char word assembly
# #     """
# #     # Step 1: get y-positions of all bullets in benefit column from chars
# #     bullet_ys = [c["top"] for c in page.chars if c["text"] == "•" and c["x0"] < 200]

# #     if not bullet_ys:
# #         return set()

# #     # Step 2: get all words with font info
# #     words = page.extract_words(extra_attrs=["fontname"])

# #     # Step 3: for each bullet, find the first word on the same line and check bold
# #     starters = set()
# #     for word in words:
# #         if word["x0"] >= 200:
# #             continue  # skip cost/OON columns
# #         for bullet_y in bullet_ys:
# #             if abs(word["top"] - bullet_y) <= 4:
# #                 if "Bold" in word.get("fontname", ""):
# #                     starters.add(word["text"])
# #                 break

# #     return starters


# # def parse_benefit_cell(cell_text, subsection_headers):
# #     """
# #     Split the benefit column cell into a benefit name and list of (service, subsection) tuples.

# #     BENEFIT NAME:
# #         Non-bullet lines at the top form the benefit name. Lines that start with
# #         prose descriptions or notes stop the name collection — they are not part of the name.
# #         e.g. "Cellular Immunotherapy And\nGene Therapy\nYou may have additional costs..."
# #             → benefit = "Cellular Immunotherapy And Gene Therapy"  (stops at "You may")

# #     BULLET CLASSIFICATION — a bullet is a SUB-SECTION HEADER if:
# #         1. Has a "(See ...)" cross-reference
# #         2. Its first word is in subsection_headers (bold font detected via page.chars)
# #         3. Has 2+ continuation lines that contain limit/qualifier text
# #             e.g. "• Outpatient Care\ncalendar year visit limit: 45 visits\nNo limit for..."

# #     LEAF SERVICE:
# #         All other bullets. Description notes are stripped from the service name.
# #     """
# #     CROSS_REF = re.compile(r"\s*\(See.*", re.I)

# #     # Lines that start a description — stop appending to benefit name when seen
# #     BENEFIT_NOTE = re.compile(
# #         r"^(you\s+may|the\s+copay|this\s+plan|see\s+the\s+|covers\s+routine|"
# #         r"includes\s+|for\s+permanent|care\s+during|for\s+transplants|"
# #         r"limited\s+as\s+follows|for\s+member|travel\s+and|interactive\s+audio|"
# #         r"benefits\s+are\s+limited|calendar\s+year|day\s+limit|visit\s+limit|"
# #         r"lifetime\s+limit|for\s+hearing|for\s+washington)",
# #         re.I,
# #     )

# #     # Patterns to strip from the END of service names
# #     SERVICE_NOTE = re.compile(
# #         r"\s+(calendar\s+year|no\s+limit|day\s+limit|visit\s+limit|"
# #         r"you\s+may|the\s+copay|this\s+plan|see\s+those|no\s+charge\s+on|"
# #         r"\*all\s+approved|also\s+covered|limit\s+per|limit\s+\$|"
# #         r"special\s+criteria|for\s+coverage\s+details|"
# #         r"virtual\s+pediatric|for\s+members\s+\d).*",
# #         re.I,
# #     )

# #     # Limit text that indicates a bullet is a sub-section header
# #     LIMIT_NOTE = re.compile(
# #         r"^(calendar\s+year|day\s+limit|visit\s+limit|no\s+limit|"
# #         r"limited\s+to|up\s+to\s+\d)",
# #         re.I,
# #     )

# #     benefit = ""
# #     benefit_done = (
# #         False  # once we hit a note line or bullet, stop adding to benefit name
# #     )
# #     current_subsection = None
# #     services = []
# #     lines_ = cell_text.split("\n")
# #     i = 0

# #     while i < len(lines_):
# #         line = lines_[i].strip()
# #         if not line:
# #             i += 1
# #             continue

# #         if line.startswith("•"):
# #             benefit_done = True
# #             bullet_text = re.sub(r"^•\s*", "", line)
# #             item = bullet_text
# #             continuation_count = 0
# #             item_lines = []
# #             i += 1

# #             while i < len(lines_) and not lines_[i].strip().startswith("•"):
# #                 cont = lines_[i].strip()
# #                 if cont and not CROSS_REF.match(cont):
# #                     item += " " + cont
# #                     continuation_count += 1
# #                     item_lines.append(cont)
# #                 i += 1

# #             had_cross_ref = bool(re.search(r"\(See", item, re.I))
# #             item = CROSS_REF.sub("", item).strip()
# #             first_word = (CROSS_REF.sub("", bullet_text).strip().split() or [""])[0]
# #             has_limit_continuations = any(LIMIT_NOTE.match(l) for l in item_lines)

# #             is_subsection = (
# #                 had_cross_ref
# #                 or first_word in subsection_headers
# #                 or (continuation_count >= 2 and has_limit_continuations)
# #             )

# #             if is_subsection:
# #                 current_subsection = clean(CROSS_REF.sub("", bullet_text).strip())
# #             elif item:
# #                 item = SERVICE_NOTE.sub("", item).strip()
# #                 if item:
# #                     services.append((clean(item), current_subsection))
# #         else:
# #             if not benefit_done and not BENEFIT_NOTE.match(line):
# #                 benefit = (benefit + " " + line).strip()
# #             i += 1

# #     return benefit, services


# # def parse_cost_column(cell_text):
# #     """
# #     Split a cost column cell into a list of individual cost values.

# #     Each new cost value starts with a recognisable token: "$25", "20%",
# #     "No charge", "Deductible", "Kinwell", "All Other", etc.
# #     Lines that do not start a new cost are continuation lines and are
# #     appended to the current cost value.

# #     Kinwell / All Other tier lines are merged into the preceding cost entry
# #     so the member sees the full tiered price as one string.

# #     Example:
# #         Input  → "Kinwell Clinics: $0 copay\ndeductible waived\nAll Other: $25 copay"
# #         Output → ["Kinwell Clinics: $0 copay deductible waived  All Other: $25 copay"]
# #     """
# #     # "deductible" starts a new cost only when it begins with "Deductible, then"
# #     # — bare "deductible waived" is a continuation of the preceding Kinwell line
# #     COST_START = re.compile(
# #         r"^(\$\d|\d+%|no charge|not covered|no cost|deductible,\s*then|kinwell|all other)",
# #         re.I,
# #     )
# #     costs = []
# #     current = ""

# #     for line in cell_text.split("\n"):
# #         line = line.strip()
# #         if not line:
# #             continue
# #         if COST_START.match(line):
# #             if current:
# #                 costs.append(current)
# #             current = line
# #         elif current:
# #             current += " " + line  # continuation of the current cost value
# #     if current:
# #         costs.append(current)

# #     # Merge consecutive Kinwell / All Other tier lines into one cost string.
# #     # Only merge if the preceding entry was ALSO a tier — this prevents
# #     # "Kinwell Clinics: $0..." from being absorbed into "Deductible, then 20%"
# #     # when they belong to different services.
# #     TIER = re.compile(r"^(kinwell|all other)", re.I)
# #     merged = []
# #     for cost in costs:
# #         if merged and TIER.match(cost) and TIER.match(merged[-1]):
# #             merged[-1] += "  " + cost  # same tier group — merge
# #         else:
# #             merged.append(cost)
# #     return merged


# # def generate_sub_index(sub_index_path, pdf_path):
# #     """
# #     Parse the Medical Benefits booklet and write a structured index file.

# #     Each page with a "YOUR SHARE OF THE ALLOWED AMOUNT" table is processed.
# #     The table has three data columns:
# #         Col 0  Benefit name + bullet services (multi-line)
# #         Col 3  In-network costs
# #         Col 6  Out-of-network costs

# #     For each data row we:
# #         1. Detect sub-section headers using bullet indentation (page word positions)
# #         2. Parse col 0 into benefit name + leaf services (skipping headers)
# #         3. Parse col 3 and col 6 into ordered cost lists
# #         4. Pair each service with its cost by index, inheriting when fewer costs than services
# #     """

# #     sub_index = []
# #     seen = set()

# #     def add(topic, content):
# #         key = json_lib.dumps(content, sort_keys=True)
# #         if key not in seen:
# #             seen.add(key)
# #             sub_index.append(
# #                 {
# #                     "topic": topic,
# #                     "category": "cost",
# #                     "benefit_category": "medical",
# #                     "content": content,
# #                     "keywords": get_smart_keywords(content),
# #                 }
# #             )

# #     last_benefit = ""  # persists across pages for page-continuation rows
# #     last_subsection = None  # persists across cells for page-continuation rows

# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             tables = page.extract_tables() or []

# #             # Only process benefit cost pages — identified by the fixed header text
# #             all_text = " ".join(
# #                 clean(str(c or "")) for t in tables for r in t for c in r
# #             ).upper()
# #             page_text = (page.extract_text() or "").upper()
# #             if (
# #                 "YOUR SHARE OF THE ALLOWED AMOUNT" not in all_text
# #                 and "YOUR SHARE OF THE ALLOWED AMOUNT" not in page_text
# #             ):
# #                 continue

# #             # Use character-level data to detect bold bullet sub-section headers
# #             subsection_headers = get_subsection_headers(page)

# #             for table in tables:
# #                 for row_idx, row in enumerate(table):
# #                     if row_idx < 2:  # skip header rows
# #                         continue

# #                     # Col layout: 9-col tables have data at 0, 3, 6; 3-col at 0, 1, 2
# #                     ncols = len(row)
# #                     benefit_cell = str(row[0] or "")
# #                     in_net_cell = str(
# #                         row[3] if ncols > 6 else (row[1] if ncols > 1 else "")
# #                     )
# #                     out_net_cell = str(
# #                         row[6] if ncols > 6 else (row[2] if ncols > 2 else "")
# #                     )

# #                     if not benefit_cell.strip() or (
# #                         not in_net_cell.strip() and not out_net_cell.strip()
# #                     ):
# #                         continue

# #                     benefit, services = parse_benefit_cell(
# #                         benefit_cell, subsection_headers
# #                     )

# #                     # Use last_benefit for page-continuation rows where benefit col is empty
# #                     if not benefit and last_benefit:
# #                         benefit = last_benefit
# #                     if benefit:
# #                         last_benefit = benefit

# #                     # For page-continuation rows, services have no subsection context.
# #                     # Apply last known subsection so they map correctly.
# #                     if not benefit_cell.strip() or not any(
# #                         s
# #                         for s in benefit_cell.split("\n")
# #                         if s.strip() and not s.strip().startswith("•")
# #                     ):
# #                         # This is a continuation row — apply last_subsection to services with no subsection
# #                         services = [
# #                             (svc, sub if sub is not None else last_subsection)
# #                             for svc, sub in services
# #                         ]

# #                     # Update last_subsection from this cell's services
# #                     if services:
# #                         last_sub = next(
# #                             (sub for _, sub in reversed(services) if sub is not None),
# #                             None,
# #                         )
# #                         if last_sub is not None:
# #                             last_subsection = last_sub

# #                     # Single-service benefit: no bullets, benefit itself is the service.
# #                     # Strip trailing notes from benefit name before indexing.
# #                     if not services:
# #                         if benefit:
# #                             # Strip limit/note text that leaked into benefit name
# #                             BENEFIT_TRAIL = re.compile(
# #                                 r"\s+(calendar\s+year|lifetime\s+limit|day\s+limit|"
# #                                 r"visit\s+limit|you\s+may|the\s+copay|see\s+the).*",
# #                                 re.I,
# #                             )
# #                             svc_name = BENEFIT_TRAIL.sub("", benefit).strip()
# #                             in_c = parse_cost_column(in_net_cell)
# #                             out_c = parse_cost_column(out_net_cell)
# #                             add(
# #                                 svc_name,
# #                                 {
# #                                     "event": svc_name,
# #                                     "service": svc_name,
# #                                     "in_network": (
# #                                         in_c[0] if in_c else clean(in_net_cell)
# #                                     ),
# #                                     "out_of_network": (
# #                                         out_c[0] if out_c else clean(out_net_cell)
# #                                     ),
# #                                     "limitations": "Data not found",
# #                                 },
# #                             )
# #                         continue

# #                     in_costs = parse_cost_column(in_net_cell)
# #                     out_costs = parse_cost_column(out_net_cell)

# #                     # Assign costs to services.
# #                     #
# #                     # Most rows are simple (1 cost per service) — sequential works.
# #                     # Complex rows have multiple subsection groups sharing costs, e.g.:
# #                     #
# #                     #   Dental Anesthesia group (3 services) → costs: Deductible, Deductible
# #                     #   Dental Injury group     (1 service)  → costs: Kinwell tiers
# #                     #
# #                     # In this case sequential would wrongly give Kinwell to Anesthesiologist.
# #                     # Rule: if the next cost is a TIERED cost (Kinwell/All Other) and the
# #                     # subsection has NOT changed, that cost belongs to the NEXT subsection —
# #                     # inherit the previous cost instead of advancing.
# #                     TIER_START = re.compile(r"^(kinwell|all other)", re.I)
# #                     cost_idx = 0
# #                     last_in = ""
# #                     last_out = ""
# #                     prev_subsec = None

# #                     for service, subsection in services:
# #                         next_in = (
# #                             in_costs[cost_idx] if cost_idx < len(in_costs) else None
# #                         )
# #                         next_out = (
# #                             out_costs[cost_idx] if cost_idx < len(out_costs) else None
# #                         )

# #                         # Advance the cost pointer when:
# #                         # 1. The subsection changed (always take the next cost for a new group)
# #                         # 2. The next cost is the same "type" as current (not a tier shift)
# #                         subsection_changed = subsection != prev_subsec
# #                         next_is_tier_shift = (
# #                             next_in
# #                             and TIER_START.match(next_in)
# #                             and last_in
# #                             and not TIER_START.match(last_in)
# #                         )
# #                         should_advance = subsection_changed or not next_is_tier_shift

# #                         if should_advance and next_in is not None:
# #                             last_in = next_in
# #                             last_out = next_out or last_out
# #                             cost_idx += 1

# #                         prev_subsec = subsection
# #                         event = subsection if subsection else benefit
# #                         add(
# #                             f"{benefit} — {service}",
# #                             {
# #                                 "event": event,
# #                                 "service": service,
# #                                 "in_network": last_in,
# #                                 "out_of_network": last_out,
# #                                 "limitations": "Data not found",
# #                             },
# #                         )

# #     with open(sub_index_path, "w", encoding="utf-8") as f:
# #         json_lib.dump(sub_index, f, indent=4)

# #     return sub_index


# # # BUILD ALL — folder-aware pivot
# # # ═══════════════════════════════════════════════════════


# import os
# import sqlite3
# import json as json_lib
# import re
# import ollama
# import pdfplumber

# from datetime import datetime
# from dotenv import load_dotenv

# load_dotenv()

# DOC_BASE_DIR = os.path.abspath("./docs")
# INDEX_OUTPUT_DIR = "./indices"
# DB_PATH = os.path.join(os.path.dirname(__file__), "p_insurance_index.db")
# LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
# CURRENT_YEAR_INT = datetime.now().year


# def setup_db():
#     conn = sqlite3.connect(DB_PATH)
#     cursor = conn.cursor()

#     # 1. Master Index for Fast Routing with unique Plan Identity
#     cursor.execute(
#         """
#         CREATE TABLE IF NOT EXISTS master_index (
#             id INTEGER PRIMARY KEY AUTOINCREMENT,
#             year INTEGER,
#             plan_category TEXT,     -- SBC, MEDICAL, DENTAL, VISION
#             plan_type TEXT,    -- PPO, HMO, HSA
#             plan_tier TEXT,    -- Gold, Silver, Bronze
#             product_line TEXT, -- e.g., 'EPO HSA Preferred', 'Cascade Care'
#             variant TEXT,      -- e.g., 'American Indian 300%', 'CSR 94%', 'Standard'
#             network TEXT,      -- e.g., 'Individual Signature', 'Sherwood HMO'
#             pdf_path TEXT UNIQUE,
#             sub_index_path TEXT
#         )
#     """
#     )

#     # Performance: This 5-way Composite Index is the "Identity Lock"
#     # It ensures lookups for specific variants are instant and deterministic.
#     cursor.execute(
#         """
#         CREATE INDEX IF NOT EXISTS idx_plan_identity
#         ON master_index (year, plan_category, plan_type, plan_tier, product_line, variant, network)
#     """
#     )

#     # 2. FTS5 Virtual Table for Fuzzy Search
#     # We include product_line and variant so users can search for "94%" or "HSA"
#     cursor.execute(
#         """
#         CREATE VIRTUAL TABLE IF NOT EXISTS search_index
#         USING fts5(
#             year,
#             tier,
#             type,
#             product_line,
#             variant,
#             network,
#             topic,
#             benefit_category,
#             content,
#             keywords,
#             tokenize='porter'
#         )
#     """
#     )

#     conn.commit()
#     conn.close()
#     print("[*] Database Schema Updated: Ready for Premera multi-plan indexing.")


# def get_smart_keywords(text):
#     # 🔥 FIX: normalize input to string
#     if isinstance(text, dict):
#         text = json_lib.dumps(text)

#     text_lower = text.lower()
#     patterns = {
#         "pcp": r"\bpcp\b|primary[- ]?care",
#         "specialist": r"specialist",
#         "in-network": r"in[- ]?network",
#         "out-of-network": r"out[- ]?of[- ]?network",
#         "copay": r"co[- ]?pay|copay",
#         "deductible": r"deductible",
#         "coinsurance": r"co[- ]?insurance",
#         "emergency": r"emergency|medical[- ]?attention",
#         "urgent-care": r"urgent[- ]?care",
#         "pharmacy": r"pharmacy|prescription|rx",
#         "dental": r"dental|dentist|ortho|braces",
#         "vision": r"vision|eye|glasses",
#         "imaging": r"imaging|mri|ct\s?scan|pet\s?scan",
#         "diagnostic": r"diagnostic|x-ray|blood\s?work",
#         "mental-health": r"mental|behavioral|substance|abuse",
#         "therapy": r"rehab|physical|speech|occupational",
#     }
#     found = [
#         label for label, pattern in patterns.items() if re.search(pattern, text_lower)
#     ]
#     if len(found) < 10:
#         backups = re.findall(r"\b\w{7,}\b", text_lower)
#         for w in backups:
#             if w not in found and len(found) < 10:
#                 found.append(w)

#     print(f"[*] RETURNING SMART KEYWORDS: {found[:10]}")
#     return found[:10]


# def classify_document(pdf_path):
#     """
#     Extract plan identity from first 2 pages of PDF using pdfplumber + LLM.
#     Replaces the docling-based version since we no longer use docling here.
#     """
#     try:
#         with pdfplumber.open(pdf_path) as pdf:
#             header_text = ""
#             for page in pdf.pages[:3]:
#                 header_text += (page.extract_text() or "") + "\n"
#                 if len(header_text) > 4000:
#                     break
#         header_snippet = header_text[:4000].strip()

#         prompt = f"""
#             ACT AS A STRICT STRUCTURED DATA EXTRACTOR.

#             Extract ONLY if explicitly present in the text.

#             Rules:
#             1. year: Extract from "Coverage Period" or "Effective Date" or "January 1, YYYY"

#             2. type: Extract plan type. Look for it in TWO ways:
#                 - Explicit label: "Plan Type: <VALUE>"
#                 - Embedded in plan name: e.g. "Standard PPO Retiree Plan" → PPO,
#                     "HMO Gold Plan" → HMO, "EPO HSA Preferred" → EPO
#                 Allowed values: HMO, PPO, EPO, HSA
#                 If found multiple (e.g. "PPO HSA"), prefer the first.

#             3. tier: Extract from plan title (Gold, Silver, Bronze, Catastrophic)
#                 If not present return null.

#             4. product_line: Full plan name as written (e.g. "Premera Employees Health Plan
#                 Standard PPO Retiree Plan"). Remove year references.

#             5. variant: Extract modifiers like Standard, Retiree, Non-Grandfathered, CSR, etc.
#                 Else return "Standard"

#             6. network: ONLY extract if explicitly labeled (e.g. "Network: Sherwood").
#                 DO NOT infer. If not found return null.

#             RETURN STRICT JSON ONLY. Example:
#             {{"year": 2025, "type": "PPO", "tier": null, "product_line": "Premera Employees Health Plan Standard PPO Retiree Plan", "variant": "Retiree", "network": null}}

#             TEXT:
#             {header_snippet}
#             """

#         response = ollama.generate(
#             model=LOCAL_MODEL,
#             prompt=prompt,
#             format="json",
#             options={"temperature": 0},
#         )

#         data = json_lib.loads(response["response"])
#         return {
#             "year": int(re.sub(r"\D", "", str(data.get("year", CURRENT_YEAR_INT)))),
#             "type": str(data.get("type", "")).strip().upper(),
#             "tier": str(data.get("tier", "Gold")).strip().capitalize(),
#             "product_line": str(data.get("product_line", "Plan")).strip(),
#             "variant": str(data.get("variant", "Standard")).strip(),
#             "network": str(data.get("network", "Standard Network")).strip(),
#         }
#     except Exception as e:
#         print(f"[!] Classification failed: {e}")
#         return None


# # ── helpers used by generate_sub_index ───────────────────────────────────────


# def find_bold_bullet_starters(page_words):
#     """
#     Scan all words in the benefit column (x < 200) and return a set of
#     first-words that immediately follow a bold bullet (•).

#     Purpose: in this PDF, bold bullets mark sub-section headers
#     (e.g. '• Dental Anesthesia') while regular bullets mark actual
#     leaf services (e.g. '• Inpatient facility care').  By collecting
#     the first word of every bold-bullet line, we can later classify
#     any bullet item as a sub-section vs a leaf service.
#     """
#     bold_starters = set()
#     prev_bullet = None

#     for word in sorted(page_words, key=lambda w: (w["top"], w["x0"])):
#         if word["x0"] >= 200:  # only look at the benefit column
#             continue
#         if word["text"] in ("•", "\u2022"):
#             prev_bullet = word  # remember this bullet position
#         elif prev_bullet and abs(word["top"] - prev_bullet["top"]) < 6:
#             # This word is on the same line as the preceding bullet
#             fontname = word.get("fontname", "")
#             if "Bold" in fontname or "bold" in fontname:
#                 bold_starters.add(word["text"])
#             prev_bullet = None
#         else:
#             prev_bullet = None  # bullet was on a different line — reset

#     return bold_starters


# def parse_benefit_cell(cell_text, bold_starters):
#     """
#     Parse the raw text of a benefit cell (column 0) into:
#         - benefit_name : top-level benefit name  (e.g. 'Dental Injury and Facility Anesthesia')
#         - services     : ordered list of (service_name, subsection_name) tuples

#     How it works:
#         - Lines without a bullet are joined to form the benefit name.
#         - Bullet items whose first word is in bold_starters are sub-section
#         headers (branch nodes in the tree) — they set the current subsection.
#         - All other bullet items are leaf services that get indexed.
#         - Wrapped continuation lines (no bullet) are appended to the current item.
#         - Cross-reference text like '(See Dental Injury...)' is stripped.
#     """
#     CROSS_REF = re.compile(
#         r"\s*\(See.*|^(benefit for|injury and facility anesthesia benefit)", re.I
#     )

#     benefit = ""
#     subsection = None
#     services = []

#     lines = cell_text.split("\n")
#     i = 0
#     while i < len(lines):
#         line = lines[i].strip()
#         if not line:
#             i += 1
#             continue

#         if line.startswith("•"):
#             # Start of a bullet item — collect its full text across wrapped lines
#             item = re.sub(r"^•\s*", "", line)
#             i += 1
#             while i < len(lines) and not lines[i].strip().startswith("•"):
#                 continuation = lines[i].strip()
#                 if not CROSS_REF.match(continuation):
#                     item += " " + continuation
#                 i += 1
#             # Strip any trailing cross-reference suffix e.g. "(See X benefit...)"
#             item = re.sub(r"\s*\(See.*", "", item, flags=re.I).strip()

#             if item.split()[0] in bold_starters:
#                 subsection = item  # bold bullet → sub-section header
#             else:
#                 services.append((n(item), subsection))  # regular bullet → leaf service
#         else:
#             # Non-bullet line → part of the top-level benefit name (may wrap)
#             benefit = (benefit + " " + line).strip()
#             i += 1

#     return benefit, services


# def parse_cost_column(cell_text):
#     """
#     Split the raw text of a cost column cell into a list of individual
#     cost value strings.

#     Each cost value starts with a recognisable pattern ($, %, 'No charge',
#     'Deductible', etc.).  Continuation lines (wrapping) are appended to the
#     current cost value.

#     Special rule: Kinwell Clinics / All Other tier sub-headers are merged
#     into the PRECEDING cost entry because they are pricing tiers for the
#     same service, not separate services.

#     Example input:
#         'Kinwell Clinics: $0 copay\ndeductible waived\nAll Other: $25 copay\n...'
#     Example output:
#         ['Kinwell Clinics: $0 copay deductible waived All Other: $25 copay ...']
#     """
#     COST_LINE_START = re.compile(
#         r"^(\$\d|\d+%|no charge|not covered|no cost|deductible|kinwell|all other)", re.I
#     )
#     TIER_MARKER = re.compile(r"^(kinwell|all other)", re.I)

#     # First pass: split into raw cost strings on line-start patterns
#     raw_costs = []
#     current = ""
#     for line in cell_text.split("\n"):
#         line = line.strip()
#         if COST_LINE_START.match(line):
#             if current:
#                 raw_costs.append(current)
#             current = line
#         elif current:
#             current += " " + line  # wrapped continuation of the current cost
#     if current:
#         raw_costs.append(current)

#     # Second pass: merge Kinwell / All Other tier lines into the previous entry
#     # so a single service that has multiple pricing tiers is one cost string
#     merged = []
#     for cost in raw_costs:
#         if merged and TIER_MARKER.match(cost):
#             merged[-1] += "  " + cost  # append tier to previous cost
#         else:
#             merged.append(cost)

#     return merged


# def map_costs_to_services(service_positions, cost_positions):
#     """
#     Assign each cost value to the leaf service it belongs to, using
#     vertical (y-axis) proximity on the page.

#     Why y-coordinates?  In a complex benefit like 'Dental Anesthesia'
#     three leaf services (Inpatient, Outpatient, Anesthesiologist) share
#     two cost lines in the PDF.  Sequential assignment mis-assigns the
#     third service.  Y-proximity correctly maps each cost to the service
#     on the same visual row.

#     Args:
#         service_positions : list of y-coordinates for each leaf service
#         cost_positions    : list of (y, cost_text) tuples from the cost column

#     Returns:
#         dict {service_y: [cost_text, ...]}
#         Services with no direct cost get an empty list — they will
#         inherit the previous service's cost during the emit loop.
#     """
#     assignment = {y: [] for y in service_positions}
#     if not service_positions or not cost_positions:
#         return assignment

#     for cost_y, cost_text in cost_positions:
#         # Find the service whose vertical position is closest to this cost line
#         nearest_service_y = min(service_positions, key=lambda sy: abs(sy - cost_y))
#         assignment[nearest_service_y].append(cost_text)

#     return assignment


# def get_leaf_service_positions(page_words):
#     """
#     Return the vertical (y) positions of every regular (non-bold) bullet
#     item in the benefit column (x < 200), in top-to-bottom reading order.

#     These y-positions are used to match cost lines to the correct leaf
#     service when the cost and service appear on the same visual row.

#     Bold bullets are sub-section headers and are intentionally excluded.
#     """
#     positions = []
#     prev_bullet = None

#     for word in sorted(page_words, key=lambda w: (w["top"], w["x0"])):
#         if word["x0"] >= 200:
#             continue
#         if word["text"] in ("•", "\u2022"):
#             prev_bullet = word
#         elif prev_bullet and abs(word["top"] - prev_bullet["top"]) < 6:
#             fontname = word.get("fontname", "")
#             if "Bold" not in fontname and "bold" not in fontname:
#                 # Regular bullet → this is a leaf service row
#                 positions.append(prev_bullet["top"])
#             prev_bullet = None
#         else:
#             prev_bullet = None

#     return positions


# def get_cost_line_positions(page_words, col_x_start, col_x_end):
#     """
#     Extract (y_position, cost_text) pairs for every cost value line
#     that falls within the given horizontal column range on the page.

#     Words are first grouped by their vertical band (y ± 4px) to
#     reconstruct full text lines, then each line that starts with a
#     recognised cost pattern is recorded with its y-position.
#     Continuation lines (e.g. line-wrapped dollar amounts) are appended
#     to the preceding cost entry.

#     Args:
#         page_words  : word list from pdfplumber extract_words()
#         col_x_start : left x-boundary of the column
#         col_x_end   : right x-boundary of the column
#     """
#     COST_LINE_START = re.compile(
#         r"^(\$\d|\d+%|no charge|not covered|no cost|deductible|kinwell|all other)", re.I
#     )
#     # Group words into y-bands to reconstruct text lines
#     bands = {}
#     for word in page_words:
#         if col_x_start <= word["x0"] < col_x_end:
#             band_key = round(word["top"] / 4) * 4
#             bands.setdefault(band_key, []).append(word)

#     cost_lines = []
#     for band_key in sorted(bands):
#         band_words = sorted(bands[band_key], key=lambda w: w["x0"])
#         line_text = n(" ".join(w["text"] for w in band_words))
#         line_y = sum(w["top"] for w in band_words) / len(band_words)

#         if COST_LINE_START.match(line_text):
#             cost_lines.append((line_y, line_text))
#         elif cost_lines:
#             # Wrapped continuation — append to the previous cost line
#             cost_lines[-1] = (cost_lines[-1][0], cost_lines[-1][1] + " " + line_text)

#     return cost_lines


# def n(v):
#     """Normalise whitespace in any value to a single space."""
#     return re.sub(r"\s+", " ", str(v or "")).strip()


# # ── Main indexer ──────────────────────────────────────────────────────────────


# def generate_sub_index(md_content, sub_index_path, pdf_path=None):
#     """
#     Index a Medical Benefits booklet PDF into structured cost entries.

#     Uses pdfplumber (no docling) because this booklet has a consistent
#     3-column table layout:  Benefit | In-Network | Out-of-Network.

#     The benefit column (col 0) has a TREE structure:
#         Root benefit name  (bold, no bullet)
#         └─ Sub-section   (bold bullet)
#             └─ Service (regular bullet)  ← what gets indexed

#     Cost columns use bold sub-headers like 'Kinwell Clinics:' /
#     'All Other Non-Specialist:' within a single cell to show tiered
#     pricing for one service.  These tiers are merged into one string.

#     Because some services share a cost line in the PDF (e.g. 'Outpatient
#     surgery center' and 'Anesthesiologist' both map to the same cost row),
#     costs are matched to services by vertical position (y-coordinate)
#     rather than sequential counting.  Services with no direct cost match
#     inherit the previous service's cost.

#     Output schema matches the SBC indexer:
#         event, service, in_network, out_of_network, limitations
#     """

#     sub_index = []
#     seen = set()

#     def add(topic, content):
#         key = json_lib.dumps(content, sort_keys=True)
#         if key not in seen:
#             seen.add(key)
#             sub_index.append(
#                 {
#                     "topic": topic,
#                     "category": "cost",
#                     "benefit_category": "medical",
#                     "content": content,
#                     "keywords": get_smart_keywords(content),
#                 }
#             )

#     if not pdf_path:
#         with open(sub_index_path, "w", encoding="utf-8") as f:
#             json_lib.dump(sub_index, f, indent=4)
#         return sub_index

#     # Column x-boundaries (consistent across all benefit pages in this PDF):
#     #   Col 0  (benefit names)  : x  <  200
#     #   Col 3  (in-network)     : 200 <= x < 375
#     #   Col 6  (out-of-network) : 375 <= x
#     IN_NET_X_START = 200
#     IN_NET_X_END = 375
#     OUT_NET_X_START = 375
#     OUT_NET_X_END = 600

#     with pdfplumber.open(pdf_path) as pdf:
#         for page in pdf.pages:
#             tables = page.extract_tables() or []

#             # Skip pages that do not contain a benefit cost table
#             flat_header = " ".join(
#                 n(str(c or "")) for t in tables for r in t[:3] for c in r
#             ).upper()
#             if "YOUR SHARE OF THE ALLOWED AMOUNT" not in flat_header:
#                 continue

#             # Word-level data is needed to detect bold bullets and match
#             # costs to services by vertical position
#             page_words = page.extract_words(extra_attrs=["fontname"])
#             bold_starters = find_bold_bullet_starters(page_words)

#             # Get vertical positions of every cost line in each column
#             # (done once per page, shared across all rows on that page)
#             in_net_positions = get_cost_line_positions(
#                 page_words, IN_NET_X_START, IN_NET_X_END
#             )
#             out_net_positions = get_cost_line_positions(
#                 page_words, OUT_NET_X_START, OUT_NET_X_END
#             )

#             for table in tables:
#                 for row_idx, row in enumerate(table):
#                     if row_idx < 2:  # rows 0-1 are column headers — skip
#                         continue

#                     # Handle both 9-column (merged header) and 3-column layouts
#                     ncols = len(row)
#                     benefit_cell = str(row[0] or "")
#                     in_net_cell = str(
#                         row[3] if ncols > 6 else (row[1] if ncols > 1 else "")
#                     )
#                     out_net_cell = str(
#                         row[6] if ncols > 6 else (row[2] if ncols > 2 else "")
#                     )

#                     if not benefit_cell.strip():
#                         continue
#                     if not in_net_cell.strip() and not out_net_cell.strip():
#                         continue

#                     benefit, services = parse_benefit_cell(benefit_cell, bold_starters)
#                     if not benefit or not services:
#                         continue

#                     # Get vertical positions of the leaf services in this row
#                     # so we can align them with the correct cost lines
#                     service_positions = get_leaf_service_positions(page_words)

#                     in_net_map = map_costs_to_services(
#                         service_positions, in_net_positions
#                     )
#                     out_net_map = map_costs_to_services(
#                         service_positions, out_net_positions
#                     )

#                     # Emit one index entry per leaf service.
#                     # If a service has no direct cost assignment (it shares a
#                     # cost row with the service above it), inherit the last seen cost.
#                     last_in_net = ""
#                     last_out_net = ""
#                     for (service_name, subsection), svc_y in zip(
#                         services, service_positions
#                     ):
#                         assigned_in = " ".join(in_net_map.get(svc_y, []))
#                         assigned_out = " ".join(out_net_map.get(svc_y, []))

#                         if assigned_in:
#                             last_in_net = assigned_in
#                         if assigned_out:
#                             last_out_net = assigned_out

#                         event = f"{benefit} — {subsection}" if subsection else benefit
#                         topic = (
#                             f"{event} — {service_name}"
#                             if service_name != benefit
#                             else event
#                         )

#                         add(
#                             topic,
#                             {
#                                 "event": event,
#                                 "service": service_name,
#                                 "in_network": n(last_in_net),
#                                 "out_of_network": n(last_out_net),
#                                 "limitations": "",
#                             },
#                         )

#     with open(sub_index_path, "w", encoding="utf-8") as f:
#         json_lib.dump(sub_index, f, indent=4)

#     return sub_index


# def build_all():
#     setup_db()
#     os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)

#     conn = sqlite3.connect(DB_PATH)
#     doc_path = os.path.abspath(DOC_BASE_DIR)
#     print(f"[*] Absolute Doc Path: {doc_path}")

#     for root, _, files in os.walk(doc_path):
#         parts = os.path.normpath(root).lower().split(os.sep)
#         # # 🚫 Skip separators / markdown junk
#         # if all(re.match(r"^-+$", p.replace(" ", "")) for p in parts):
#         #     continue

#         # # 🚫 Skip image placeholders
#         # if any("<!-- image" in p.lower() for p in parts):
#         #     continue

#         # # 🚫 Skip headers
#         # if any(
#         #     h in parts[0].lower()
#         #     for h in [
#         #         "what you will pay",
#         #         "common medical event",
#         #         "services you may need",
#         #     ]
#         # ):
#         #     continue
#         path_year = next((int(p) for p in parts if p.isdigit()), None)
#         final_plan_category = final_plan_category = os.path.basename(
#             root
#         ).lower()  # This takes the folders name which tells the booklet for plan category. SBC, Medical etc...

#         for filename in files:
#             if not filename.lower().endswith(".pdf"):
#                 continue
#             pdf_path = os.path.abspath(os.path.join(root, filename))

#             try:
#                 print(f"[*] Processing: {filename}...")

#                 # --- STEP 1: CLASSIFY FROM PDF DIRECTLY ---
#                 plan_info = classify_document(pdf_path)
#                 print(f"[*] LLM Classification for {filename}: {plan_info}")
#                 # --- STEP 2: HARDENED IDENTITY FALLBACKS ---

#                 # Folder path overrides LLM guess for Year and Type
#                 final_year = path_year or (
#                     plan_info["year"]
#                     if plan_info and plan_info.get("year")
#                     else CURRENT_YEAR_INT
#                 )

#                 # -----------------------------
#                 # TYPE LOGIC (FINAL FIX)
#                 # -----------------------------
#                 llm_type = (
#                     str(plan_info.get("type", "")).upper()
#                     if plan_info and plan_info.get("type")
#                     else ""
#                 )

#                 # Normalize LLM types
#                 VALID_TYPES = ["HMO", "PPO", "EPO", "HSA"]

#                 if llm_type not in VALID_TYPES:
#                     final_type = ""
#                 else:
#                     final_type = llm_type

#                 # -------------------------
#                 # Tier
#                 # -------------------------
#                 final_tier = (
#                     plan_info["tier"] if plan_info and plan_info.get("tier") else "Gold"
#                 ).capitalize()
#                 print(f"[*] Tier Locked: {final_tier}")

#                 if final_tier.lower() == "none":
#                     row_tier = ""
#                 else:
#                     row_tier = final_tier
#                 # -------------------------
#                 # Product Line
#                 # -------------------------
#                 raw_prod = plan_info.get("product_line", "") if plan_info else ""

#                 if not raw_prod or str(raw_prod).lower() in [
#                     "plan",
#                     "standard",
#                     "none",
#                     "",
#                 ]:
#                     final_product = (
#                         filename.replace(".pdf", "").replace("_", " ").title()
#                     )
#                 else:
#                     final_product = str(raw_prod).strip()

#                 # -------------------------
#                 # Variant (DB SAFE)
#                 # -------------------------
#                 raw_vari = plan_info.get("variant", "") if plan_info else ""

#                 if not raw_vari or str(raw_vari).lower() in ["none", "standard", ""]:
#                     final_variant = "Standard"
#                 else:
#                     final_variant = str(raw_vari).strip()

#                 # -------------------------
#                 # Network (DB SAFE — NO HARDCODE)
#                 # -------------------------
#                 raw_net = plan_info.get("network") if plan_info else None

#                 if raw_net and str(raw_net).strip().lower() not in ["network", "none"]:
#                     final_network = str(raw_net).strip()
#                 else:
#                     final_network = "Unknown Network"

#                 print(
#                     f"[*] Identity Locked: {final_year} | {final_product} | {final_network}"
#                 )

#                 # =====================================================
#                 # 🧼 STEP 3: CLEAN FILENAME (NO NOISE VALUES)
#                 # =====================================================

#                 INVALID_NETWORK_VALUES = {
#                     "unknown network",
#                     "standard network",
#                     "network",
#                     "",
#                 }

#                 INVALID_VARIANT_VALUES = {
#                     "standard",
#                     "none",
#                     "",
#                 }

#                 # Clean product
#                 safe_prod = re.sub(r"\W+", "_", final_product.lower()).strip("_")

#                 # Clean network (ONLY if real)
#                 raw_net_clean = str(final_network).strip().lower()
#                 if raw_net_clean not in INVALID_NETWORK_VALUES:
#                     safe_net = re.sub(r"\W+", "_", raw_net_clean).strip("_")
#                 else:
#                     safe_net = None

#                 # Clean variant (ONLY if meaningful)
#                 raw_var_clean = str(final_variant).strip().lower()
#                 if raw_var_clean not in INVALID_VARIANT_VALUES:
#                     safe_var = re.sub(r"\W+", "_", raw_var_clean).strip("_")
#                 else:
#                     safe_var = None

#                 # -------------------------
#                 # Build filename dynamically
#                 # -------------------------
#                 filepath = [
#                     str(final_year),
#                     final_plan_category,
#                     final_type,
#                     row_tier,
#                     safe_prod,
#                 ]

#                 if safe_var:
#                     filepath.append(safe_var)

#                 if safe_net:
#                     filepath.append(safe_net)

#                 unique_fn = "_".join(filepath) + ".json"

#                 sub_index_path = os.path.abspath(
#                     os.path.join(INDEX_OUTPUT_DIR, unique_fn)
#                 )

#                 # --- STEP 4: GENERATE INDEX ---
#                 sub_chunks = generate_sub_index(None, sub_index_path, pdf_path)

#                 # --- STEP 4: DB INSERTS (Surgical Siloing) ---
#                 conn.execute(
#                     """
#                     DELETE FROM search_index
#                     WHERE year = ? AND tier = ? AND type = ?
#                     AND product_line = ? AND variant = ? AND network = ?
#                 """,
#                     (
#                         final_year,
#                         final_tier,
#                         final_type,
#                         final_product,
#                         final_variant,
#                         final_network,
#                     ),
#                 )

#                 for chunk in sub_chunks:
#                     raw_content = chunk.get("content", "")

#                     if isinstance(raw_content, dict):
#                         clean_content = json_lib.dumps(raw_content)
#                     else:
#                         clean_content = str(raw_content)

#                     # ✅ SAFE KEYWORDS HANDLING (FIXED)
#                     keywords_str = (
#                         " ".join(chunk.get("keywords", []))
#                         if chunk.get("keywords")
#                         else ""
#                     )

#                     conn.execute(
#                         """
#                         INSERT INTO search_index
#                         (year, tier, type, product_line, variant, network, topic, benefit_category, content, keywords)
#                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,?)
#                     """,
#                         (
#                             final_year,
#                             final_tier,
#                             final_type,
#                             final_product,
#                             final_variant,
#                             final_network,
#                             chunk.get("topic", ""),  # ✅ extra safety
#                             chunk.get("benefit_category", ""),
#                             clean_content,
#                             keywords_str,
#                         ),
#                     )

#                 conn.execute(
#                     """
#                     INSERT OR REPLACE INTO master_index
#                     (year, plan_category, plan_type, plan_tier, product_line, variant, network, pdf_path, sub_index_path)
#                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
#                 """,
#                     (
#                         final_year,
#                         final_plan_category,
#                         final_type,
#                         final_tier,
#                         final_product,
#                         final_variant,
#                         final_network,
#                         pdf_path,
#                         sub_index_path,
#                     ),
#                 )

#                 conn.commit()
#                 print(f"✅ SUCCESS: {filename} -> {unique_fn}")

#             except Exception as e:
#                 print(f"❌ FAILED {filename}: {e}")

#     conn.close()


# if __name__ == "__main__":
#     build_all()
