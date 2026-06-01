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
from utility.utils import get_smart_keywords

load_dotenv()

CURRENT_YEAR_INT = datetime.now().year


def classify_document(pdf_path):
    """
    Read the first few pages of the Medical booklet PDF and extract:
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
        print(f"[!] Medical classification failed: {e}")
        return None


def clean(text):
    """Collapse all whitespace in a string to a single space."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_benefit_cell(cell_text):
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
    # --- Patterns ---
    CROSS_REF = re.compile(r"\s*\(See.*", re.I)
    LIMIT_NOTE = re.compile(
        r"^(calendar\s+year|day\s+limit|visit\s+limit|no\s+limit|"
        r"limited\s+to|limit\s+each|limit\s+\$|\(copay|\(your)",
        re.I,
    )
    DESC_LINE = re.compile(
        r"^(see\s+the\s+|includes\s+|you\s+may|for\s+hip|for\s+therapies|"
        r"for\s+hearing|covers\s+routine|such\s+as|benefits\s+are|"
        r"travel\s+\(|travel\s+and|special\s+criteria|"
        r"for\s+coverage|for\s+permanent|care\s+during|"
        r"for\s+member)",
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
    # Non-bullet $ lines that are limit notes, not service labels
    LIMIT_DOLLAR = re.compile(r"^limit\s+\$", re.I)

    def is_header(item, has_limit_cont, next_is_bullet):
        """Return True if this bullet should be skipped as a sub-section header."""
        return (
            bool(re.search(r"\(See", item, re.I))  # cross-reference
            or item.lower().startswith("for ")  # sub-group label
            or (
                has_limit_cont and next_is_bullet and GENERIC_CAT.match(item)
            )  # generic category
        )

    lines_ = cell_text.split("\n")
    benefit = ""
    notes = []  # "You may have..." type notes — important for member context
    services = []
    i = 0

    # Step 1: collect benefit name — stop at bullets or description lines
    while i < len(lines_):
        line = lines_[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("•") or DESC_LINE.match(line):
            break
        benefit = (benefit + " " + line).strip()
        i += 1

    # Step 1b: collect note lines that follow the benefit name (before bullets)
    # e.g. "Covers routine patient care during the trial"
    #      "You may have additional costs for other services..."
    # These are important coverage context — captured for the limitations field.
    NOTE_LINE = re.compile(
        r"^(you\s+may|covers\s+routine|for\s+permanent|care\s+during|"
        r"benefits\s+are\s+limited|limited\s+as\s+follows|"
        r"for\s+member|includes\s+travel|travel\s+and\s+lodging|"
        r"prior\s+approval|special\s+criteria)",
        re.I,
    )
    note_parts = []
    while i < len(lines_):
        line = lines_[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("•"):
            break
        # Stop if this line or the next has "$" — it's a service label, not a note
        peek = lines_[i + 1].strip() if i + 1 < len(lines_) else ""
        if "$" in line or "$" in peek:
            break
        if NOTE_LINE.match(line) or note_parts:
            note_parts.append(line)
        i += 1
    if note_parts:
        notes.append(clean(" ".join(note_parts)))

    # Step 2: collect services
    subgroup_context = ""  # text of the most recent skipped "• For X" sub-group bullet
    # e.g. "For total hip and knee joint replacements..."
    # attached to subsequent leaf services so they're searchable

    while i < len(lines_):
        line = lines_[i].strip()
        i += 1
        if not line:
            continue

        if line.startswith("•"):
            item = re.sub(r"^•\s*", "", line)

            # Collect continuation lines — split into name parts and note parts.
            # Lines starting with "You may..." or "The copay..." are notes for
            # THIS service specifically, not part of the service name.
            has_limit = False
            past_limit_note = (
                False  # once limit text starts, skip its continuations too
            )
            svc_note = []
            collecting_note = False
            SVC_NOTE = re.compile(r"^(you\s+may|the\s+copay|examples\s+are)", re.I)

            while i < len(lines_) and not lines_[i].strip().startswith("•"):
                cont = lines_[i].strip()
                peek = lines_[i + 1].strip() if i + 1 < len(lines_) else ""
                if "$" in cont or "$" in peek:
                    break
                if re.match(r"^(no\s+charge\s+on|please\s+call)", cont, re.I):
                    break
                # A short Title-Case line with no punctuation/symbols is likely a
                # bold sub-section header (e.g. "PV Core Preventive Drugs",
                # "Exceptions", "Covered Drugs") — stop absorbing into service name
                if (
                    cont
                    and len(cont.split()) <= 6
                    and cont[0].isupper()
                    and not any(c in cont for c in ",.;:$%()")
                    and not LIMIT_NOTE.match(cont)
                    and not SVC_NOTE.match(cont)
                ):
                    peek_next = next(
                        (
                            lines_[j].strip()
                            for j in range(i + 1, len(lines_))
                            if lines_[j].strip()
                        ),
                        "",
                    )
                    if not peek_next.startswith("•"):
                        break
                if LIMIT_NOTE.match(cont):
                    has_limit = True
                    past_limit_note = True  # flag: don't append subsequent lines either
                elif past_limit_note:
                    pass  # continuation of limit note — discard
                elif SVC_NOTE.match(cont) or collecting_note:
                    collecting_note = True
                    if cont and not CROSS_REF.match(cont):
                        svc_note.append(cont)
                elif cont and not CROSS_REF.match(cont):
                    item += " " + cont
                i += 1

            next_is_bullet = i < len(lines_) and lines_[i].strip().startswith("•")

            if is_header(item, has_limit, next_is_bullet):
                # Sub-group bullet skipped — but if it starts with "For ",
                # save its full text as context for the leaf services under it.
                # e.g. "For total hip and knee joint replacements..."
                # This makes the leaf services discoverable via that search term.
                if item.lower().startswith("for "):
                    subgroup_context = clean(CROSS_REF.sub("", item).strip())
                else:
                    subgroup_context = ""  # other sub-group types reset context
                continue

            item = CROSS_REF.sub("", item).strip()
            item = SERVICE_STOP.sub("", item).strip()
            if item:
                # Build limitation: service-specific note takes priority,
                # then sub-group context (e.g. "For total hip..."),
                # then empty (caller will apply "Data Not Found")
                svc_limitation = clean(" ".join(svc_note))
                services.append((clean(item), svc_limitation, subgroup_context))
                # Don't reset subgroup_context here — it persists for all siblings
                # under the same sub-group until a new sub-group or dollar-line resets it

        elif (
            "$" in line or (i < len(lines_) and "$" in lines_[i])
        ) and not LIMIT_DOLLAR.match(line):
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
                services.append((clean(item), "", ""))
                subgroup_context = ""  # dollar-line service resets sub-group context

    return benefit, services, notes


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
    # Lines like "$250 maximum per drug" or "$500 maximum per drug" are cap notes
    # that continue the preceding cost — not standalone cost entries
    COST_CAP = re.compile(r"^\$[\d,]+\s+maximum\s+per", re.I)
    TIER_LINE = re.compile(r"^(kinwell|all\s+other)", re.I)

    costs = []
    current = ""

    for line in cell_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if COST_START.match(line) and not COST_CAP.match(line):
            if current:
                costs.append(current)
            current = line
        elif current:
            current += " " + line  # wrapped continuation (including cap notes)

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


def parse_summary_page(pdf_path):
    """
    Parse the 'Summary of Your Costs' page for plan-level cost-sharing data:
    deductibles, coinsurance, out-of-pocket maximums, and professional visit copays.

    These values appear as visually-aligned text rows (not real PDF tables), so
    we use extract_text() and match each known row with a specific regex pattern.

    Identifies the right page by requiring at least 2 of these signals:
        - "individual deductible"
        - "family deductible"
        - "out-of-pocket maximum"
        - "professional visit copay"

    Returns a list of index entries in the standard schema.
    """
    import pdfplumber

    # Each tuple: (regex, event_name, service_name)
    # regex must have group(1)=in_network, group(2)=out_of_network
    # For the 3-tier copay row, group(1)/group(2)/group(3) = Kinwell/NonSpec/Spec
    PATTERNS = [
        (
            re.compile(
                r"^Professional visit copay\s+(\$[\d,]+)\s+(\$[\d,]+)\s+(\$[\d,]+)$",
                re.I,
            ),
            "Professional Visit Copay",
            "Professional visit copay",
            "three_tier",
        ),
        (
            re.compile(r"^Individual deductible\s+(\$[\d,]+)\s+(.+)$", re.I),
            "Deductible",
            "Individual deductible",
            "two_col",
        ),
        (
            re.compile(r"^Family deductible\s+(\$[\d,]+)\s+(.+)$", re.I),
            "Deductible",
            "Family deductible",
            "two_col",
        ),
        (
            re.compile(r"^Coinsurance\s+(\d+%)\s+(\d+%)$", re.I),
            "Coinsurance",
            "Coinsurance",
            "two_col",
        ),
        (
            re.compile(
                r"^Individual out-of-pocket maximum\s+(\$[\d,]+)\s+(\w+)$", re.I
            ),
            "Out-of-Pocket Maximum",
            "Individual out-of-pocket maximum",
            "two_col",
        ),
        (
            re.compile(r"^Family out-of-pocket maximum\s+(\$[\d,]+)\s+(\w+)$", re.I),
            "Out-of-Pocket Maximum",
            "Family out-of-pocket maximum",
            "two_col",
        ),
    ]

    SIGNALS = [
        "individual deductible",
        "family deductible",
        "out-of-pocket maximum",
        "professional visit copay",
    ]

    entries = []

    def add(event, service, in_net, out_net):
        entries.append(
            {
                "topic": f"{event} \u2014 {service}",
                "category": "cost",
                "benefit_category": "medical",
                "content": {
                    "event": event,
                    "service": service,
                    "in_network": in_net,
                    "out_of_network": out_net,
                    "limitations": "Data Not Found",
                },
                "keywords": get_smart_keywords(
                    {
                        "event": event,
                        "service": service,
                        "in_network": in_net,
                        "out_of_network": out_net,
                    }
                ),
            }
        )

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            text_lower = text.lower()
            if not all(s in text_lower for s in SIGNALS):
                continue

            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                for pattern, event, service, kind in PATTERNS:
                    m = pattern.match(line)
                    if not m:
                        continue
                    if kind == "three_tier":
                        in_net = (
                            f"Kinwell Clinic: {m.group(1)} / "
                            f"All Other Non-Specialist: {m.group(2)} / "
                            f"All Other Specialist: {m.group(3)}"
                        )
                        add(event, service, in_net, "Data Not Found")
                    else:
                        add(event, service, m.group(1), m.group(2))
                    break  # matched — move to next line

            if entries:
                break  # found and parsed the summary page

    return entries


def parse_prose_sections(pdf_path):
    """
    Scan all prose pages and extract every benefit-relevant section
    as a category="info" entry.

    Fixes applied:
    1. Title Case benefit headers detected (Blood Products, Emergency Room, etc.)
    2. Continuation headers merged (Cellular Immunotherapy And + Gene Therapy)
    3. Cost table content filtered out (deductible/copay leakage)
    4. Address/contact noise filtered out
    5. Lines starting with ( filtered out
    """
    import pdfplumber

    ADMIN_SECTIONS = {
        # Enrollment / eligibility
        "WHEN DOES COVERAGE BEGIN",
        "ENROLLMENT",
        "SPECIAL ENROLLMENT",
        "WHO IS ELIGIBLE",
        "SUBSCRIBER ELIGIBILITY",
        "DEPENDENT ELIGIBILITY",
        "CHANGES IN COVERAGE",
        "PLAN TRANSFERS",
        "EVENTS THAT END COVERAGE",
        "PLAN TERMINATION",
        "CONTINUED ELIGIBILITY",
        "LEAVE OF ABSENCE",
        "LABOR DISPUTE",
        # Continuation / COBRA
        "HOW DO I CONTINUE COVERAGE",
        "COBRA",
        "EXTENDED BENEFITS",
        "CONTINUATION UNDER USERRA",
        "USERRA",
        "MEDICARE SUPPLEMENT",
        # Claims / appeals
        "HOW DO I FILE A CLAIM",
        "WHERE TO SEND CLAIMS",
        "MAIL YOUR CLAIMS",
        "COMPLAINTS AND APPEALS",
        "WHAT YOU CAN APPEAL",
        "APPEAL LEVELS",
        "IF WE NEED MORE TIME",
        "WHAT IF IT",
        "HOW TO ASK FOR AN EXTERNAL",
        "ONCE THE IRO",
        "EXTERNAL REVIEW",
        # Coordination / recovery
        "WHAT IF YOU HAVE ONGOING CARE",
        "COORDINATING BENEFITS",
        "WHAT IF I HAVE OTHER COVERAGE",
        "THIRD PARTY RECOVERY",
        # Admin / legal
        "PRIVACY",
        "NOTICE OF INFORMATION",
        "ERISA",
        "YOUR ERISA RIGHTS",
        "TYPE OF ADMINISTRATION",
        "RIGHT TO AND PAYMENT",
        "RIGHT OF RECOVERY",
        "OTHER INFORMATION ABOUT THIS PLAN",
        "CONFORMITY WITH THE LAW",
        "TIMELY FILING",
        "VENUE",
        # Navigation / contact
        "DEFINITIONS",
        "CONTACT US",
        "FOR MORE INFORMATION",
        "YOUR IDENTIFICATION CARD",
        "HOW TO USE THIS BOOKLET",
        "TABLE OF CONTENTS",
        "INTRODUCTION",
    }

    def is_admin(header):
        h = re.sub(r"\s+", " ", header.upper().strip())
        return any(a in h for a in ADMIN_SECTIONS)

    def is_address_header(header):
        lower = header.lower()
        return any(
            p in lower
            for p in (
                "wa 98",
                "po box",
                "mailing address",
                "phone number",
                "seattle,",
                "mountlake",
                "bluecard website",
                "844-",
                ", wa ",
            )
        )

    def is_cost_table_content(text):
        """True if content is cost table data, not a prose description."""
        lower = text.lower()
        cost_hits = sum(
            1
            for w in (
                "coinsurance",
                "copay",
                "deductible",
                "in-network",
                "out-of-network",
            )
            if w in lower
        )
        return cost_hits >= 2 and len(text) < 300

    def is_section_header(line):
        if len(line) < 4 or len(line) > 100:
            return False
        TABLE_WORDS = (
            "IN-NETWORK PROVIDERS",
            "OUT-OF-NETWORK PROVIDERS",
            "YOUR SHARE OF THE ALLOWED AMOUNT",
            "BENEFIT IN-NETWORK",
        )
        if any(w in line.upper() for w in TABLE_WORDS):
            return False
        # Skip sentences, bullets, symbols, parentheses
        if line.rstrip().endswith("."):
            return False
        if re.match(r"^[•\-\*\d\$%(]", line):
            return False
        words = line.split()
        upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
        if upper_ratio > 0.7:
            return True
        SENTENCE_STARTERS = {
            "the",
            "a",
            "an",
            "you",
            "we",
            "this",
            "these",
            "for",
            "if",
            "when",
            "while",
            "note",
            "see",
            "benefits",
            "covered",
            "services",
            "some",
            "in",
        }
        if 2 <= len(words) <= 7:
            cap_ratio = sum(1 for w in words if w and w[0].isupper()) / len(words)
            first_word = words[0].lower() if words else ""
            if cap_ratio >= 0.6 and first_word not in SENTENCE_STARTERS:
                return True
        return False

    # Continuation endings: header split across lines (e.g. "Cellular Immunotherapy And")
    CONTINUATION_ENDINGS = re.compile(r"\b(And|Or|Of|–|-|The|For|In|A)\s*$", re.I)

    all_lines = []  # list of (line_text, page_num)
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            # Skip pages that contain the cost table header — those are
            # benefit cost table pages and must never contribute info chunks
            if "YOUR SHARE OF THE ALLOWED AMOUNT" in page_text.upper():
                continue
            for line in page_text.split("\n"):
                stripped = line.strip()
                if stripped:
                    all_lines.append((stripped, page.page_number))

    sections = []  # list of (header, content_lines, page_num)
    current_header = None
    current_content = []
    current_page = 0

    for line, pnum in all_lines:
        if is_section_header(line):
            if current_header and current_content:
                sections.append((current_header, current_content, current_page))
            # Merge continuation: "Cellular Immunotherapy And" + "Gene Therapy"
            if (
                current_header
                and not current_content
                and CONTINUATION_ENDINGS.search(current_header)
            ):
                current_header = current_header.rstrip("–- ") + " " + line
            else:
                current_header = line
                current_content = []
                current_page = pnum
        elif current_header:
            current_content.append(line)

    if current_header and current_content:
        sections.append((current_header, current_content, current_page))

    # Pattern: inline benefit name appearing mid-content followed by benefit description.
    # e.g. "• Serums Ambulance This benefit covers: ..." should split at "Ambulance".
    # Matches 1-6 Title Case words immediately before "This benefit covers/does not cover".
    INLINE_SPLIT = re.compile(
        r"(?<!\. )(?<![•] )([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,5})\s+"
        r"(This benefit (?:covers|does not cover):)",
    )

    def split_inline_benefits(text, base_page):
        """
        Split text at inline benefit boundaries and return list of (name, text, page).
        If no inline boundaries found, returns [(None, text, base_page)].
        """
        parts = []
        last = 0
        for m in INLINE_SPLIT.finditer(text):
            before = text[last : m.start()].strip()
            if before:
                parts.append((None, before, base_page))
            last = m.start(1)
        remainder = text[last:].strip()
        if remainder:
            parts.append((None, remainder, base_page))
        if not parts:
            return [(None, text, base_page)]
        # First part keeps original name; subsequent parts extract name from text start
        result = []
        for i, (_, chunk, pg) in enumerate(parts):
            m2 = re.match(
                r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,5})\s+(This benefit)",
                chunk,
            )
            if m2:
                result.append((m2.group(1), chunk, pg))
            else:
                result.append((None, chunk, pg))
        return result

    entries = []
    seen_headers = set()

    for header, content_lines, section_page in sections:
        if is_admin(header):
            continue
        if is_address_header(header):
            continue

        header_key = re.sub(r"\s+", " ", header.upper().strip())
        if header_key in seen_headers:
            continue
        seen_headers.add(header_key)

        content_text = " ".join(content_lines).strip()
        if len(content_text) < 80:
            continue
        if is_cost_table_content(content_text):
            continue

        for inline_name, chunk_text, chunk_page in split_inline_benefits(
            content_text, section_page
        ):
            event = (inline_name or header).strip().title()
            entries.append(
                {
                    "topic": f"{event} \u2014 Coverage Information",
                    "category": "info",
                    "benefit_category": "medical",
                    "content": {
                        "event": event,
                        "service": "Coverage Information",
                        "in_network": "Data Not Found",
                        "out_of_network": "Data Not Found",
                        "limitations": chunk_text,
                    },
                    "keywords": get_smart_keywords(
                        {
                            "event": event,
                            "limitations": chunk_text,
                        }
                    ),
                    "page_number": chunk_page,
                }
            )

    return entries


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
    import pdfplumber

    sub_index = []
    seen = set()

    def add(topic, content, page_num: int = 0):
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
                    "page_number": page_num,
                }
            )

    last_benefit = ""  # carries the benefit name across page-continuation rows

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_num = page.page_number
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
                    benefit, services, notes = parse_benefit_cell(benefit_cell)
                    limitations = " ".join(notes)  # coverage notes for member context

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
                                    "limitations": limitations or "Data Not Found",
                                },
                                page_num,
                            )
                        continue

                    # --- INDEX ROWS WITH BULLET SERVICES ---
                    in_costs = parse_cost_column(in_net_cell)
                    out_costs = parse_cost_column(out_net_cell)

                    # Pair each service with its cost by index.
                    # services is a list of (service_name, service_limitation) tuples.
                    # Use service-specific limitation if present, else fall back to
                    # the benefit-level notes captured before the bullets.
                    last_in = ""
                    last_out = ""
                    for idx, (service, svc_notes, sub_ctx) in enumerate(services):
                        if idx < len(in_costs):
                            last_in = in_costs[idx]
                        if idx < len(out_costs):
                            last_out = out_costs[idx]

                        # Include sub-group context in topic so it's searchable.
                        # e.g. "Medical Transportation — For total hip... — To/from COE"
                        if sub_ctx:
                            topic = f"{benefit} \u2014 {sub_ctx} \u2014 {service}"
                        else:
                            topic = f"{benefit} \u2014 {service}"

                        add(
                            topic,
                            {
                                "event": benefit,
                                "service": service,
                                "in_network": last_in,
                                "out_of_network": last_out,
                                "limitations": svc_notes
                                or limitations
                                or "Data Not Found",
                            },
                            page_num,
                        )

    # Parse the Summary of Your Costs page for plan-level cost-sharing data.
    # Wrapped in try/except — a failure here must never block the main benefit index.
    try:
        summary_entries = parse_summary_page(pdf_path)
        sub_index.extend(summary_entries)
        if summary_entries:
            print(f"[+] Summary page: {len(summary_entries)} plan-level entries added")
        else:
            print("[!] Summary page: no entries found — page structure may differ")
    except Exception as e:
        print(f"[!] Summary page parsing failed (benefit index unaffected): {e}")

    # Parse prose sections (coverage descriptions, provider rules, exclusions etc.)
    # as category="info" entries so members can ask "what is covered?" questions.
    # FULLY ISOLATED — any failure here never touches the cost table entries above.
    try:
        info_entries = parse_prose_sections(pdf_path)
        sub_index.extend(info_entries)
        if info_entries:
            print(f"[+] Prose sections: {len(info_entries)} info entries added")
        else:
            print("[!] Prose sections: no entries found")
    except Exception as e:
        print(f"[!] Prose parsing failed (cost table unaffected): {e}")

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# ==========================Previous working code before page number addition==========================#

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
# # from utility.utils import get_smart_keywords

