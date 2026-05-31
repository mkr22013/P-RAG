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
    r"All\s+charges\s+in\s+excess\s+of\s+\$[\d,]+|"
    r"\d+%\s+of\s+(?:the\s+)?allowable\s+charge|"
    r"\d+%\s+of\s+(?:the\s+)?allowed\s+amount|"
    r"\$[\d,]+\s+(?:per|lifetime|annual)\s+(?:copay|maximum|benefit))",
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
    r"^Premera\s+Employee|^Willamette\s+Dental\s+Plan$|"
    r"^TEMPOROMANDIBULAR|"
    r"^All\s+other\s+services\s+are\s+not\s+covered|"
    r"^The\s+following\s+services\s+are\s+not\s+covered)",
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


def is_willamette_format(pdf_path):
    """
    Returns True if the PDF contains D-code procedure lines (Willamette format).
    Returns False for class-based coinsurance plans (Premera format).
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:20]:
            text = page.extract_text() or ""
            if re.search(r"^D\d{4}\s+\w", text, re.M):
                return True
    return False


def parse_premera_dental_costs(pdf_path, sub_index, seen):
    """
    Parse the Premera Dental class-based coinsurance plan directly from the PDF.

    Structure (confirmed from pdfplumber debug output):
        - Page footers: "N Premera Dental Plan / January 1, YYYY / WVB13 V-N"
        - Coinsurance rates: bullet lines "• Class X - Name .... N%"
        - Class headers:     plain lines  "Class I/II/III - Name" (no bullet)
        - Procedure bullets: "• procedure text" with continuation lines
        - Group headers:     bullets ending with ":" (skipped as services)
        - Tricky case:       lines ending with " •" (inline next-bullet start)
        - Plan limits:       prose sections with headers on their own lines
        - Orthodontia:       "ORTHODONTIA" / "Covered Services And Supplies" sections
    """
    FOOTER_RE = re.compile(
        r"^\d+\s+Premera\s+Dental\s+Plan$"
        r"|^January\s+\d+,\s+\d{4}$"
        r"|^WVB13\s+V-\d+$",
        re.I,
    )

    all_lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text() or "").split("\n"):
                line = raw.strip()
                if line and not FOOTER_RE.match(line):
                    all_lines.append(line)

    full_text = "\n".join(all_lines)

    # ── Dollar limits ─────────────────────────────────────────────────────────
    def _amt(pattern, default):
        m = re.search(pattern, full_text, re.I)
        return f"${m.group(1)}" if m else default

    deductible = _amt(
        r"individual\s+calendar\s+year\s+deductible\s+amount\s+is\s+\$(\d[\d,]*)", "$25"
    )
    annual_max = _amt(r"in\s+a\s+calendar\s+year\s+is\s+\$(\d[\d,]*)", "$2,000")
    ortho_max = _amt(r"lifetime\s+maximum\s+of\s+\$(\d[\d,]*)", "$2,000")
    family_ded = "$" + _amt(r"total\s+equals\s+\$(\d[\d,]*)", "75").lstrip("$").rstrip(
        ","
    )

    # ── Coinsurance rates from "• Class X .... N%" lines ─────────────────────
    class_rates = {}
    for line in all_lines:
        m = re.match(
            r"^•\s+(Class\s+[IVX]+\s*-\s*[^.]+?)\s*\.{2,}\s*(\d+%)", line, re.I
        )
        if m:
            class_rates[m.group(1).strip().lower()] = (
                m.group(1).strip(),
                m.group(2).strip(),
            )

    if not class_rates:
        class_rates = {
            "class i - diagnostic and preventive services": (
                "Class I - Diagnostic And Preventive Services",
                "0%",
            ),
            "class ii - basic services": ("Class II - Basic Services", "20%"),
            "class iii - major services": ("Class III - Major Services", "20%"),
        }

    def resolve_class(header):
        key = header.strip().lower()
        for k, (cname, rate) in class_rates.items():
            if k == key:
                cost = (
                    "Covered in full (0% coinsurance, no deductible)"
                    if rate == "0%"
                    else f"{rate} coinsurance after calendar year deductible"
                )
                return cname, cost
        return header.strip(), "See plan booklet"

    # ── Plan limit descriptions from prose sections ───────────────────────────
    def section_desc(header, stop_list):
        collecting, parts = False, []
        for line in all_lines:
            if not collecting:
                if re.match(r"^" + re.escape(header) + r"\s*$", line, re.I):
                    collecting = True
            else:
                if any(
                    re.match(r"^" + re.escape(h) + r"\s*$", line, re.I)
                    for h in stop_list
                ):
                    break
                parts.append(line)
        return " ".join(parts).strip()

    ded_desc = section_desc(
        "Calendar Year Deductible",
        ["Family Dental Deductible", "Dental Benefit Maximum", "Coinsurance"],
    )
    family_desc = section_desc(
        "Family Dental Deductible",
        ["Coinsurance", "Dental Benefit Maximum", "Network Providers"],
    )
    max_desc = section_desc(
        "Dental Benefit Maximum",
        ["Network Providers", "Non-Network Providers", "Coinsurance"],
    )

    # ── add() and keyword helpers ─────────────────────────────────────────────
    out_net = "Benefits based on allowed amount"

    def add(event, service, in_net, limit, page_num: int = 0):
        content_dict = {
            "event": event,
            "service": service,
            "in_network": in_net,
            "out_of_network": (
                out_net
                if "50%" not in in_net
                else "50% coinsurance based on allowed amount"
            ),
            "limitations": limit or "No additional limitations.",
        }
        key = json_lib.dumps(content_dict, sort_keys=True)
        if key not in seen:
            seen.add(key)
            sub_index.append(
                {
                    "topic": f"{event} \u2014 {service}",
                    "category": "cost",
                    "benefit_category": "dental",
                    "content": content_dict,
                    "keywords": _dental_keywords(event, service),
                    "page_number": page_num,
                }
            )

    def _dental_keywords(event, service):
        kws = ["dental"]
        event_lower = event.lower()
        for cls in ["class iii", "class ii", "class i", "orthodontia", "plan limits"]:
            if re.search(r"\b" + re.escape(cls) + r"\b", event_lower):
                kws.append(cls)
                break
        for word in re.split(r"[\s/,()\-]+", service.lower()):
            if len(word) >= 5 and word not in kws:
                kws.append(word)
        return kws[:10]

    # ── Service/limitation splitter ───────────────────────────────────────────
    LIMIT_RE = re.compile(
        r",?\s+(?:"
        r"(?:is|are)\s+limited\s+to"
        r"|limited\s+to"
        r"|may\s+be\s+limited"
        r"|for\s+members\s+under\s+the\s+age\s+of"
        r"|is\s+covered\s+for\s+members"
        r"|subject\s+to\s+review"
        r"|when\s+dentally\s+necessary\.?"
        r"|as\s+a\s+follow-up\s+to"
        r"|including\s+repair,\s+reline"
        r"|covered\s+when\s+services\s+are\s+done"
        r"|covered\s+in\s+the\s+same\s+quadrant"
        r"|in\s+a\s+dental\s+care\s+provider"
        r")|\.\s+We\s+require\s+a\s+written",
        re.I,
    )

    def split_svc_lim(text):
        text = re.sub(r"\.\s+See\s+the\s+definition.*$", ".", text, flags=re.I).strip()
        m = LIMIT_RE.search(text)
        if m:
            svc = text[: m.start()].strip().rstrip(",.:;")
            lim = text[m.start() :].strip().lstrip(",. ")
        else:
            svc = text.rstrip(".,;:").strip()
            lim = "No additional limitations."
        # Strip trailing verb artifacts left by the split (e.g. "Periodontal Surgery is")
        svc = re.sub(
            r"\s+(?:is|are|is covered|are covered)\s*$", "", svc, flags=re.I
        ).strip()
        svc = svc.rstrip(".,;:").strip()
        return svc, lim

    JUNK_RE = re.compile(
        r"^(Were\s+(started|completed)\b"
        r"|For\s+root\s+canals\s+and\s+retreatment.{0,40}service\s+(start|completion)\s+date"
        r"|service\s+(start|completion)\s+date\s+is"
        r"|The\s+plan\s+will\s+cover\s+Class"
        r"|Once\s+every\s+\d"
        r"|The\s+replacement\s+or\s+addition\s+of\s+teeth)",
        re.I,
    )

    def process_bullet(parts, class_name, class_cost):
        if not parts or not class_name:
            return
        text = " ".join(parts).strip()
        if len(text) < 4:
            return
        # Skip legal/administrative text that is not a procedure
        if JUNK_RE.match(text):
            return
        # Skip group headers (end with ":")
        if text.rstrip().endswith(":"):
            return
        # Handle inline sub-bullets: "parent text: • sub1 text"
        if ": •" in text:
            for sub in text.split(": •")[1:]:
                sub = sub.strip()
                if sub:
                    svc, lim = split_svc_lim(sub)
                    if len(svc.strip().rstrip(".,;:")) > 4:
                        add(class_name, svc.strip().rstrip(".,;:"), class_cost, lim)
            return
        svc, lim = split_svc_lim(text)
        svc = svc.strip().rstrip(".,;:")
        if len(svc) > 4:
            add(class_name, svc, class_cost, lim)

    # ── Parse Description of Covered Services ─────────────────────────────────
    CLASS_HDR_RE = re.compile(r"^Class\s+(?:I{1,3}|IV)\s*-\s*.+$", re.I)
    SECTION_END_RE = re.compile(
        r"^(DENTAL\s+CARE\s+SERVICES\s+FOR\s+INJURIES"
        r"|ORTHODONTIA"
        r"|EXCLUSIONS\s+AND\s+LIMITATIONS"
        r"|HIGH\s+RISK\s+CONDITIONS)",
        re.I,
    )

    desc_start = next(
        (
            i + 1
            for i, l in enumerate(all_lines)
            if re.match(r"^DESCRIPTION\s+OF\s+COVERED\s+SERVICES$", l.strip(), re.I)
            and not re.search(r"\.{3,}|\d+$", l)
        ),  # exclude TOC dotted lines
        None,
    )
    if desc_start is None:
        print("[DIAG] ERROR: 'DESCRIPTION OF COVERED SERVICES' section not found")
        return

    desc_end = next(
        (
            i
            for i in range(desc_start, len(all_lines))
            if SECTION_END_RE.match(all_lines[i])
        ),
        len(all_lines),
    )

    # Pre-process: split lines ending with " •" (inline bullet artifact)
    desc_lines = []
    for line in all_lines[desc_start:desc_end]:
        if line.endswith(" •"):
            desc_lines.append(line[:-1].strip())
            desc_lines.append("• ")  # empty bullet; continuation follows
        elif line == "•":
            desc_lines.append("• ")
        else:
            desc_lines.append(line)

    current_class = None
    current_cost = None
    bullet_parts = []

    for line in desc_lines:
        if CLASS_HDR_RE.match(line):
            process_bullet(bullet_parts, current_class, current_cost)
            bullet_parts = []
            current_class, current_cost = resolve_class(line)
        elif line.startswith("• "):
            process_bullet(bullet_parts, current_class, current_cost)
            new_text = line[2:].strip()
            bullet_parts = [new_text] if new_text else []
        elif bullet_parts is not None:
            # If a continuation line ends with ":" it's a group header that
            # bled across a page boundary (e.g. "The plan will cover Class III
            # ...not covered when they:"). Flush the current bullet cleanly
            # and discard this line — it is not part of the procedure.
            if line.rstrip().endswith(":"):
                process_bullet(bullet_parts, current_class, current_cost)
                bullet_parts = []
            else:
                bullet_parts.append(line)

    process_bullet(bullet_parts, current_class, current_cost)

    # ── Orthodontia ───────────────────────────────────────────────────────────
    ortho_rate_m = re.search(r"(\d+)%\s+of\s+the\s+allowable\s+charge", full_text, re.I)
    ortho_rate = f"{ortho_rate_m.group(1)}%" if ortho_rate_m else "50%"
    ortho_cost = f"{ortho_rate} coinsurance"
    ortho_lim = (
        f"Lifetime maximum: {ortho_max} per member. "
        f"Class I/II/III deductibles do not apply."
    )

    # Parse services under "Covered Services And Supplies"
    in_ortho_svc = False
    o_parts = []

    for line in all_lines:
        if re.match(r"^Covered\s+Services\s+And\s+Supplies$", line, re.I):
            in_ortho_svc = True
            continue
        if in_ortho_svc:
            if re.match(
                r"^(Benefits|We\s+reserve|Limitations|TEMPOROMANDIBULAR)", line, re.I
            ):
                break
            if line.startswith("• "):
                if o_parts:
                    svc = " ".join(o_parts).strip().rstrip(".,;:")
                    if len(svc) > 4:
                        add("Orthodontia", svc, ortho_cost, ortho_lim)
                o_parts = [line[2:].strip()]
            elif o_parts:
                o_parts.append(line)

    if o_parts:
        svc = " ".join(o_parts).strip().rstrip(".,;:")
        if len(svc) > 4:
            add("Orthodontia", svc, ortho_cost, ortho_lim)

    if not any(e["content"]["event"] == "Orthodontia" for e in sub_index):
        add(
            "Orthodontia",
            "Orthodontic Treatment",
            ortho_cost,
            f"Lifetime maximum: {ortho_max} per member. "
            f"Requires diagnosis of handicapping malocclusion. "
            f"Class I/II/III deductibles do not apply.",
        )

    # ── Plan Limits ───────────────────────────────────────────────────────────
    PL = "Plan Limits"
    add(
        PL,
        "Calendar Year Deductible",
        deductible,
        ded_desc
        or f"Applies to Class II and III only. Class I has no deductible. "
        f"Family deductible: {family_ded}.",
    )
    add(
        PL,
        "Annual Benefit Maximum",
        annual_max,
        max_desc or "Maximum dental benefits per member per calendar year.",
    )
    add(
        PL,
        "Family Dental Deductible",
        family_ded,
        family_desc
        or f"When combined family deductible reaches {family_ded}, "
        f"individual deductible is met for all enrolled members for the year.",
    )
    add(
        PL,
        "Orthodontia Lifetime Maximum",
        ortho_max,
        f"Lifetime maximum per member for orthodontic treatment. "
        f"Applies across all prior contracts or programs issued by this plan.",
    )


def parse_prose_sections(pdf_path):
    """
    Extract all benefit-relevant prose sections as category="info" entries.
    Same logic used by medical_indexer and vision_indexer.
    Admin sections (COBRA, appeals, ERISA, definitions) are skipped.
    Always called inside try/except.
    """
    ADMIN = {
        "WHEN DOES COVERAGE BEGIN",
        "WHEN WILL MY COVERAGE END",
        "ENROLLMENT",
        "SPECIAL ENROLLMENT",
        "OPEN ENROLLMENT",
        "CHANGES IN COVERAGE",
        "PLAN TRANSFERS",
        "HOW DO I CONTINUE COVERAGE",
        "CONTINUED ELIGIBILITY",
        "LEAVE OF ABSENCE",
        "LABOR DISPUTE",
        "CONTINUATION UNDER USERRA",
        "COBRA",
        "HOW DO I FILE A CLAIM",
        "COMPLAINTS AND APPEALS",
        "WHAT YOU CAN APPEAL",
        "APPEAL LEVELS",
        "HOW TO SUBMIT AN APPEAL",
        "WHAT IF YOU HAVE ONGOING CARE",
        "PRIVACY",
        "ERISA",
        "DEFINITIONS",
        "WHERE TO SEND",
        "CONTACT US",
        "TABLE OF CONTENTS",
        "INTRODUCTION",
        "RIGHT TO AND PAYMENT",
        "RIGHT OF RECOVERY",
        "ERISA PLAN DESCRIPTION",
        "SUBROGATION AND REIMBURSEMENT",
        "PRIMARY AND SECONDARY RULES",
        "WHAT IF I HAVE OTHER COVERAGE",
        "COORDINATING BENEFITS",
        "WHO IS ELIGIBLE",
        "SUBSCRIBER ELIGIBILITY",
        "DEPENDENT ELIGIBILITY",
        "EVENTS THAT END COVERAGE",
        "PLAN TERMINATION",
        "OTHER INFORMATION ABOUT THIS PLAN",
        "UNINSURED AND UNDERINSURED",
        "NOTICE OF",
        "MEMBER COOPERATION",
        "INTENTIONALLY FALSE",
        "CONFORMITY WITH",
        "EVIDENCE OF DENTAL NECESSITY",
        "VENUE",
    }

    def is_admin(header):
        h = re.sub(r"\s+", " ", header.upper().strip())
        return any(a in h for a in ADMIN)

    def is_header(line):
        if len(line) < 4 or len(line) > 120:
            return False
        TABLE_WORDS = ("IN-NETWORK", "OUT-OF-NETWORK", "PROVIDERS", "YOUR SHARE")
        if any(w in line.upper() for w in TABLE_WORDS):
            return False
        upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
        return (
            upper_ratio > 0.7
            and not re.match(r"^(D\d{4}|\d)", line)
            and not re.match(r"^[$%]", line)
        )

    all_lines = []  # list of (line, page_num)
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").split("\n"):
                if line.strip():
                    all_lines.append((line.strip(), page.page_number))

    sections = []  # list of (header, content_lines, page_num)
    cur_hdr, cur_content, cur_page = None, [], 0
    for line, pnum in all_lines:
        if is_header(line):
            if cur_hdr and cur_content:
                sections.append((cur_hdr, cur_content, cur_page))
            cur_hdr, cur_content, cur_page = line, [], pnum
        elif cur_hdr:
            cur_content.append(line)
    if cur_hdr and cur_content:
        sections.append((cur_hdr, cur_content, cur_page))

    entries, seen_hdrs = [], set()
    for header, content_lines, section_page in sections:
        if is_admin(header):
            continue
        key = re.sub(r"\s+", " ", header.upper().strip())
        if key in seen_hdrs:
            continue
        seen_hdrs.add(key)
        content_text = " ".join(content_lines).strip()
        if len(content_text) < 50:
            continue
        event = header.strip().title()
        entries.append(
            {
                "topic": f"{event} \u2014 Coverage Information",
                "category": "info",
                "benefit_category": "dental",
                "content": {
                    "event": event,
                    "service": "Coverage Information",
                    "in_network": "Data Not Found",
                    "out_of_network": "Data Not Found",
                    "limitations": content_text,
                },
                "keywords": get_smart_keywords(
                    {"event": event, "limitations": content_text}
                ),
                "page_number": section_page,
            }
        )
    return entries


def generate_sub_index(sub_index_path, pdf_path):
    """
    Parse the Dental booklet and write a structured index file.

    Auto-detects Willamette (D-code schedule) vs Premera (class-based) format.
    Pass 1 — cost entries  : procedures or class coinsurance rates
    Pass 2 — info entries  : prose coverage descriptions and exclusions
    Both passes isolated in try/except — cost entries always written first.
    """
    import pdfplumber

    sub_index = []
    seen = set()

    willamette = is_willamette_format(pdf_path)
    print(f"[+] Dental format: {'WILLAMETTE' if willamette else 'PREMERA'}")

    # ── Pass 1: cost entries ───────────────────────────────────────────────────
    try:
        if willamette:
            # ── WILLAMETTE: original D-code parser (unchanged) ─────────────────

            def add(code, procedure, category, copay, page_num: int = 0):
                topic = f"{category} \u2014 {procedure}" if category else procedure
                content = {
                    "event": category,
                    "service": f"{code} {procedure}" if code else procedure,
                    "in_network": copay,
                    "out_of_network": "Not covered",
                    "limitations": "Data Not Found",
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
                            "page_number": page_num,
                        }
                    )

            # Extract office visit copay amounts from the schedule header
            OFFICE_VISIT_RE = re.compile(
                r"^(General|Specialist)\s+Office\s+Visit\s+Copayment:\s+(\$[\d,]+)",
                re.I,
            )
            office_visit_amounts = {}

            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    if "Office Visit Copayment" not in text:
                        continue
                    for line in text.split("\n"):
                        m = OFFICE_VISIT_RE.match(n(line))
                        if m:
                            office_visit_amounts[m.group(1).lower()] = m.group(2)
                            add(
                                "",
                                f"{m.group(1).capitalize()} Office Visit",
                                "Office Visit Copayments",
                                m.group(2),
                            )
                    break

            def resolve_copay(raw_copay):
                if re.search(
                    r"covered\s+under\s+office\s+visit\s+copay", raw_copay, re.I
                ):
                    g = office_visit_amounts.get("general", "$15")
                    s = office_visit_amounts.get("specialist", "$30")
                    return f"{g} (General Visit) / {s} (Specialist Visit)"
                return raw_copay

            current_page = 0
            with pdfplumber.open(pdf_path) as pdf:
                current_section = ""
                pending_code = ""
                pending_proc = ""
                all_lines = []  # list of (line, page_num)
                for page in pdf.pages:
                    for ln in (page.extract_text() or "").split("\n"):
                        all_lines.append((ln, page.page_number))

                for raw_line, current_page in all_lines:
                    line = n(raw_line)
                    if not line:
                        continue
                    if SKIP_RE.match(line):
                        if pending_code and pending_proc:
                            add(
                                pending_code,
                                n(pending_proc),
                                current_section,
                                "",
                                current_page,
                            )
                            pending_code = pending_proc = ""
                        continue
                    if SECTION_HDR_RE.match(line) and not CODE_RE.match(line):
                        if pending_code and pending_proc:
                            add(
                                pending_code,
                                n(pending_proc),
                                current_section,
                                "",
                                current_page,
                            )
                            pending_code = pending_proc = ""
                        current_section = line
                        continue
                    code_match = CODE_RE.match(line)
                    if code_match:
                        if pending_code and pending_proc:
                            add(
                                pending_code,
                                n(pending_proc),
                                current_section,
                                "",
                                current_page,
                            )
                        code = code_match.group(1).strip()
                        rest = code_match.group(2).strip()
                        cm = COPAY_RE.search(rest)
                        if cm:
                            add(
                                code,
                                rest[: cm.start()].strip(),
                                current_section,
                                resolve_copay(cm.group(0).strip()),
                                current_page,
                            )
                            pending_code = pending_proc = ""
                        else:
                            pending_code = code
                            pending_proc = rest
                    elif pending_code:
                        combined = pending_proc + " " + line
                        cm = COPAY_RE.search(combined)
                        if cm:
                            add(
                                pending_code,
                                combined[: cm.start()].strip(),
                                current_section,
                                resolve_copay(cm.group(0).strip()),
                                current_page,
                            )
                            pending_code = pending_proc = ""
                        else:
                            pending_proc = combined

                if pending_code and pending_proc:
                    add(
                        pending_code, n(pending_proc), current_section, "", current_page
                    )

            print(f"[+] Willamette cost entries: {len(sub_index)} procedures indexed")

        else:
            # ── PREMERA: class-based coinsurance (new, simple) ─────────────────
            parse_premera_dental_costs(pdf_path, sub_index, seen)

    except Exception as e:
        print(f"[!] Dental cost parsing failed: {e}")

    # ── Pass 2: prose sections as info entries ─────────────────────────────────
    # Fully isolated — any failure never affects cost entries above.
    try:
        info_entries = parse_prose_sections(pdf_path)
        sub_index.extend(info_entries)
        if info_entries:
            print(f"[+] Dental prose sections: {len(info_entries)} info entries added")
        else:
            print("[!] Dental prose sections: no entries found")
    except Exception as e:
        print(f"[!] Dental prose parsing failed (cost entries unaffected): {e}")

    # ── Pass 2b: ensure TMJ keyword in any temporomandibular entry ──────────────
    # get_smart_keywords only extracts 7+ char words so "tmj" (4 chars) is never
    # added automatically. Patch existing entries and create one if missing.
    if willamette:
        try:
            tmj_patched = False
            for e in sub_index:
                e_text = (
                    e.get("content", {}).get("event", "") + " " + e.get("topic", "")
                ).lower()
                if "temporomandibular" in e_text:
                    kws = e.setdefault("keywords", [])
                    if "tmj" not in kws:
                        kws.append("tmj")
                    if "temporomandibular" not in kws:
                        kws.append("temporomandibular")
                    tmj_patched = True
            if tmj_patched:
                print("[+] TMJ entry: 'tmj' keyword ensured")
            else:
                with pdfplumber.open(pdf_path) as _pdf:
                    _full = " ".join((p.extract_text() or "") for p in _pdf.pages)
                m = re.search(
                    r"TEMPOROMANDIBULAR\s+JOINT\s+DISORDER\s+TREATMENT\s*(.+?)(?=[A-Z]{4,}\s|$)",
                    _full,
                    re.DOTALL | re.I,
                )
                if m:
                    tmj_text = re.sub(r"\s+", " ", m.group(1)).strip()[:2000]
                    if len(tmj_text) > 50:
                        tmj_kws = get_smart_keywords(
                            {
                                "event": "Temporomandibular Joint Disorder Treatment",
                                "limitations": tmj_text,
                            }
                        )
                        for kw in ["tmj", "temporomandibular"]:
                            if kw not in tmj_kws:
                                tmj_kws.append(kw)
                        sub_index.append(
                            {
                                "topic": "Temporomandibular Joint Disorder Treatment — Coverage Information",
                                "category": "info",
                                "benefit_category": "dental",
                                "content": {
                                    "event": "Temporomandibular Joint Disorder Treatment",
                                    "service": "Coverage Information",
                                    "in_network": "Data Not Found",
                                    "out_of_network": "Data Not Found",
                                    "limitations": tmj_text,
                                },
                                "keywords": tmj_kws,
                                "page_number": 0,
                            }
                        )
                        print("[+] Willamette TMJ INFO entry created from raw PDF")
        except Exception as e:
            print(f"[!] TMJ keyword patch failed: {e}")

    # Always writes whatever sub_index has, even if both passes failed
    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# ==================================Previous working code before page number addition==================================
# # """
# # Dental Benefits booklet indexer.

