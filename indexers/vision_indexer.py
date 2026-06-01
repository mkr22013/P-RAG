"""
vision_indexer.py — Index a Vision Plan booklet.

Two passes, both isolated so a failure in one never blocks the other:
    Pass 1 — Cost table  : the 2-row summary table (Vision Exams, Vision Hardware)
    Pass 2 — Prose pages : all benefit-relevant sections as category="info"

Output schema matches all other indexers:
    topic, category, benefit_category, content{event, service,
    in_network, out_of_network, limitations}, keywords
"""

import os, re, json as json_lib
import pdfplumber
import ollama
from dotenv import load_dotenv
from utility.utils import get_smart_keywords

load_dotenv()
CURRENT_YEAR_INT = 2025


# ── helpers ───────────────────────────────────────────────────────────────────


def clean(text):
    """Collapse whitespace."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ── document classification ───────────────────────────────────────────────────


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
            "Premera Dental Plan"
            
        5. type
        - Extract from explicit label OR plan name
        - Allowed values ONLY:
            HMO, PPO, EPO, HSA

        6. tier
        - Extract ONLY if explicitly present:
            Gold / Silver / Bronze / Platinum
        - Else return null

        7. variant
        - Extract ONLY if explicitly mentioned     
        - Return as string
        - If none found → return "Standard"

        8. network
        - Extract ONLY if explicitly mentioned       
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
            "plan": "Premera Dental Plan",
            "type": null,
            "tier": null,
            "variant": null,
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

        if not plan_type:
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
        print(f"[!] Vision classification failed: {e}")
        return None


# ── pass 1: cost table ────────────────────────────────────────────────────────


def parse_cost_table(pdf_path):
    """
    Find the page with "YOUR SHARE OF THE ALLOWED AMOUNT" and parse its table.

    The vision booklet has a simple 3-column table (page 7):
        col 0 : benefit name + limit note (e.g. "Vision Exams | Calendar year limit...")
        col 1 : in-network cost
        col 2 : out-of-network cost

    Benefit name  = first line of col 0
    Limitations   = any remaining lines (e.g. "Calendar year limit: one complete exam")
    """
    entries = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or "").upper()
            if "YOUR SHARE OF THE ALLOWED AMOUNT" not in text:
                continue

            page_num = page.page_number
            for table in page.extract_tables() or []:
                for row in table:
                    if len(row) < 3:
                        continue

                    c0 = clean(str(row[0] or ""))
                    c1 = clean(str(row[1] or ""))
                    c2 = clean(str(row[2] or ""))

                    # Skip header rows
                    if not c0:
                        continue
                    if c0.upper() in ("BENEFIT", "YOUR SHARE OF THE ALLOWED AMOUNT"):
                        continue
                    if "IN-NETWORK" in c0.upper():
                        continue
                    if not c1 and not c2:
                        continue

                    # Split benefit name from limit note
                    lines = [
                        l.strip() for l in str(row[0] or "").split("\n") if l.strip()
                    ]
                    benefit = lines[0] if lines else c0
                    limitation = (
                        clean(" ".join(lines[1:]))
                        if len(lines) > 1
                        else "Data Not Found"
                    )
                    if not limitation:
                        limitation = "Data Not Found"

                    entries.append(
                        {
                            "topic": f"{benefit} \u2014 Cost",
                            "category": "cost",
                            "benefit_category": "vision",
                            "content": {
                                "event": benefit,
                                "service": benefit,
                                "in_network": c1 or "Data Not Found",
                                "out_of_network": c2 or "Data Not Found",
                                "limitations": limitation,
                            },
                            "keywords": get_smart_keywords(
                                {
                                    "event": benefit,
                                    "service": benefit,
                                    "in_network": c1,
                                    "out_of_network": c2,
                                }
                            ),
                            "page_number": page_num,
                        }
                    )

    return entries


# ── pass 2: prose sections ────────────────────────────────────────────────────


def parse_prose_sections(pdf_path):
    """
    Scan all prose pages and extract every benefit-relevant section
    as a category="info" entry.

    Section detection: lines where >70% of characters are uppercase
    (e.g. "VISION EXAMS", "ALLOWED AMOUNT", "EXCLUSIONS AND LIMITATIONS").

    Administrative sections (enrollment, COBRA, appeals, ERISA, definitions)
    are skipped — members don't ask benefit questions about them.

    Content goes into the 'limitations' field to keep the schema consistent
    with cost entries and make it searchable.

    IMPORTANT: Always called inside try/except so any failure never
    affects the cost table entries.
    """
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

    def is_section_header(line):
        if len(line) < 4 or len(line) > 120:
            return False
        TABLE_WORDS = ("IN-NETWORK", "OUT-OF-NETWORK", "PROVIDERS", "YOUR SHARE")
        if any(w in line.upper() for w in TABLE_WORDS):
            return False
        upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
        return (
            upper_ratio > 0.7
            and not re.match(r"^\d", line)
            and not re.match(r"^[$%]", line)
        )

    # Collect all lines across all pages with page tracking
    all_lines = []  # list of (line, page_num)
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped:
                    all_lines.append((stripped, page.page_number))

    # Split into (header, content_lines, page_num) sections
    sections = []
    current_header = None
    current_content = []
    current_page = 0

    for line, pnum in all_lines:
        if is_section_header(line):
            if current_header and current_content:
                sections.append((current_header, current_content, current_page))
            current_header = line
            current_content = []
            current_page = pnum
        elif current_header:
            current_content.append(line)

    if current_header and current_content:
        sections.append((current_header, current_content, current_page))

    # Build info entries
    INLINE_SPLIT = re.compile(
        r"(?<!\. )(?<![•] )([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,5})\s+"
        r"(This benefit (?:covers|does not cover):)",
    )

    def split_inline_benefits(text, base_page):
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
        result = []
        for _, chunk, pg in parts:
            m2 = re.match(
                r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,5})\s+(This benefit)",
                chunk,
            )
            result.append((m2.group(1) if m2 else None, chunk, pg))
        return result

    entries = []
    seen_headers = set()

    for header, content_lines, section_page in sections:
        if is_admin(header):
            continue

        header_key = re.sub(r"\s+", " ", header.upper().strip())
        if header_key in seen_headers:
            continue
        seen_headers.add(header_key)

        content_text = " ".join(content_lines).strip()
        if len(content_text) < 50:
            continue

        for inline_name, chunk_text, chunk_page in split_inline_benefits(
            content_text, section_page
        ):
            event = (inline_name or header).strip().title()
            entries.append(
                {
                    "topic": f"{event} \u2014 Coverage Information",
                    "category": "info",
                    "benefit_category": "vision",
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


# ── main entry point ──────────────────────────────────────────────────────────


def generate_sub_index(sub_index_path, pdf_path):
    """
    Parse the Vision booklet and write a structured index file.

    Pass 1 — cost table  : Vision Exams and Vision Hardware cost entries
    Pass 2 — prose pages : info entries for coverage questions

    Both passes are isolated in try/except — a failure in either one
    never blocks the other or prevents the file from being written.
    The cost table is always the priority.
    """
    sub_index = []

    # ── Pass 1: cost table ─────────────────────────────────────────────────────
    # This is the most important pass — always runs first, always isolated.
    try:
        cost_entries = parse_cost_table(pdf_path)
        sub_index.extend(cost_entries)
        if cost_entries:
            print(f"[+] Vision cost table: {len(cost_entries)} entries added")
        else:
            print("[!] Vision cost table: no entries found")
    except Exception as e:
        print(f"[!] Vision cost table failed: {e}")

    # ── Pass 2: prose sections ─────────────────────────────────────────────────
    # Fully isolated — any failure here never affects cost entries above.
    try:
        info_entries = parse_prose_sections(pdf_path)
        sub_index.extend(info_entries)
        if info_entries:
            print(f"[+] Vision prose sections: {len(info_entries)} info entries added")
        else:
            print("[!] Vision prose sections: no entries found")
    except Exception as e:
        print(f"[!] Vision prose parsing failed (cost table unaffected): {e}")

    # ── Write output ───────────────────────────────────────────────────────────
    # Always writes whatever sub_index has, even if both passes above failed.
    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# =============================Previous working code before page number addition============================
# # """
# # vision_indexer.py — Index a Vision Plan booklet.