# # load_dotenv()

# # CURRENT_YEAR_INT = datetime.now().year


# # def classify_document(pdf_path):
# #     """
# #     Read the first few pages of the Medical booklet PDF and extract:
# #     - year
# #     - group_number
# #     - group_name
# #     - plan
# #     - type
# #     - tier
# #     - variant
# #     - network
# #     """

# #     try:
# #         with pdfplumber.open(pdf_path) as pdf:
# #             header_text = ""

# #             # ---------------------------------------------------------
# #             # 🔥 READ FIRST FEW PAGES
# #             # ---------------------------------------------------------
# #             for page in pdf.pages[:3]:
# #                 header_text += (page.extract_text() or "") + "\n"

# #                 if len(header_text) > 4000:
# #                     break

# #         # ---------------------------------------------------------
# #         # 🔥 EXTRACTION PROMPT
# #         # ---------------------------------------------------------
# #         prompt = f"""
# #         ACT AS A STRICT STRUCTURED DATA EXTRACTOR.
# #         Extract ONLY if explicitly present in the text. DO NOT GUESS.

# #         ----------------------------------------
# #         🎯 FIELDS TO EXTRACT
# #         ----------------------------------------

# #         1. year
# #         - Extract from:
# #             • "Effective Date"
# #             • "Coverage Period"
# #             • Any date like "January 1, YYYY"
# #         - Return only the year as integer (e.g. 2025)