# # Structure: Schedule of Covered Services — flat rows with:
# #     - D-code (e.g. D0120)
# #     - Procedure description
# #     - Copayment ("Covered In Full", "$25 copay", "Covered Under Office Visit Copay", etc.)

# # Uses pdfplumber text extraction — the schedule is plain text, not a proper table.
# # Each D-code line is parsed with its copay value; wrapped continuation lines are
# # assembled before emitting an entry.
# # """

# # import os
# # import re
# # import json as json_lib
# # import ollama
# # import pdfplumber

# # from datetime import datetime
# # from dotenv import load_dotenv
# # from utility.utils import get_smart_keywords

# # load_dotenv()

# # CURRENT_YEAR_INT = datetime.now().year

# # # Regex to detect the start of a new procedure line (D-code at start)
# # CODE_RE = re.compile(r"^(D\d{4}(?:\s*&\s*D\d{4})?)\s+(.+)", re.I)

# # # Copay patterns that mark the END of a procedure line
# # COPAY_RE = re.compile(
# #     r"(Covered\s+In\s+Full[\w\s,$]*|"
# #     r"Covered\s+Under\s+Office\s+Visit\s+Copay|"
# #     r"\$[\d,]+(?:\.\d+)?\s+(?:Copay|copay)[^|]*|"
# #     r"All\s+charges\s+in\s+excess\s+of\s+\$[\d,]+|"
# #     r"\d+%\s+of\s+(?:the\s+)?allowable\s+charge|"
# #     r"\d+%\s+of\s+(?:the\s+)?allowed\s+amount|"
# #     r"\$[\d,]+\s+(?:per|lifetime|annual)\s+(?:copay|maximum|benefit))",
# #     re.I,
# # )