# # Two passes, both isolated so a failure in one never blocks the other:
# #     Pass 1 — Cost table  : the 2-row summary table (Vision Exams, Vision Hardware)
# #     Pass 2 — Prose pages : all benefit-relevant sections as category="info"

# # Output schema matches all other indexers:
# #     topic, category, benefit_category, content{event, service,
# #     in_network, out_of_network, limitations}, keywords
# # """

# # import os, re, json as json_lib
# # import pdfplumber
# # import ollama
# # from dotenv import load_dotenv
# # from utility.utils import get_smart_keywords

# # load_dotenv()
# # CURRENT_YEAR_INT = 2025


# # # ── helpers ───────────────────────────────────────────────────────────────────


# # def clean(text):
# #     """Collapse whitespace."""
# #     return re.sub(r"\s+", " ", str(text or "")).strip()


# # # ── document classification ───────────────────────────────────────────────────


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
# #             "Premera Dental Plan"

# #         5. type
# #         - Extract from explicit label OR plan name
# #         - Allowed values ONLY:
# #             HMO, PPO, EPO, HSA

# #         6. tier
# #         - Extract ONLY if explicitly present:
# #             Gold / Silver / Bronze / Platinum
# #         - Else return null

# #         7. variant
# #         - Extract ONLY if explicitly mentioned
# #         - Return as string
# #         - If none found → return "Standard"