# #         2. group_number
# #         - Extract from:
# #             • "Group Number"
# #             • Standalone number near header
# #         - Return as string

# #         3. group_name
# #         - Extract from:
# #             • "Group Name"
# #         - Return full text exactly

# #         4. plan
# #         - Extract FULL plan name exactly as written
# #         - Examples:
# #             "Premera Employees Health Plan – Standard PPO Retiree Plan"
# #             "Heritage Prime HMO Gold 2500"

# #         5. type
# #         - Extract from explicit label OR plan name
# #         - Allowed values ONLY:
# #             HMO, PPO, EPO, HSA

# #         6. tier
# #         - Extract ONLY if explicitly present:
# #             Gold / Silver / Bronze / Platinum
# #         - Else return null

# #         7. variant
# #         - Extract plan modifiers such as:
# #             Standard
# #             Retiree
# #             Basic
# #             Plus
# #             HDHP
# #             Advantage
# #         - Return as string
# #         - If none found → return "Standard"

# #         8. network
# #         - Extract ONLY if explicitly mentioned
# #         - Examples:
# #             BlueCard Network
# #             National Network
# #             Heritage Network
# #             In-Network Only
# #         - Else return null

# #         ----------------------------------------
# #         🚫 STRICT RULES
# #         ----------------------------------------