# # # Section headers — lines with no D-code that label a service group
# # SECTION_HDR_RE = re.compile(
# #     r"^(Diagnostic|Restorative|Endodontics|Periodontics|Prosthodontics|"
# #     r"Oral\s+Surgery|Adjunctive|Crowns|Implants|Orthodontic|"
# #     r"Out\s+of\s+Area)",
# #     re.I,
# # )

# # # Lines to skip entirely
# # SKIP_RE = re.compile(
# #     r"^(\d+\s+Willamette|January\s+\d|^\d{7}$|^Code\s+Procedure|"
# #     r"^Provider\s+Network|^Office\s+Visit\s+Copay|^General\s+Office|"
# #     r"^Specialist\s+Office|SCHEDULE\s+OF|WHAT\s+ARE\s+MY|"
# #     r"^Premera\s+Employee|^Willamette\s+Dental\s+Plan$|"
# #     r"^TEMPOROMANDIBULAR|"
# #     r"^All\s+other\s+services\s+are\s+not\s+covered|"
# #     r"^The\s+following\s+services\s+are\s+not\s+covered)",
# #     re.I,
# # )


# # def n(v):
# #     """Normalise whitespace."""
# #     return re.sub(r"\s+", " ", str(v or "")).strip()


# # def classify_document(pdf_path):
# #     """
# #     Read the first few pages of the Dental booklet PDF and extract:
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
# #         print(f"[!] Dental classification failed: {e}")
# #         return None