# #         8. network
# #         - Extract ONLY if explicitly mentioned
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
# #             "plan": "Premera Dental Plan",
# #             "type": null,
# #             "tier": null,
# #             "variant": null,
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

# #         if not plan_type:
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
# #         print(f"[!] Vision classification failed: {e}")
# #         return None


# # # ── pass 1: cost table ────────────────────────────────────────────────────────


# # def parse_cost_table(pdf_path):
# #     """
# #     Find the page with "YOUR SHARE OF THE ALLOWED AMOUNT" and parse its table.

# #     The vision booklet has a simple 3-column table (page 7):
# #         col 0 : benefit name + limit note (e.g. "Vision Exams | Calendar year limit...")
# #         col 1 : in-network cost
# #         col 2 : out-of-network cost

# #     Benefit name  = first line of col 0
# #     Limitations   = any remaining lines (e.g. "Calendar year limit: one complete exam")
# #     """
# #     entries = []

# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             text = (page.extract_text() or "").upper()
# #             if "YOUR SHARE OF THE ALLOWED AMOUNT" not in text:
# #                 continue

# #             for table in page.extract_tables() or []:
# #                 for row in table:
# #                     if len(row) < 3:
# #                         continue

# #                     c0 = clean(str(row[0] or ""))
# #                     c1 = clean(str(row[1] or ""))
# #                     c2 = clean(str(row[2] or ""))

# #                     # Skip header rows
# #                     if not c0:
# #                         continue
# #                     if c0.upper() in ("BENEFIT", "YOUR SHARE OF THE ALLOWED AMOUNT"):
# #                         continue
# #                     if "IN-NETWORK" in c0.upper():
# #                         continue
# #                     if not c1 and not c2:
# #                         continue

# #                     # Split benefit name from limit note
# #                     lines = [
# #                         l.strip() for l in str(row[0] or "").split("\n") if l.strip()
# #                     ]
# #                     benefit = lines[0] if lines else c0
# #                     limitation = (
# #                         clean(" ".join(lines[1:]))
# #                         if len(lines) > 1
# #                         else "Data Not Found"
# #                     )
# #                     if not limitation:
# #                         limitation = "Data Not Found"

# #                     entries.append(
# #                         {
# #                             "topic": f"{benefit} \u2014 Cost",
# #                             "category": "cost",
# #                             "benefit_category": "vision",
# #                             "content": {
# #                                 "event": benefit,
# #                                 "service": benefit,
# #                                 "in_network": c1 or "Data Not Found",
# #                                 "out_of_network": c2 or "Data Not Found",
# #                                 "limitations": limitation,
# #                             },
# #                             "keywords": get_smart_keywords(
# #                                 {
# #                                     "event": benefit,
# #                                     "service": benefit,
# #                                     "in_network": c1,
# #                                     "out_of_network": c2,
# #                                 }
# #                             ),
# #                         }
# #                     )

# #     return entries


# # # ── pass 2: prose sections ────────────────────────────────────────────────────


# # def parse_prose_sections(pdf_path):
# #     """
# #     Scan all prose pages and extract every benefit-relevant section
# #     as a category="info" entry.

# #     Section detection: lines where >70% of characters are uppercase
# #     (e.g. "VISION EXAMS", "ALLOWED AMOUNT", "EXCLUSIONS AND LIMITATIONS").