# #         - DO NOT infer missing fields
# #         - DO NOT guess network from PPO/HMO
# #         - DO NOT fabricate tier
# #         - DO NOT merge unrelated fields
# #         - If missing → return null
# #         - Return STRICT JSON ONLY

# #         ----------------------------------------
# #         ✅ OUTPUT FORMAT
# #         ----------------------------------------

# #         {{
# #             "year": 2025,
# #             "group_number": "1000016",
# #             "group_name": "Premera Employees Health Plan",
# #             "plan": "Premera Employees Health Plan – Standard PPO Retiree Plan",
# #             "type": "PPO",
# #             "tier": null,
# #             "variant": "Retiree",
# #             "network": null
# #         }}

# #         ----------------------------------------
# #         TEXT:
# #         {header_text[:4000].strip()}
# #         """

# #         # ---------------------------------------------------------
# #         # 🔥 CALL LLM
# #         # ---------------------------------------------------------
# #         response = ollama.generate(
# #             model=os.getenv("OLLAMA_MODEL", "llama3.1"),
# #             prompt=prompt,
# #             format="json",
# #             options={
# #                 "temperature": 0,
# #             },
# #         )

# #         raw_response = response.get("response", "{}")

# #         print(f"[*] RAW DOCUMENT CLASSIFICATION RESPONSE: {raw_response}")

# #         data = json_lib.loads(raw_response)

# #         # ---------------------------------------------------------
# #         # 🔥 SAFE NORMALIZATION
# #         # ---------------------------------------------------------

# #         # YEAR
# #         raw_year = str(data.get("year", "")).strip()
# #         year_match = re.search(r"\d{4}", raw_year)

# #         year = int(year_match.group()) if year_match else CURRENT_YEAR_INT

# #         # TYPE
# #         plan_type = str(data.get("type", "")).strip().upper()

# #         allowed_types = {"HMO", "PPO", "EPO", "HSA"}

# #         if plan_type not in allowed_types:
# #             plan_type = "UNKNOWN"

# #         # TIER
# #         tier = data.get("tier")

# #         if tier:
# #             tier = str(tier).strip().capitalize()
# #         else:
# #             tier = None

# #         # GROUP NUMBER
# #         group_number = data.get("group_number")

# #         if group_number:
# #             group_number = str(group_number).strip()
# #         else:
# #             group_number = None

# #         # GROUP NAME
# #         group_name = data.get("group_name")

# #         if group_name:
# #             group_name = str(group_name).strip()
# #         else:
# #             group_name = None

# #         # PLAN
# #         plan = data.get("plan")

# #         if plan:
# #             plan = str(plan).strip()
# #         else:
# #             plan = "Unknown Plan"

# #         # VARIANT
# #         variant = data.get("variant")

# #         if variant:
# #             variant = str(variant).strip()
# #         else:
# #             variant = "Standard"

# #         # NETWORK
# #         network = data.get("network")

# #         if network:
# #             network = str(network).strip()
# #         else:
# #             network = None

# #         # ---------------------------------------------------------
# #         # 🔥 FINAL STRUCTURED OUTPUT
# #         # ---------------------------------------------------------
# #         final_data = {
# #             "year": year,
# #             "group_number": group_number,
# #             "group_name": group_name,
# #             "plan": plan,
# #             "type": plan_type,
# #             "tier": tier,
# #             "variant": variant,
# #             "network": network,
# #         }

# #         print(f"[*] FINAL DOCUMENT CLASSIFICATION: {final_data}")

# #         return final_data

# #     except Exception as e:
# #         print(f"[!] Medical classification failed: {e}")
# #         return None


# # def clean(text):
# #     """Collapse all whitespace in a string to a single space."""
# #     return re.sub(r"\s+", " ", str(text or "")).strip()


# # def parse_benefit_cell(cell_text):
# #     """
# #     Extract the benefit name and flat list of leaf services from col 0.

# #     BENEFIT NAME  — non-bullet lines at the top, until a bullet or
# #                     a description line (starts with "See ", "Includes ", etc.)

# #     LEAF SERVICES — bullet lines that are NOT headers. A bullet is a header
# #                     (and gets skipped) when ANY of these are true:
# #                     1. Contains "(See ...)"       — cross-reference
# #                     2. First word is in bold_headers — bold font on page
# #                     3. Starts with "For "         — sub-group label
# #                     4. Has a limit-note continuation (calendar year, day limit...)
# #                         AND the next item is also a bullet
# #                         AND the bullet text is a generic category word
# #                         (e.g. "Outpatient care", "Inpatient care")