# # def is_willamette_format(pdf_path):
# #     """
# #     Returns True if the PDF contains D-code procedure lines (Willamette format).
# #     Returns False for class-based coinsurance plans (Premera format).
# #     """
# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages[:20]:
# #             text = page.extract_text() or ""
# #             if re.search(r"^D\d{4}\s+\w", text, re.M):
# #                 return True
# #     return False


# # def parse_premera_dental_costs(pdf_path, sub_index, seen):
# #     """
# #     Parse the Premera Dental class-based coinsurance plan directly from the PDF.

# #     Structure (confirmed from pdfplumber debug output):
# #         - Page footers: "N Premera Dental Plan / January 1, YYYY / WVB13 V-N"
# #         - Coinsurance rates: bullet lines "• Class X - Name .... N%"
# #         - Class headers:     plain lines  "Class I/II/III - Name" (no bullet)
# #         - Procedure bullets: "• procedure text" with continuation lines
# #         - Group headers:     bullets ending with ":" (skipped as services)
# #         - Tricky case:       lines ending with " •" (inline next-bullet start)
# #         - Plan limits:       prose sections with headers on their own lines
# #         - Orthodontia:       "ORTHODONTIA" / "Covered Services And Supplies" sections
# #     """
# #     FOOTER_RE = re.compile(
# #         r"^\d+\s+Premera\s+Dental\s+Plan$"
# #         r"|^January\s+\d+,\s+\d{4}$"
# #         r"|^WVB13\s+V-\d+$",
# #         re.I,
# #     )