# #     Administrative sections (enrollment, COBRA, appeals, ERISA, definitions)
# #     are skipped — members don't ask benefit questions about them.

# #     Content goes into the 'limitations' field to keep the schema consistent
# #     with cost entries and make it searchable.

# #     IMPORTANT: Always called inside try/except so any failure never
# #     affects the cost table entries.
# #     """
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

# #     def is_section_header(line):
# #         if len(line) < 4 or len(line) > 120:
# #             return False
# #         TABLE_WORDS = ("IN-NETWORK", "OUT-OF-NETWORK", "PROVIDERS", "YOUR SHARE")
# #         if any(w in line.upper() for w in TABLE_WORDS):
# #             return False
# #         upper_ratio = sum(1 for c in line if c.isupper()) / max(len(line), 1)
# #         return (
# #             upper_ratio > 0.7
# #             and not re.match(r"^\d", line)
# #             and not re.match(r"^[$%]", line)
# #         )

# #     # Collect all lines across all pages
# #     all_lines = []
# #     with pdfplumber.open(pdf_path) as pdf:
# #         for page in pdf.pages:
# #             text = page.extract_text() or ""
# #             for line in text.split("\n"):
# #                 stripped = line.strip()
# #                 if stripped:
# #                     all_lines.append(stripped)

# #     # Split into (header, content_lines) sections
# #     sections = []
# #     current_header = None
# #     current_content = []

# #     for line in all_lines:
# #         if is_section_header(line):
# #             if current_header and current_content:
# #                 sections.append((current_header, current_content))
# #             current_header = line
# #             current_content = []
# #         elif current_header:
# #             current_content.append(line)

# #     if current_header and current_content:
# #         sections.append((current_header, current_content))

# #     # Build info entries
# #     entries = []
# #     seen_headers = set()

# #     for header, content_lines in sections:
# #         if is_admin(header):
# #             continue

# #         header_key = re.sub(r"\s+", " ", header.upper().strip())
# #         if header_key in seen_headers:
# #             continue
# #         seen_headers.add(header_key)

# #         content_text = " ".join(content_lines).strip()
# #         if len(content_text) < 50:
# #             continue

# #         event = header.strip().title()

# #         entries.append(
# #             {
# #                 "topic": f"{event} \u2014 Coverage Information",
# #                 "category": "info",
# #                 "benefit_category": "vision",
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


# # # ── main entry point ──────────────────────────────────────────────────────────


# # def generate_sub_index(sub_index_path, pdf_path):
# #     """
# #     Parse the Vision booklet and write a structured index file.

# #     Pass 1 — cost table  : Vision Exams and Vision Hardware cost entries
# #     Pass 2 — prose pages : info entries for coverage questions

# #     Both passes are isolated in try/except — a failure in either one
# #     never blocks the other or prevents the file from being written.
# #     The cost table is always the priority.
# #     """
# #     sub_index = []

# #     # ── Pass 1: cost table ─────────────────────────────────────────────────────
# #     # This is the most important pass — always runs first, always isolated.
# #     try:
# #         cost_entries = parse_cost_table(pdf_path)
# #         sub_index.extend(cost_entries)
# #         if cost_entries:
# #             print(f"[+] Vision cost table: {len(cost_entries)} entries added")
# #         else:
# #             print("[!] Vision cost table: no entries found")
# #     except Exception as e:
# #         print(f"[!] Vision cost table failed: {e}")

# #     # ── Pass 2: prose sections ─────────────────────────────────────────────────
# #     # Fully isolated — any failure here never affects cost entries above.
# #     try:
# #         info_entries = parse_prose_sections(pdf_path)
# #         sub_index.extend(info_entries)
# #         if info_entries:
# #             print(f"[+] Vision prose sections: {len(info_entries)} info entries added")
# #         else:
# #             print("[!] Vision prose sections: no entries found")
# #     except Exception as e:
# #         print(f"[!] Vision prose parsing failed (cost table unaffected): {e}")

# #     # ── Write output ───────────────────────────────────────────────────────────
# #     # Always writes whatever sub_index has, even if both passes above failed.
# #     with open(sub_index_path, "w", encoding="utf-8") as f:
# #         json_lib.dump(sub_index, f, indent=4)

# #     return sub_index