# #     NON-BULLET LINES with "$" after the benefit name are also collected as
# #     services — they are bold service labels like "For transplants: $7,500".
# #     """
# #     # --- Patterns ---
# #     CROSS_REF = re.compile(r"\s*\(See.*", re.I)
# #     LIMIT_NOTE = re.compile(
# #         r"^(calendar\s+year|day\s+limit|visit\s+limit|no\s+limit|"
# #         r"limited\s+to|limit\s+each|limit\s+\$|\(copay|\(your)",
# #         re.I,
# #     )
# #     DESC_LINE = re.compile(
# #         r"^(see\s+the\s+|includes\s+|you\s+may|for\s+hip|for\s+therapies|"
# #         r"for\s+hearing|covers\s+routine|such\s+as|benefits\s+are|"
# #         r"travel\s+\(|travel\s+and|special\s+criteria|"
# #         r"for\s+coverage|for\s+permanent|care\s+during|"
# #         r"for\s+member)",
# #         re.I,
# #     )
# #     GENERIC_CAT = re.compile(
# #         r"^(outpatient\s+care|inpatient\s+care|outpatient\s+services|"
# #         r"home\s+care|office\s+care)$",
# #         re.I,
# #     )
# #     SERVICE_STOP = re.compile(
# #         r"\s+(you\s+may|the\s+copay|this\s+plan|see\s+those|"
# #         r"\*all\s+approved|special\s+criteria|"
# #         r"for\s+coverage\s+details|virtual\s+pediatric|"
# #         r"for\s+members\s+\d).*",
# #         re.I,
# #     )
# #     # Non-bullet $ lines that are limit notes, not service labels
# #     LIMIT_DOLLAR = re.compile(r"^limit\s+\$", re.I)

# #     def is_header(item, has_limit_cont, next_is_bullet):
# #         """Return True if this bullet should be skipped as a sub-section header."""
# #         return (
# #             bool(re.search(r"\(See", item, re.I))  # cross-reference
# #             or item.lower().startswith("for ")  # sub-group label
# #             or (
# #                 has_limit_cont and next_is_bullet and GENERIC_CAT.match(item)
# #             )  # generic category
# #         )

# #     lines_ = cell_text.split("\n")
# #     benefit = ""
# #     notes = []  # "You may have..." type notes — important for member context
# #     services = []
# #     i = 0

# #     # Step 1: collect benefit name — stop at bullets or description lines
# #     while i < len(lines_):
# #         line = lines_[i].strip()
# #         if not line:
# #             i += 1
# #             continue
# #         if line.startswith("•") or DESC_LINE.match(line):
# #             break
# #         benefit = (benefit + " " + line).strip()
# #         i += 1

# #     # Step 1b: collect note lines that follow the benefit name (before bullets)
# #     # e.g. "Covers routine patient care during the trial"
# #     #      "You may have additional costs for other services..."
# #     # These are important coverage context — captured for the limitations field.
# #     NOTE_LINE = re.compile(
# #         r"^(you\s+may|covers\s+routine|for\s+permanent|care\s+during|"
# #         r"benefits\s+are\s+limited|limited\s+as\s+follows|"
# #         r"for\s+member|includes\s+travel|travel\s+and\s+lodging|"
# #         r"prior\s+approval|special\s+criteria)",
# #         re.I,
# #     )
# #     note_parts = []
# #     while i < len(lines_):
# #         line = lines_[i].strip()
# #         if not line:
# #             i += 1
# #             continue
# #         if line.startswith("•"):
# #             break
# #         # Stop if this line or the next has "$" — it's a service label, not a note
# #         peek = lines_[i + 1].strip() if i + 1 < len(lines_) else ""
# #         if "$" in line or "$" in peek:
# #             break
# #         if NOTE_LINE.match(line) or note_parts:
# #             note_parts.append(line)
# #         i += 1
# #     if note_parts:
# #         notes.append(clean(" ".join(note_parts)))

# #     # Step 2: collect services
# #     subgroup_context = ""  # text of the most recent skipped "• For X" sub-group bullet
# #     # e.g. "For total hip and knee joint replacements..."
# #     # attached to subsequent leaf services so they're searchable

# #     while i < len(lines_):
# #         line = lines_[i].strip()
# #         i += 1
# #         if not line:
# #             continue

# #         if line.startswith("•"):
# #             item = re.sub(r"^•\s*", "", line)

# #             # Collect continuation lines — split into name parts and note parts.
# #             # Lines starting with "You may..." or "The copay..." are notes for
# #             # THIS service specifically, not part of the service name.
# #             has_limit = False
# #             past_limit_note = (
# #                 False  # once limit text starts, skip its continuations too
# #             )
# #             svc_note = []
# #             collecting_note = False
# #             SVC_NOTE = re.compile(r"^(you\s+may|the\s+copay|examples\s+are)", re.I)

# #             while i < len(lines_) and not lines_[i].strip().startswith("•"):
# #                 cont = lines_[i].strip()
# #                 peek = lines_[i + 1].strip() if i + 1 < len(lines_) else ""
# #                 if "$" in cont or "$" in peek:
# #                     break
# #                 if re.match(r"^(no\s+charge\s+on|please\s+call)", cont, re.I):
# #                     break
# #                 # A short Title-Case line with no punctuation/symbols is likely a
# #                 # bold sub-section header (e.g. "PV Core Preventive Drugs",
# #                 # "Exceptions", "Covered Drugs") — stop absorbing into service name
# #                 if (
# #                     cont
# #                     and len(cont.split()) <= 6
# #                     and cont[0].isupper()
# #                     and not any(c in cont for c in ",.;:$%()")
# #                     and not LIMIT_NOTE.match(cont)
# #                     and not SVC_NOTE.match(cont)
# #                 ):
# #                     peek_next = next(
# #                         (
# #                             lines_[j].strip()
# #                             for j in range(i + 1, len(lines_))
# #                             if lines_[j].strip()
# #                         ),
# #                         "",
# #                     )
# #                     if not peek_next.startswith("•"):
# #                         break
# #                 if LIMIT_NOTE.match(cont):
# #                     has_limit = True
# #                     past_limit_note = True  # flag: don't append subsequent lines either
# #                 elif past_limit_note:
# #                     pass  # continuation of limit note — discard
# #                 elif SVC_NOTE.match(cont) or collecting_note:
# #                     collecting_note = True
# #                     if cont and not CROSS_REF.match(cont):
# #                         svc_note.append(cont)
# #                 elif cont and not CROSS_REF.match(cont):
# #                     item += " " + cont
# #                 i += 1

# #             next_is_bullet = i < len(lines_) and lines_[i].strip().startswith("•")

# #             if is_header(item, has_limit, next_is_bullet):
# #                 # Sub-group bullet skipped — but if it starts with "For ",
# #                 # save its full text as context for the leaf services under it.
# #                 # e.g. "For total hip and knee joint replacements..."
# #                 # This makes the leaf services discoverable via that search term.
# #                 if item.lower().startswith("for "):
# #                     subgroup_context = clean(CROSS_REF.sub("", item).strip())
# #                 else:
# #                     subgroup_context = ""  # other sub-group types reset context
# #                 continue

# #             item = CROSS_REF.sub("", item).strip()
# #             item = SERVICE_STOP.sub("", item).strip()
# #             if item:
# #                 # Build limitation: service-specific note takes priority,
# #                 # then sub-group context (e.g. "For total hip..."),
# #                 # then empty (caller will apply "Data Not Found")
# #                 svc_limitation = clean(" ".join(svc_note))
# #                 services.append((clean(item), svc_limitation, subgroup_context))
# #                 # Don't reset subgroup_context here — it persists for all siblings
# #                 # under the same sub-group until a new sub-group or dollar-line resets it