# #     all_lines = []
# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             for raw in (page.extract_text() or "").split("\n"):
# #                 line = raw.strip()
# #                 if line and not FOOTER_RE.match(line):
# #                     all_lines.append(line)

# #     full_text = "\n".join(all_lines)

# #     # ── Dollar limits ─────────────────────────────────────────────────────────
# #     def _amt(pattern, default):
# #         m = re.search(pattern, full_text, re.I)
# #         return f"${m.group(1)}" if m else default

# #     deductible = _amt(
# #         r"individual\s+calendar\s+year\s+deductible\s+amount\s+is\s+\$(\d[\d,]*)", "$25"
# #     )
# #     annual_max = _amt(r"in\s+a\s+calendar\s+year\s+is\s+\$(\d[\d,]*)", "$2,000")
# #     ortho_max = _amt(r"lifetime\s+maximum\s+of\s+\$(\d[\d,]*)", "$2,000")
# #     family_ded = "$" + _amt(r"total\s+equals\s+\$(\d[\d,]*)", "75").lstrip("$").rstrip(
# #         ","
# #     )

# #     # ── Coinsurance rates from "• Class X .... N%" lines ─────────────────────
# #     class_rates = {}
# #     for line in all_lines:
# #         m = re.match(
# #             r"^•\s+(Class\s+[IVX]+\s*-\s*[^.]+?)\s*\.{2,}\s*(\d+%)", line, re.I
# #         )
# #         if m:
# #             class_rates[m.group(1).strip().lower()] = (
# #                 m.group(1).strip(),
# #                 m.group(2).strip(),
# #             )

# #     if not class_rates:
# #         class_rates = {
# #             "class i - diagnostic and preventive services": (
# #                 "Class I - Diagnostic And Preventive Services",
# #                 "0%",
# #             ),
# #             "class ii - basic services": ("Class II - Basic Services", "20%"),
# #             "class iii - major services": ("Class III - Major Services", "20%"),
# #         }

# #     def resolve_class(header):
# #         key = header.strip().lower()
# #         for k, (cname, rate) in class_rates.items():
# #             if k == key:
# #                 cost = (
# #                     "Covered in full (0% coinsurance, no deductible)"
# #                     if rate == "0%"
# #                     else f"{rate} coinsurance after calendar year deductible"
# #                 )
# #                 return cname, cost
# #         return header.strip(), "See plan booklet"

# #     # ── Plan limit descriptions from prose sections ───────────────────────────
# #     def section_desc(header, stop_list):
# #         collecting, parts = False, []
# #         for line in all_lines:
# #             if not collecting:
# #                 if re.match(r"^" + re.escape(header) + r"\s*$", line, re.I):
# #                     collecting = True
# #             else:
# #                 if any(
# #                     re.match(r"^" + re.escape(h) + r"\s*$", line, re.I)
# #                     for h in stop_list
# #                 ):
# #                     break
# #                 parts.append(line)
# #         return " ".join(parts).strip()

# #     ded_desc = section_desc(
# #         "Calendar Year Deductible",
# #         ["Family Dental Deductible", "Dental Benefit Maximum", "Coinsurance"],
# #     )
# #     family_desc = section_desc(
# #         "Family Dental Deductible",
# #         ["Coinsurance", "Dental Benefit Maximum", "Network Providers"],
# #     )
# #     max_desc = section_desc(
# #         "Dental Benefit Maximum",
# #         ["Network Providers", "Non-Network Providers", "Coinsurance"],
# #     )

# #     # ── add() and keyword helpers ─────────────────────────────────────────────
# #     out_net = "Benefits based on allowed amount"

# #     def add(event, service, in_net, limit):
# #         content_dict = {
# #             "event": event,
# #             "service": service,
# #             "in_network": in_net,
# #             "out_of_network": (
# #                 out_net
# #                 if "50%" not in in_net
# #                 else "50% coinsurance based on allowed amount"
# #             ),
# #             "limitations": limit or "No additional limitations.",
# #         }
# #         key = json_lib.dumps(content_dict, sort_keys=True)
# #         if key not in seen:
# #             seen.add(key)
# #             sub_index.append(
# #                 {
# #                     "topic": f"{event} \u2014 {service}",
# #                     "category": "cost",
# #                     "benefit_category": "dental",
# #                     "content": content_dict,
# #                     "keywords": _dental_keywords(event, service),
# #                 }
# #             )

# #     def _dental_keywords(event, service):
# #         kws = ["dental"]
# #         event_lower = event.lower()
# #         for cls in ["class iii", "class ii", "class i", "orthodontia", "plan limits"]:
# #             if re.search(r"\b" + re.escape(cls) + r"\b", event_lower):
# #                 kws.append(cls)
# #                 break
# #         for word in re.split(r"[\s/,()\-]+", service.lower()):
# #             if len(word) >= 5 and word not in kws:
# #                 kws.append(word)
# #         return kws[:10]

# #     # ── Service/limitation splitter ───────────────────────────────────────────
# #     LIMIT_RE = re.compile(
# #         r",?\s+(?:"
# #         r"(?:is|are)\s+limited\s+to"
# #         r"|limited\s+to"
# #         r"|may\s+be\s+limited"
# #         r"|for\s+members\s+under\s+the\s+age\s+of"
# #         r"|is\s+covered\s+for\s+members"
# #         r"|subject\s+to\s+review"
# #         r"|when\s+dentally\s+necessary\.?"
# #         r"|as\s+a\s+follow-up\s+to"
# #         r"|including\s+repair,\s+reline"
# #         r"|covered\s+when\s+services\s+are\s+done"
# #         r"|covered\s+in\s+the\s+same\s+quadrant"
# #         r"|in\s+a\s+dental\s+care\s+provider"
# #         r")|\.\s+We\s+require\s+a\s+written",
# #         re.I,
# #     )

# #     def split_svc_lim(text):
# #         text = re.sub(r"\.\s+See\s+the\s+definition.*$", ".", text, flags=re.I).strip()
# #         m = LIMIT_RE.search(text)
# #         if m:
# #             svc = text[: m.start()].strip().rstrip(",.:;")
# #             lim = text[m.start() :].strip().lstrip(",. ")
# #         else:
# #             svc = text.rstrip(".,;:").strip()
# #             lim = "No additional limitations."
# #         # Strip trailing verb artifacts left by the split (e.g. "Periodontal Surgery is")
# #         svc = re.sub(
# #             r"\s+(?:is|are|is covered|are covered)\s*$", "", svc, flags=re.I
# #         ).strip()
# #         svc = svc.rstrip(".,;:").strip()
# #         return svc, lim

# #     JUNK_RE = re.compile(
# #         r"^(Were\s+(started|completed)\b"
# #         r"|For\s+root\s+canals\s+and\s+retreatment.{0,40}service\s+(start|completion)\s+date"
# #         r"|service\s+(start|completion)\s+date\s+is"
# #         r"|The\s+plan\s+will\s+cover\s+Class"
# #         r"|Once\s+every\s+\d"
# #         r"|The\s+replacement\s+or\s+addition\s+of\s+teeth)",
# #         re.I,
# #     )

# #     def process_bullet(parts, class_name, class_cost):
# #         if not parts or not class_name:
# #             return
# #         text = " ".join(parts).strip()
# #         if len(text) < 4:
# #             return
# #         # Skip legal/administrative text that is not a procedure
# #         if JUNK_RE.match(text):
# #             return
# #         # Skip group headers (end with ":")
# #         if text.rstrip().endswith(":"):
# #             return
# #         # Handle inline sub-bullets: "parent text: • sub1 text"
# #         if ": •" in text:
# #             for sub in text.split(": •")[1:]:
# #                 sub = sub.strip()
# #                 if sub:
# #                     svc, lim = split_svc_lim(sub)
# #                     if len(svc.strip().rstrip(".,;:")) > 4:
# #                         add(class_name, svc.strip().rstrip(".,;:"), class_cost, lim)
# #             return
# #         svc, lim = split_svc_lim(text)
# #         svc = svc.strip().rstrip(".,;:")
# #         if len(svc) > 4:
# #             add(class_name, svc, class_cost, lim)

# #     # ── Parse Description of Covered Services ─────────────────────────────────
# #     CLASS_HDR_RE = re.compile(r"^Class\s+(?:I{1,3}|IV)\s*-\s*.+$", re.I)
# #     SECTION_END_RE = re.compile(
# #         r"^(DENTAL\s+CARE\s+SERVICES\s+FOR\s+INJURIES"
# #         r"|ORTHODONTIA"
# #         r"|EXCLUSIONS\s+AND\s+LIMITATIONS"
# #         r"|HIGH\s+RISK\s+CONDITIONS)",
# #         re.I,
# #     )

# #     desc_start = next(
# #         (
# #             i + 1
# #             for i, l in enumerate(all_lines)
# #             if re.match(r"^DESCRIPTION\s+OF\s+COVERED\s+SERVICES$", l.strip(), re.I)
# #             and not re.search(r"\.{3,}|\d+$", l)
# #         ),  # exclude TOC dotted lines
# #         None,
# #     )
# #     if desc_start is None:
# #         print("[DIAG] ERROR: 'DESCRIPTION OF COVERED SERVICES' section not found")
# #         return

# #     desc_end = next(
# #         (
# #             i
# #             for i in range(desc_start, len(all_lines))
# #             if SECTION_END_RE.match(all_lines[i])
# #         ),
# #         len(all_lines),
# #     )

# #     # Pre-process: split lines ending with " •" (inline bullet artifact)
# #     desc_lines = []
# #     for line in all_lines[desc_start:desc_end]:
# #         if line.endswith(" •"):
# #             desc_lines.append(line[:-1].strip())
# #             desc_lines.append("• ")  # empty bullet; continuation follows
# #         elif line == "•":
# #             desc_lines.append("• ")
# #         else:
# #             desc_lines.append(line)

# #     current_class = None
# #     current_cost = None
# #     bullet_parts = []

# #     for line in desc_lines:
# #         if CLASS_HDR_RE.match(line):
# #             process_bullet(bullet_parts, current_class, current_cost)
# #             bullet_parts = []
# #             current_class, current_cost = resolve_class(line)
# #         elif line.startswith("• "):
# #             process_bullet(bullet_parts, current_class, current_cost)
# #             new_text = line[2:].strip()
# #             bullet_parts = [new_text] if new_text else []
# #         elif bullet_parts is not None:
# #             # If a continuation line ends with ":" it's a group header that
# #             # bled across a page boundary (e.g. "The plan will cover Class III
# #             # ...not covered when they:"). Flush the current bullet cleanly
# #             # and discard this line — it is not part of the procedure.
# #             if line.rstrip().endswith(":"):
# #                 process_bullet(bullet_parts, current_class, current_cost)
# #                 bullet_parts = []
# #             else:
# #                 bullet_parts.append(line)

# #     process_bullet(bullet_parts, current_class, current_cost)

# #     # ── Orthodontia ───────────────────────────────────────────────────────────
# #     ortho_rate_m = re.search(r"(\d+)%\s+of\s+the\s+allowable\s+charge", full_text, re.I)
# #     ortho_rate = f"{ortho_rate_m.group(1)}%" if ortho_rate_m else "50%"
# #     ortho_cost = f"{ortho_rate} coinsurance"
# #     ortho_lim = (
# #         f"Lifetime maximum: {ortho_max} per member. "
# #         f"Class I/II/III deductibles do not apply."
# #     )

# #     # Parse services under "Covered Services And Supplies"
# #     in_ortho_svc = False
# #     o_parts = []

# #     for line in all_lines:
# #         if re.match(r"^Covered\s+Services\s+And\s+Supplies$", line, re.I):
# #             in_ortho_svc = True
# #             continue
# #         if in_ortho_svc:
# #             if re.match(
# #                 r"^(Benefits|We\s+reserve|Limitations|TEMPOROMANDIBULAR)", line, re.I
# #             ):
# #                 break
# #             if line.startswith("• "):
# #                 if o_parts:
# #                     svc = " ".join(o_parts).strip().rstrip(".,;:")
# #                     if len(svc) > 4:
# #                         add("Orthodontia", svc, ortho_cost, ortho_lim)
# #                 o_parts = [line[2:].strip()]
# #             elif o_parts:
# #                 o_parts.append(line)

# #     if o_parts:
# #         svc = " ".join(o_parts).strip().rstrip(".,;:")
# #         if len(svc) > 4:
# #             add("Orthodontia", svc, ortho_cost, ortho_lim)

# #     if not any(e["content"]["event"] == "Orthodontia" for e in sub_index):
# #         add(
# #             "Orthodontia",
# #             "Orthodontic Treatment",
# #             ortho_cost,
# #             f"Lifetime maximum: {ortho_max} per member. "
# #             f"Requires diagnosis of handicapping malocclusion. "
# #             f"Class I/II/III deductibles do not apply.",
# #         )