# #         elif (
# #             "$" in line or (i < len(lines_) and "$" in lines_[i])
# #         ) and not LIMIT_DOLLAR.match(line):
# #             # Non-bullet bold service label, possibly split across two lines
# #             item = line
# #             if "$" not in line and "$" in lines_[i]:
# #                 item += " " + lines_[i].strip()
# #                 i += 1
# #             while i < len(lines_):
# #                 nl = lines_[i].strip()
# #                 if not nl or nl.startswith("•") or CROSS_REF.match(nl):
# #                     break
# #                 if re.match(
# #                     r"^(special|travel|benefits|for\s+surgeries|lodging)", nl, re.I
# #                 ):
# #                     break
# #                 item += " " + nl
# #                 i += 1
# #             item = SERVICE_STOP.sub("", item).strip()
# #             if item:
# #                 services.append((clean(item), "", ""))
# #                 subgroup_context = ""  # dollar-line service resets sub-group context

# #     return benefit, services, notes


# # def parse_cost_column(cell_text):
# #     """
# #     Split a cost cell into an ordered list of individual cost values.

# #     Each new cost starts with a recognisable token like "$25 copay",
# #     "Deductible, then 20%", "No charge", "Not covered", "Kinwell", etc.
# #     Lines that don't start a new cost are wrapped continuations and
# #     are appended to the current cost value.

# #     Kinwell / All Other tier lines are merged into the preceding cost
# #     entry because they describe pricing tiers for the SAME service.

# #     Example:
# #         Input  → "Kinwell Clinics: $0 copay\ndeductible waived\nAll Other: $25 copay"
# #         Output → ["Kinwell Clinics: $0 copay deductible waived  All Other: $25 copay"]
# #     """
# #     COST_START = re.compile(
# #         r"^(\$\d|\d+%|no\s+charge|not\s+covered|no\s+cost|"
# #         r"deductible,\s*then|kinwell|all\s+other)",
# #         re.I,
# #     )
# #     # Lines like "$250 maximum per drug" or "$500 maximum per drug" are cap notes
# #     # that continue the preceding cost — not standalone cost entries
# #     COST_CAP = re.compile(r"^\$[\d,]+\s+maximum\s+per", re.I)
# #     TIER_LINE = re.compile(r"^(kinwell|all\s+other)", re.I)

# #     costs = []
# #     current = ""

# #     for line in cell_text.split("\n"):
# #         line = line.strip()
# #         if not line:
# #             continue
# #         if COST_START.match(line) and not COST_CAP.match(line):
# #             if current:
# #                 costs.append(current)
# #             current = line
# #         elif current:
# #             current += " " + line  # wrapped continuation (including cap notes)

# #     if current:
# #         costs.append(current)

# #     # Merge consecutive Kinwell / All Other tier entries into one string
# #     # so tiered pricing appears as a single readable cost value
# #     merged = []
# #     for cost in costs:
# #         if merged and TIER_LINE.match(cost) and TIER_LINE.match(merged[-1]):
# #             merged[-1] += "  " + cost
# #         else:
# #             merged.append(cost)

# #     return merged


# # def parse_summary_page(pdf_path):
# #     """
# #     Parse the 'Summary of Your Costs' page for plan-level cost-sharing data:
# #     deductibles, coinsurance, out-of-pocket maximums, and professional visit copays.

# #     These values appear as visually-aligned text rows (not real PDF tables), so
# #     we use extract_text() and match each known row with a specific regex pattern.

# #     Identifies the right page by requiring at least 2 of these signals:
# #         - "individual deductible"
# #         - "family deductible"
# #         - "out-of-pocket maximum"
# #         - "professional visit copay"

# #     Returns a list of index entries in the standard schema.
# #     """
# #     import pdfplumber

# #     # Each tuple: (regex, event_name, service_name)
# #     # regex must have group(1)=in_network, group(2)=out_of_network
# #     # For the 3-tier copay row, group(1)/group(2)/group(3) = Kinwell/NonSpec/Spec
# #     PATTERNS = [
# #         (
# #             re.compile(
# #                 r"^Professional visit copay\s+(\$[\d,]+)\s+(\$[\d,]+)\s+(\$[\d,]+)$",
# #                 re.I,
# #             ),
# #             "Professional Visit Copay",
# #             "Professional visit copay",
# #             "three_tier",
# #         ),
# #         (
# #             re.compile(r"^Individual deductible\s+(\$[\d,]+)\s+(.+)$", re.I),
# #             "Deductible",
# #             "Individual deductible",
# #             "two_col",
# #         ),
# #         (
# #             re.compile(r"^Family deductible\s+(\$[\d,]+)\s+(.+)$", re.I),
# #             "Deductible",
# #             "Family deductible",
# #             "two_col",
# #         ),
# #         (
# #             re.compile(r"^Coinsurance\s+(\d+%)\s+(\d+%)$", re.I),
# #             "Coinsurance",
# #             "Coinsurance",
# #             "two_col",
# #         ),
# #         (
# #             re.compile(
# #                 r"^Individual out-of-pocket maximum\s+(\$[\d,]+)\s+(\w+)$", re.I
# #             ),
# #             "Out-of-Pocket Maximum",
# #             "Individual out-of-pocket maximum",
# #             "two_col",
# #         ),
# #         (
# #             re.compile(r"^Family out-of-pocket maximum\s+(\$[\d,]+)\s+(\w+)$", re.I),
# #             "Out-of-Pocket Maximum",
# #             "Family out-of-pocket maximum",
# #             "two_col",
# #         ),
# #     ]

# #     SIGNALS = [
# #         "individual deductible",
# #         "family deductible",
# #         "out-of-pocket maximum",
# #         "professional visit copay",
# #     ]

# #     entries = []

# #     def add(event, service, in_net, out_net):
# #         entries.append(
# #             {
# #                 "topic": f"{event} \u2014 {service}",
# #                 "category": "cost",
# #                 "benefit_category": "medical",
# #                 "content": {
# #                     "event": event,
# #                     "service": service,
# #                     "in_network": in_net,
# #                     "out_of_network": out_net,
# #                     "limitations": "Data Not Found",
# #                 },
# #                 "keywords": get_smart_keywords(
# #                     {
# #                         "event": event,
# #                         "service": service,
# #                         "in_network": in_net,
# #                         "out_of_network": out_net,
# #                     }
# #                 ),
# #             }
# #         )

# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             text = page.extract_text() or ""
# #             text_lower = text.lower()
# #             if not all(s in text_lower for s in SIGNALS):
# #                 continue

# #             for line in text.split("\n"):
# #                 line = line.strip()
# #                 if not line:
# #                     continue
# #                 for pattern, event, service, kind in PATTERNS:
# #                     m = pattern.match(line)
# #                     if not m:
# #                         continue
# #                     if kind == "three_tier":
# #                         in_net = (
# #                             f"Kinwell Clinic: {m.group(1)} / "
# #                             f"All Other Non-Specialist: {m.group(2)} / "
# #                             f"All Other Specialist: {m.group(3)}"
# #                         )
# #                         add(event, service, in_net, "Data Not Found")
# #                     else:
# #                         add(event, service, m.group(1), m.group(2))
# #                     break  # matched — move to next line

# #             if entries:
# #                 break  # found and parsed the summary page

# #     return entries


# # def parse_prose_sections(pdf_path):
# #     """
# #     Scan all prose pages and extract every benefit-relevant section
# #     as a category="info" entry.