# #     # ── Plan Limits ───────────────────────────────────────────────────────────
# #     PL = "Plan Limits"
# #     add(
# #         PL,
# #         "Calendar Year Deductible",
# #         deductible,
# #         ded_desc
# #         or f"Applies to Class II and III only. Class I has no deductible. "
# #         f"Family deductible: {family_ded}.",
# #     )
# #     add(
# #         PL,
# #         "Annual Benefit Maximum",
# #         annual_max,
# #         max_desc or "Maximum dental benefits per member per calendar year.",
# #     )
# #     add(
# #         PL,
# #         "Family Dental Deductible",
# #         family_ded,
# #         family_desc
# #         or f"When combined family deductible reaches {family_ded}, "
# #         f"individual deductible is met for all enrolled members for the year.",
# #     )
# #     add(
# #         PL,
# #         "Orthodontia Lifetime Maximum",
# #         ortho_max,
# #         f"Lifetime maximum per member for orthodontic treatment. "
# #         f"Applies across all prior contracts or programs issued by this plan.",
# #     )


# # def parse_prose_sections(pdf_path):
# #     """
# #     Extract all benefit-relevant prose sections as category="info" entries.
# #     Same logic used by medical_indexer and vision_indexer.
# #     Admin sections (COBRA, appeals, ERISA, definitions) are skipped.
# #     Always called inside try/except.
# #     """
# #     ADMIN = {
# #         "WHEN DOES COVERAGE BEGIN",
# #         "WHEN WILL MY COVERAGE END",
# #         "ENROLLMENT",
# #         "SPECIAL ENROLLMENT",
# #         "OPEN ENROLLMENT",
# #         "CHANGES IN COVERAGE",
# #         "PLAN TRANSFERS",
# #         "HOW DO I CONTINUE COVERAGE",
# #         "CONTINUED ELIGIBILITY",
# #         "LEAVE OF ABSENCE",
# #         "LABOR DISPUTE",
# #         "CONTINUATION UNDER USERRA",
# #         "COBRA",
# #         "HOW DO I FILE A CLAIM",
# #         "COMPLAINTS AND APPEALS",
# #         "WHAT YOU CAN APPEAL",
# #         "APPEAL LEVELS",
# #         "HOW TO SUBMIT AN APPEAL",
# #         "WHAT IF YOU HAVE ONGOING CARE",
# #         "PRIVACY",
# #         "ERISA",
# #         "DEFINITIONS",
# #         "WHERE TO SEND",
# #         "CONTACT US",
# #         "TABLE OF CONTENTS",
# #         "INTRODUCTION",
# #         "RIGHT TO AND PAYMENT",
# #         "RIGHT OF RECOVERY",
# #         "ERISA PLAN DESCRIPTION",
# #         "SUBROGATION AND REIMBURSEMENT",
# #         "PRIMARY AND SECONDARY RULES",
# #         "WHAT IF I HAVE OTHER COVERAGE",
# #         "COORDINATING BENEFITS",
# #         "WHO IS ELIGIBLE",
# #         "SUBSCRIBER ELIGIBILITY",
# #         "DEPENDENT ELIGIBILITY",
# #         "EVENTS THAT END COVERAGE",
# #         "PLAN TERMINATION",
# #         "OTHER INFORMATION ABOUT THIS PLAN",
# #         "UNINSURED AND UNDERINSURED",
# #         "NOTICE OF",
# #         "MEMBER COOPERATION",
# #         "INTENTIONALLY FALSE",
# #         "CONFORMITY WITH",
# #         "EVIDENCE OF DENTAL NECESSITY",
# #         "VENUE",
# #     }

# #     def is_admin(header):
# #         h = re.sub(r"\s+", " ", header.upper().strip())
# #         return any(a in h for a in ADMIN)

# #     def is_header(line):
# #         if len(line) < 4 or len(line) > 120:
# #             return False
# #         TABLE_WORDS = ("IN-NETWORK", "OUT-OF-NETWORK", "PROVIDERS", "YOUR SHARE")
# #         if any(w in line.upper() for w in TABLE_WORDS):
# #             return False
# #         upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
# #         return (
# #             upper_ratio > 0.7
# #             and not re.match(r"^(D\d{4}|\d)", line)
# #             and not re.match(r"^[$%]", line)
# #         )

# #     all_lines = []
# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             for line in (page.extract_text() or "").split("\n"):
# #                 if line.strip():
# #                     all_lines.append(line.strip())

# #     sections = []
# #     cur_hdr, cur_content = None, []
# #     for line in all_lines:
# #         if is_header(line):
# #             if cur_hdr and cur_content:
# #                 sections.append((cur_hdr, cur_content))
# #             cur_hdr, cur_content = line, []
# #         elif cur_hdr:
# #             cur_content.append(line)
# #     if cur_hdr and cur_content:
# #         sections.append((cur_hdr, cur_content))

# #     entries, seen_hdrs = [], set()
# #     for header, content_lines in sections:
# #         if is_admin(header):
# #             continue
# #         key = re.sub(r"\s+", " ", header.upper().strip())
# #         if key in seen_hdrs:
# #             continue
# #         seen_hdrs.add(key)
# #         content_text = " ".join(content_lines).strip()
# #         if len(content_text) < 50:
# #             continue
# #         event = header.strip().title()
# #         entries.append(
# #             {
# #                 "topic": f"{event} \u2014 Coverage Information",
# #                 "category": "info",
# #                 "benefit_category": "dental",
# #                 "content": {
# #                     "event": event,
# #                     "service": "Coverage Information",
# #                     "in_network": "Data Not Found",
# #                     "out_of_network": "Data Not Found",
# #                     "limitations": content_text,
# #                 },
# #                 "keywords": get_smart_keywords(
# #                     {"event": event, "limitations": content_text}
# #                 ),
# #             }
# #         )
# #     return entries


# # def generate_sub_index(sub_index_path, pdf_path):
# #     """
# #     Parse the Dental booklet and write a structured index file.

# #     Auto-detects Willamette (D-code schedule) vs Premera (class-based) format.
# #     Pass 1 — cost entries  : procedures or class coinsurance rates
# #     Pass 2 — info entries  : prose coverage descriptions and exclusions
# #     Both passes isolated in try/except — cost entries always written first.
# #     """
# #     import pdfplumber

# #     sub_index = []
# #     seen = set()

# #     willamette = is_willamette_format(pdf_path)
# #     print(f"[+] Dental format: {'WILLAMETTE' if willamette else 'PREMERA'}")

# #     # ── Pass 1: cost entries ───────────────────────────────────────────────────
# #     try:
# #         if willamette:
# #             # ── WILLAMETTE: original D-code parser (unchanged) ─────────────────

# #             def add(code, procedure, category, copay):
# #                 topic = f"{category} \u2014 {procedure}" if category else procedure
# #                 content = {
# #                     "event": category,
# #                     "service": f"{code} {procedure}" if code else procedure,
# #                     "in_network": copay,
# #                     "out_of_network": "Not covered",
# #                     "limitations": "Data Not Found",
# #                 }
# #                 key = json_lib.dumps(content, sort_keys=True)
# #                 if key not in seen:
# #                     seen.add(key)
# #                     sub_index.append(
# #                         {
# #                             "topic": topic,
# #                             "category": "cost",
# #                             "benefit_category": "dental",
# #                             "content": content,
# #                             "keywords": get_smart_keywords(content),
# #                         }
# #                     )