# #     Fixes applied:
# #     1. Title Case benefit headers detected (Blood Products, Emergency Room, etc.)
# #     2. Continuation headers merged (Cellular Immunotherapy And + Gene Therapy)
# #     3. Cost table content filtered out (deductible/copay leakage)
# #     4. Address/contact noise filtered out
# #     5. Lines starting with ( filtered out
# #     """
# #     import pdfplumber

# #     ADMIN_SECTIONS = {
# #         # Enrollment / eligibility
# #         "WHEN DOES COVERAGE BEGIN",
# #         "ENROLLMENT",
# #         "SPECIAL ENROLLMENT",
# #         "WHO IS ELIGIBLE",
# #         "SUBSCRIBER ELIGIBILITY",
# #         "DEPENDENT ELIGIBILITY",
# #         "CHANGES IN COVERAGE",
# #         "PLAN TRANSFERS",
# #         "EVENTS THAT END COVERAGE",
# #         "PLAN TERMINATION",
# #         "CONTINUED ELIGIBILITY",
# #         "LEAVE OF ABSENCE",
# #         "LABOR DISPUTE",
# #         # Continuation / COBRA
# #         "HOW DO I CONTINUE COVERAGE",
# #         "COBRA",
# #         "EXTENDED BENEFITS",
# #         "CONTINUATION UNDER USERRA",
# #         "USERRA",
# #         "MEDICARE SUPPLEMENT",
# #         # Claims / appeals
# #         "HOW DO I FILE A CLAIM",
# #         "WHERE TO SEND CLAIMS",
# #         "MAIL YOUR CLAIMS",
# #         "COMPLAINTS AND APPEALS",
# #         "WHAT YOU CAN APPEAL",
# #         "APPEAL LEVELS",
# #         "IF WE NEED MORE TIME",
# #         "WHAT IF IT",
# #         "HOW TO ASK FOR AN EXTERNAL",
# #         "ONCE THE IRO",
# #         "EXTERNAL REVIEW",
# #         # Coordination / recovery
# #         "WHAT IF YOU HAVE ONGOING CARE",
# #         "COORDINATING BENEFITS",
# #         "WHAT IF I HAVE OTHER COVERAGE",
# #         "THIRD PARTY RECOVERY",
# #         # Admin / legal
# #         "PRIVACY",
# #         "NOTICE OF INFORMATION",
# #         "ERISA",
# #         "YOUR ERISA RIGHTS",
# #         "TYPE OF ADMINISTRATION",
# #         "RIGHT TO AND PAYMENT",
# #         "RIGHT OF RECOVERY",
# #         "OTHER INFORMATION ABOUT THIS PLAN",
# #         "CONFORMITY WITH THE LAW",
# #         "TIMELY FILING",
# #         "VENUE",
# #         # Navigation / contact
# #         "DEFINITIONS",
# #         "CONTACT US",
# #         "FOR MORE INFORMATION",
# #         "YOUR IDENTIFICATION CARD",
# #         "HOW TO USE THIS BOOKLET",
# #         "TABLE OF CONTENTS",
# #         "INTRODUCTION",
# #     }

# #     def is_admin(header):
# #         h = re.sub(r"\s+", " ", header.upper().strip())
# #         return any(a in h for a in ADMIN_SECTIONS)

# #     def is_address_header(header):
# #         lower = header.lower()
# #         return any(
# #             p in lower
# #             for p in (
# #                 "wa 98",
# #                 "po box",
# #                 "mailing address",
# #                 "phone number",
# #                 "seattle,",
# #                 "mountlake",
# #                 "bluecard website",
# #                 "844-",
# #                 ", wa ",
# #             )
# #         )

# #     def is_cost_table_content(text):
# #         """True if content is cost table data, not a prose description."""
# #         lower = text.lower()
# #         cost_hits = sum(
# #             1
# #             for w in (
# #                 "coinsurance",
# #                 "copay",
# #                 "deductible",
# #                 "in-network",
# #                 "out-of-network",
# #             )
# #             if w in lower
# #         )
# #         return cost_hits >= 2 and len(text) < 300

# #     def is_section_header(line):
# #         if len(line) < 4 or len(line) > 100:
# #             return False
# #         TABLE_WORDS = (
# #             "IN-NETWORK PROVIDERS",
# #             "OUT-OF-NETWORK PROVIDERS",
# #             "YOUR SHARE OF THE ALLOWED AMOUNT",
# #             "BENEFIT IN-NETWORK",
# #         )
# #         if any(w in line.upper() for w in TABLE_WORDS):
# #             return False
# #         # Skip sentences, bullets, symbols, parentheses
# #         if line.rstrip().endswith("."):
# #             return False
# #         if re.match(r"^[•\-\*\d\$%(]", line):
# #             return False
# #         words = line.split()
# #         upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
# #         if upper_ratio > 0.7:
# #             return True
# #         SENTENCE_STARTERS = {
# #             "the",
# #             "a",
# #             "an",
# #             "you",
# #             "we",
# #             "this",
# #             "these",
# #             "for",
# #             "if",
# #             "when",
# #             "while",
# #             "note",
# #             "see",
# #             "benefits",
# #             "covered",
# #             "services",
# #             "some",
# #             "in",
# #         }
# #         if 2 <= len(words) <= 7:
# #             cap_ratio = sum(1 for w in words if w and w[0].isupper()) / len(words)
# #             first_word = words[0].lower() if words else ""
# #             if cap_ratio >= 0.6 and first_word not in SENTENCE_STARTERS:
# #                 return True
# #         return False

# #     # Continuation endings: header split across lines (e.g. "Cellular Immunotherapy And")
# #     CONTINUATION_ENDINGS = re.compile(r"\b(And|Or|Of|–|-|The|For|In|A)\s*$", re.I)

# #     all_lines = []
# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             text = page.extract_text() or ""
# #             for line in text.split("\n"):
# #                 stripped = line.strip()
# #                 if stripped:
# #                     all_lines.append(stripped)

# #     sections = []
# #     current_header = None
# #     current_content = []

# #     for line in all_lines:
# #         if is_section_header(line):
# #             if current_header and current_content:
# #                 sections.append((current_header, current_content))
# #             # Merge continuation: "Cellular Immunotherapy And" + "Gene Therapy"
# #             if (
# #                 current_header
# #                 and not current_content
# #                 and CONTINUATION_ENDINGS.search(current_header)
# #             ):
# #                 current_header = current_header.rstrip("–- ") + " " + line
# #             else:
# #                 current_header = line
# #                 current_content = []
# #         elif current_header:
# #             current_content.append(line)

# #     if current_header and current_content:
# #         sections.append((current_header, current_content))

# #     entries = []
# #     seen_headers = set()

# #     for header, content_lines in sections:
# #         if is_admin(header):
# #             continue
# #         if is_address_header(header):
# #             continue

# #         header_key = re.sub(r"\s+", " ", header.upper().strip())
# #         if header_key in seen_headers:
# #             continue
# #         seen_headers.add(header_key)

# #         content_text = " ".join(content_lines).strip()
# #         if len(content_text) < 80:
# #             continue
# #         if is_cost_table_content(content_text):
# #             continue

# #         event = header.strip().title()

# #         entries.append(
# #             {
# #                 "topic": f"{event} \u2014 Coverage Information",
# #                 "category": "info",
# #                 "benefit_category": "medical",
# #                 "content": {
# #                     "event": event,
# #                     "service": "Coverage Information",
# #                     "in_network": "Data Not Found",
# #                     "out_of_network": "Data Not Found",
# #                     "limitations": content_text,
# #                 },
# #                 "keywords": get_smart_keywords(
# #                     {
# #                         "event": event,
# #                         "limitations": content_text,
# #                     }
# #                 ),
# #             }
# #         )