# #             # Extract office visit copay amounts from the schedule header
# #             OFFICE_VISIT_RE = re.compile(
# #                 r"^(General|Specialist)\s+Office\s+Visit\s+Copayment:\s+(\$[\d,]+)",
# #                 re.I,
# #             )
# #             office_visit_amounts = {}

# #             with pdfplumber.open(pdf_path) as pdf:
# #                 for page in pdf.pages:
# #                     text = page.extract_text() or ""
# #                     if "Office Visit Copayment" not in text:
# #                         continue
# #                     for line in text.split("\n"):
# #                         m = OFFICE_VISIT_RE.match(n(line))
# #                         if m:
# #                             office_visit_amounts[m.group(1).lower()] = m.group(2)
# #                             add(
# #                                 "",
# #                                 f"{m.group(1).capitalize()} Office Visit",
# #                                 "Office Visit Copayments",
# #                                 m.group(2),
# #                             )
# #                     break

# #             def resolve_copay(raw_copay):
# #                 if re.search(
# #                     r"covered\s+under\s+office\s+visit\s+copay", raw_copay, re.I
# #                 ):
# #                     g = office_visit_amounts.get("general", "$15")
# #                     s = office_visit_amounts.get("specialist", "$30")
# #                     return f"{g} (General Visit) / {s} (Specialist Visit)"
# #                 return raw_copay

# #             with pdfplumber.open(pdf_path) as pdf:
# #                 current_section = ""
# #                 pending_code = ""
# #                 pending_proc = ""
# #                 all_lines = []
# #                 for page in pdf.pages:
# #                     all_lines.extend((page.extract_text() or "").split("\n"))

# #                 for raw_line in all_lines:
# #                     line = n(raw_line)
# #                     if not line:
# #                         continue
# #                     if SKIP_RE.match(line):
# #                         if pending_code and pending_proc:
# #                             add(pending_code, n(pending_proc), current_section, "")
# #                             pending_code = pending_proc = ""
# #                         continue
# #                     if SECTION_HDR_RE.match(line) and not CODE_RE.match(line):
# #                         if pending_code and pending_proc:
# #                             add(pending_code, n(pending_proc), current_section, "")
# #                             pending_code = pending_proc = ""
# #                         current_section = line
# #                         continue
# #                     code_match = CODE_RE.match(line)
# #                     if code_match:
# #                         if pending_code and pending_proc:
# #                             add(pending_code, n(pending_proc), current_section, "")
# #                         code = code_match.group(1).strip()
# #                         rest = code_match.group(2).strip()
# #                         cm = COPAY_RE.search(rest)
# #                         if cm:
# #                             add(
# #                                 code,
# #                                 rest[: cm.start()].strip(),
# #                                 current_section,
# #                                 resolve_copay(cm.group(0).strip()),
# #                             )
# #                             pending_code = pending_proc = ""
# #                         else:
# #                             pending_code = code
# #                             pending_proc = rest
# #                     elif pending_code:
# #                         combined = pending_proc + " " + line
# #                         cm = COPAY_RE.search(combined)
# #                         if cm:
# #                             add(
# #                                 pending_code,
# #                                 combined[: cm.start()].strip(),
# #                                 current_section,
# #                                 resolve_copay(cm.group(0).strip()),
# #                             )
# #                             pending_code = pending_proc = ""
# #                         else:
# #                             pending_proc = combined

# #                 if pending_code and pending_proc:
# #                     add(pending_code, n(pending_proc), current_section, "")

# #             print(f"[+] Willamette cost entries: {len(sub_index)} procedures indexed")

# #         else:
# #             # ── PREMERA: class-based coinsurance (new, simple) ─────────────────
# #             parse_premera_dental_costs(pdf_path, sub_index, seen)

# #     except Exception as e:
# #         print(f"[!] Dental cost parsing failed: {e}")

# #     # ── Pass 2: prose sections as info entries ─────────────────────────────────
# #     # Fully isolated — any failure never affects cost entries above.
# #     try:
# #         info_entries = parse_prose_sections(pdf_path)
# #         sub_index.extend(info_entries)
# #         if info_entries:
# #             print(f"[+] Dental prose sections: {len(info_entries)} info entries added")
# #         else:
# #             print("[!] Dental prose sections: no entries found")
# #     except Exception as e:
# #         print(f"[!] Dental prose parsing failed (cost entries unaffected): {e}")

# #     # ── Pass 2b: ensure TMJ keyword in any temporomandibular entry ──────────────
# #     # get_smart_keywords only extracts 7+ char words so "tmj" (4 chars) is never
# #     # added automatically. Patch existing entries and create one if missing.
# #     if willamette:
# #         try:
# #             tmj_patched = False
# #             for e in sub_index:
# #                 e_text = (
# #                     e.get("content", {}).get("event", "") + " " + e.get("topic", "")
# #                 ).lower()
# #                 if "temporomandibular" in e_text:
# #                     kws = e.setdefault("keywords", [])
# #                     if "tmj" not in kws:
# #                         kws.append("tmj")
# #                     if "temporomandibular" not in kws:
# #                         kws.append("temporomandibular")
# #                     tmj_patched = True
# #             if tmj_patched:
# #                 print("[+] TMJ entry: 'tmj' keyword ensured")
# #             else:
# #                 with pdfplumber.open(pdf_path) as _pdf:
# #                     _full = " ".join((p.extract_text() or "") for p in _pdf.pages)
# #                 m = re.search(
# #                     r"TEMPOROMANDIBULAR\s+JOINT\s+DISORDER\s+TREATMENT\s*(.+?)(?=[A-Z]{4,}\s|$)",
# #                     _full,
# #                     re.DOTALL | re.I,
# #                 )
# #                 if m:
# #                     tmj_text = re.sub(r"\s+", " ", m.group(1)).strip()[:2000]
# #                     if len(tmj_text) > 50:
# #                         tmj_kws = get_smart_keywords(
# #                             {
# #                                 "event": "Temporomandibular Joint Disorder Treatment",
# #                                 "limitations": tmj_text,
# #                             }
# #                         )
# #                         for kw in ["tmj", "temporomandibular"]:
# #                             if kw not in tmj_kws:
# #                                 tmj_kws.append(kw)
# #                         sub_index.append(
# #                             {
# #                                 "topic": "Temporomandibular Joint Disorder Treatment — Coverage Information",
# #                                 "category": "info",
# #                                 "benefit_category": "dental",
# #                                 "content": {
# #                                     "event": "Temporomandibular Joint Disorder Treatment",
# #                                     "service": "Coverage Information",
# #                                     "in_network": "Data Not Found",
# #                                     "out_of_network": "Data Not Found",
# #                                     "limitations": tmj_text,
# #                                 },
# #                                 "keywords": tmj_kws,
# #                             }
# #                         )
# #                         print("[+] Willamette TMJ INFO entry created from raw PDF")
# #         except Exception as e:
# #             print(f"[!] TMJ keyword patch failed: {e}")

# #     # Always writes whatever sub_index has, even if both passes failed
# #     with open(sub_index_path, "w", encoding="utf-8") as f:
# #         json_lib.dump(sub_index, f, indent=4)

# #     return sub_index