# #     return entries


# # def generate_sub_index(sub_index_path, pdf_path):
# #     """
# #     Parse the Medical Benefits booklet and write a structured index file.

# #     Only pages containing "YOUR SHARE OF THE ALLOWED AMOUNT" are processed —
# #     these are the benefit cost table pages (pages 11-23 in a typical booklet).

# #     Table structure:
# #         Col 0 : Benefit name + bullet services (multi-line text)
# #         Col 3 : In-network costs   (9-col layout)
# #         Col 6 : Out-of-network costs
# #         Col 1/2 used as fallback for simpler 3-col layouts

# #     For each data row:
# #         1. Parse col 0 → benefit name + list of services
# #         2. Parse col 3 and col 6 → ordered cost lists
# #         3. Pair each service with its cost by position
# #             If fewer costs than services, the last cost is reused (inherited)

# #     Page-continuation rows: when col 0 has no benefit name (PDF splits a row
# #     across pages), the last seen benefit name is reused.
# #     """
# #     import pdfplumber

# #     sub_index = []
# #     seen = set()

# #     def add(topic, content):
# #         """Add an entry to the index, skipping duplicates."""
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

# #     last_benefit = ""  # carries the benefit name across page-continuation rows

# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             tables = page.extract_tables() or []

# #             # Skip pages that are not benefit cost tables
# #             all_cell_text = " ".join(
# #                 clean(str(c or "")) for t in tables for r in t for c in r
# #             ).upper()
# #             page_text = (page.extract_text() or "").upper()
# #             if (
# #                 "YOUR SHARE OF THE ALLOWED AMOUNT" not in all_cell_text
# #                 and "YOUR SHARE OF THE ALLOWED AMOUNT" not in page_text
# #             ):
# #                 continue

# #             # Detect bold bullet headers (sub-section labels) on this page

# #             for table in tables:
# #                 for row_idx, row in enumerate(table):
# #                     # Rows 0 and 1 are column headers — skip them
# #                     if row_idx < 2:
# #                         continue

# #                     # --- EXTRACT THE THREE DATA COLUMNS ---
# #                     # 9-col layout (most pages): data at positions 0, 3, 6
# #                     # 3-col layout (some pages): data at positions 0, 1, 2
# #                     ncols = len(row)
# #                     benefit_cell = str(row[0] or "")
# #                     in_net_cell = str(
# #                         row[3] if ncols > 6 else (row[1] if ncols > 1 else "")
# #                     )
# #                     out_net_cell = str(
# #                         row[6] if ncols > 6 else (row[2] if ncols > 2 else "")
# #                     )

# #                     # Skip rows with no content
# #                     if not benefit_cell.strip():
# #                         continue
# #                     if not in_net_cell.strip() and not out_net_cell.strip():
# #                         continue

# #                     # --- PARSE THE BENEFIT COLUMN ---
# #                     benefit, services, notes = parse_benefit_cell(benefit_cell)
# #                     limitations = " ".join(notes)  # coverage notes for member context

# #                     # Handle page-continuation rows where the benefit name is missing.
# #                     # The PDF sometimes splits a benefit across two pages, putting the
# #                     # name only on the first page.
# #                     if not benefit and last_benefit:
# #                         benefit = last_benefit
# #                     if benefit:
# #                         last_benefit = benefit

# #                     # --- INDEX ROWS WITH NO BULLET SERVICES ---
# #                     # e.g. "Allergy Testing And Treatment" — a single-service benefit
# #                     # with no sub-items. The benefit itself is the service.
# #                     if not services:
# #                         if benefit:
# #                             # Strip trailing limit notes from the benefit name
# #                             TRAIL = re.compile(
# #                                 r"\s+(calendar\s+year|lifetime\s+limit|day\s+limit|"
# #                                 r"visit\s+limit|you\s+may|the\s+copay|see\s+the).*",
# #                                 re.I,
# #                             )
# #                             svc = TRAIL.sub("", benefit).strip()
# #                             in_c = parse_cost_column(in_net_cell)
# #                             out_c = parse_cost_column(out_net_cell)
# #                             add(
# #                                 svc,
# #                                 {
# #                                     "event": svc,
# #                                     "service": svc,
# #                                     "in_network": (
# #                                         in_c[0] if in_c else clean(in_net_cell)
# #                                     ),
# #                                     "out_of_network": (
# #                                         out_c[0] if out_c else clean(out_net_cell)
# #                                     ),
# #                                     "limitations": limitations or "Data Not Found",
# #                                 },
# #                             )
# #                         continue

# #                     # --- INDEX ROWS WITH BULLET SERVICES ---
# #                     in_costs = parse_cost_column(in_net_cell)
# #                     out_costs = parse_cost_column(out_net_cell)

# #                     # Pair each service with its cost by index.
# #                     # services is a list of (service_name, service_limitation) tuples.
# #                     # Use service-specific limitation if present, else fall back to
# #                     # the benefit-level notes captured before the bullets.
# #                     last_in = ""
# #                     last_out = ""
# #                     for idx, (service, svc_notes, sub_ctx) in enumerate(services):
# #                         if idx < len(in_costs):
# #                             last_in = in_costs[idx]
# #                         if idx < len(out_costs):
# #                             last_out = out_costs[idx]

# #                         # Include sub-group context in topic so it's searchable.
# #                         # e.g. "Medical Transportation — For total hip... — To/from COE"
# #                         if sub_ctx:
# #                             topic = f"{benefit} \u2014 {sub_ctx} \u2014 {service}"
# #                         else:
# #                             topic = f"{benefit} \u2014 {service}"

# #                         add(
# #                             topic,
# #                             {
# #                                 "event": benefit,
# #                                 "service": service,
# #                                 "in_network": last_in,
# #                                 "out_of_network": last_out,
# #                                 "limitations": svc_notes
# #                                 or limitations
# #                                 or "Data Not Found",
# #                             },
# #                         )

# #     # Parse the Summary of Your Costs page for plan-level cost-sharing data.
# #     # Wrapped in try/except — a failure here must never block the main benefit index.
# #     try:
# #         summary_entries = parse_summary_page(pdf_path)
# #         sub_index.extend(summary_entries)
# #         if summary_entries:
# #             print(f"[+] Summary page: {len(summary_entries)} plan-level entries added")
# #         else:
# #             print("[!] Summary page: no entries found — page structure may differ")
# #     except Exception as e:
# #         print(f"[!] Summary page parsing failed (benefit index unaffected): {e}")

# #     # Parse prose sections (coverage descriptions, provider rules, exclusions etc.)
# #     # as category="info" entries so members can ask "what is covered?" questions.
# #     # FULLY ISOLATED — any failure here never touches the cost table entries above.
# #     try:
# #         info_entries = parse_prose_sections(pdf_path)
# #         sub_index.extend(info_entries)
# #         if info_entries:
# #             print(f"[+] Prose sections: {len(info_entries)} info entries added")
# #         else:
# #             print("[!] Prose sections: no entries found")
# #     except Exception as e:
# #         print(f"[!] Prose parsing failed (cost table unaffected): {e}")

# #     with open(sub_index_path, "w", encoding="utf-8") as f:
# #         json_lib.dump(sub_index, f, indent=4)

# #     return sub_index
