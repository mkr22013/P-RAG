"""
Main indexer — entry point for all plan document types.

Folder structure determines which indexer is used:
    docs/
        sbc/          → sbc_indexer   (docling + markdown parsing)
        medical/      → medical_indexer (pdfplumber + bold detection)
        dental/       → dental_indexer  (add when ready)

To add a new booklet type:
    1. Create <type>_indexer.py with classify_document() and generate_sub_index()
    2. Add its folder name to BOOKLET_STRATEGIES below
    3. Import the module
"""

import os
import sqlite3
import json as json_lib
import re

import sbc_indexer
import medical_indexer
import dental_indexer

from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DOC_BASE_DIR = os.path.abspath("./docs")
INDEX_OUTPUT_DIR = "./indices"
DB_PATH = os.path.join(os.path.dirname(__file__), "p_insurance_index.db")
LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
CURRENT_YEAR_INT = datetime.now().year

# Maps folder name (lowercase) → (classify_fn, generate_index_fn)
# Add new booklet types here without touching build_all().
BOOKLET_STRATEGIES = {
    "sbc": (sbc_indexer.classify_document, sbc_indexer.generate_sub_index),
    "medical": (medical_indexer.classify_document, medical_indexer.generate_sub_index),
    "dental": (dental_indexer.classify_document, dental_indexer.generate_sub_index),
}


def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Master Index for Fast Routing with unique Plan Identity
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS master_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER, 
            plan_category TEXT,     -- SBC, MEDICAL, DENTAL, VISION 
            plan_type TEXT,    -- PPO, HMO, HSA
            plan_tier TEXT,    -- Gold, Silver, Bronze
            product_line TEXT, -- e.g., 'EPO HSA Preferred', 'Cascade Care'
            variant TEXT,      -- e.g., 'American Indian 300%', 'CSR 94%', 'Standard'
            network TEXT,      -- e.g., 'Individual Signature', 'Sherwood HMO'
            pdf_path TEXT UNIQUE, 
            sub_index_path TEXT
        )
    """
    )

    # Performance: This 5-way Composite Index is the "Identity Lock"
    # It ensures lookups for specific variants are instant and deterministic.
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_plan_identity 
        ON master_index (year, plan_category, plan_type, plan_tier, product_line, variant, network)
    """
    )

    # 2. FTS5 Virtual Table for Fuzzy Search
    # We include product_line and variant so users can search for "94%" or "HSA"
    cursor.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index 
        USING fts5(
            year, 
            tier, 
            type, 
            product_line,
            variant,
            network, 
            topic, 
            benefit_category,
            content, 
            keywords, 
            tokenize='porter'
        )
    """
    )

    conn.commit()
    conn.close()
    print("[*] Database Schema Updated: Ready for Premera multi-plan indexing.")


def build_all():
    setup_db()
    os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    doc_path = os.path.abspath(DOC_BASE_DIR)
    print(f"[*] Absolute Doc Path: {doc_path}")

    for root, _, files in os.walk(doc_path):
        parts = os.path.normpath(root).lower().split(os.sep)

        path_year = next((int(p) for p in parts if p.isdigit()), None)

        # Determine booklet type from the folder path.
        # Walk all path parts and return the first one that matches a known
        # booklet strategy — this works regardless of subfolder depth.
        # e.g. docs/2025/sbc/file.pdf  →  "sbc"
        #      docs/medical/plan_a/file.pdf  →  "medical"
        final_plan_category = next(
            (p for p in parts if p in BOOKLET_STRATEGIES),
            os.path.basename(root).lower(),  # fallback to immediate folder name
        )

        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.abspath(os.path.join(root, filename))

            try:
                print(f"[*] Processing: {filename}...")

                # ── STEP 1: Classify + Index — look up strategy by folder name ─
                strategy = BOOKLET_STRATEGIES.get(final_plan_category)
                if not strategy:
                    print(
                        f"[!] Unknown folder type '{final_plan_category}' — skipping {filename}"
                    )
                    continue

                classify_fn, generate_index_fn = strategy
                print(
                    f"[*] PICKING UP INDEXER BASED ON TYPE OF BOOKLET TO BE INDEXED : {generate_index_fn}"
                )
                plan_info = classify_fn(pdf_path)
                print(f"[*] LLM Classification for {filename}: {plan_info}")
                # --- STEP 2: HARDENED IDENTITY FALLBACKS ---

                # Folder path overrides LLM guess for Year and Type
                final_year = path_year or (
                    plan_info["year"]
                    if plan_info and plan_info.get("year")
                    else CURRENT_YEAR_INT
                )

                # -----------------------------
                # TYPE LOGIC (FINAL FIX)
                # -----------------------------
                llm_type = (
                    str(plan_info.get("type", "")).upper()
                    if plan_info and plan_info.get("type")
                    else ""
                )

                # Normalize LLM types
                VALID_TYPES = ["HMO", "PPO", "EPO", "HSA"]

                if llm_type not in VALID_TYPES:
                    final_type = ""
                else:
                    final_type = llm_type

                # -------------------------
                # Tier
                # -------------------------
                final_tier = (
                    plan_info["tier"] if plan_info and plan_info.get("tier") else "Gold"
                ).capitalize()
                print(f"[*] Tier Locked: {final_tier}")

                if final_tier.lower() == "none":
                    row_tier = ""
                else:
                    row_tier = final_tier
                # -------------------------
                # Product Line
                # -------------------------
                raw_prod = plan_info.get("product_line", "") if plan_info else ""

                if not raw_prod or str(raw_prod).lower() in [
                    "plan",
                    "standard",
                    "none",
                    "",
                ]:
                    final_product = (
                        filename.replace(".pdf", "").replace("_", " ").title()
                    )
                else:
                    final_product = str(raw_prod).strip()

                # -------------------------
                # Variant (DB SAFE)
                # -------------------------
                raw_vari = plan_info.get("variant", "") if plan_info else ""

                if not raw_vari or str(raw_vari).lower() in ["none", "standard", ""]:
                    final_variant = "Standard"
                else:
                    final_variant = str(raw_vari).strip()

                # -------------------------
                # Network (DB SAFE — NO HARDCODE)
                # -------------------------
                raw_net = plan_info.get("network") if plan_info else None

                if raw_net and str(raw_net).strip().lower() not in ["network", "none"]:
                    final_network = str(raw_net).strip()
                else:
                    final_network = "Unknown Network"

                print(
                    f"[*] Identity Locked: {final_year} | {final_product} | {final_network}"
                )

                # =====================================================
                # 🧼 STEP 3: CLEAN FILENAME (NO NOISE VALUES)
                # =====================================================

                INVALID_NETWORK_VALUES = {
                    "unknown network",
                    "standard network",
                    "network",
                    "",
                }

                INVALID_VARIANT_VALUES = {
                    "standard",
                    "none",
                    "",
                }

                # Clean product
                safe_prod = re.sub(r"\W+", "_", final_product.lower()).strip("_")

                # Clean network (ONLY if real)
                raw_net_clean = str(final_network).strip().lower()
                if raw_net_clean not in INVALID_NETWORK_VALUES:
                    safe_net = re.sub(r"\W+", "_", raw_net_clean).strip("_")
                else:
                    safe_net = None

                # Clean variant (ONLY if meaningful)
                raw_var_clean = str(final_variant).strip().lower()
                if raw_var_clean not in INVALID_VARIANT_VALUES:
                    safe_var = re.sub(r"\W+", "_", raw_var_clean).strip("_")
                else:
                    safe_var = None

                # -------------------------
                # Build filename dynamically
                # -------------------------
                filepath = [
                    str(final_year),
                    final_plan_category,
                    final_type,
                    row_tier,
                    safe_prod,
                ]

                if safe_var:
                    filepath.append(safe_var)

                if safe_net:
                    filepath.append(safe_net)

                unique_fn = "_".join(filepath) + ".json"

                sub_index_path = os.path.abspath(
                    os.path.join(INDEX_OUTPUT_DIR, unique_fn)
                )

                # ── STEP 4: Generate index using the booklet-specific parser ───
                sub_chunks = generate_index_fn(sub_index_path, pdf_path)

                # --- STEP 4: DB INSERTS (Surgical Siloing) ---
                conn.execute(
                    """
                    DELETE FROM search_index 
                    WHERE year = ? AND tier = ? AND type = ? 
                    AND product_line = ? AND variant = ? AND network = ?
                """,
                    (
                        final_year,
                        final_tier,
                        final_type,
                        final_product,
                        final_variant,
                        final_network,
                    ),
                )

                for chunk in sub_chunks:
                    raw_content = chunk.get("content", "")

                    if isinstance(raw_content, dict):
                        clean_content = json_lib.dumps(raw_content)
                    else:
                        clean_content = str(raw_content)

                    # ✅ SAFE KEYWORDS HANDLING (FIXED)
                    keywords_str = (
                        " ".join(chunk.get("keywords", []))
                        if chunk.get("keywords")
                        else ""
                    )

                    conn.execute(
                        """
                        INSERT INTO search_index 
                        (year, tier, type, product_line, variant, network, topic, benefit_category, content, keywords) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,?)
                    """,
                        (
                            final_year,
                            final_tier,
                            final_type,
                            final_product,
                            final_variant,
                            final_network,
                            chunk.get("topic", ""),  # ✅ extra safety
                            chunk.get("benefit_category", ""),
                            clean_content,
                            keywords_str,
                        ),
                    )

                conn.execute(
                    """
                    INSERT OR REPLACE INTO master_index 
                    (year, plan_category, plan_type, plan_tier, product_line, variant, network, pdf_path, sub_index_path) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        final_year,
                        final_plan_category,
                        final_type,
                        final_tier,
                        final_product,
                        final_variant,
                        final_network,
                        pdf_path,
                        sub_index_path,
                    ),
                )

                conn.commit()
                print(f"✅ SUCCESS: {filename} -> {unique_fn}")

            except Exception as e:
                print(f"❌ FAILED {filename}: {e}")

    conn.close()


if __name__ == "__main__":
    build_all()

# import os
# import sqlite3
# import json as json_lib
# import re
# import ollama

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
#     import pdfplumber
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
#                - Explicit label: "Plan Type: <VALUE>"
#                - Embedded in plan name: e.g. "Standard PPO Retiree Plan" → PPO,
#                  "HMO Gold Plan" → HMO, "EPO HSA Preferred" → EPO
#                Allowed values: HMO, PPO, EPO, HSA
#                If found multiple (e.g. "PPO HSA"), prefer the first.

#             3. tier: Extract from plan title (Gold, Silver, Bronze, Catastrophic)
#                If not present return null.

#             4. product_line: Full plan name as written (e.g. "Premera Employees Health Plan
#                Standard PPO Retiree Plan"). Remove year references.

#             5. variant: Extract modifiers like Standard, Retiree, Non-Grandfathered, CSR, etc.
#                Else return "Standard"

#             6. network: ONLY extract if explicitly labeled (e.g. "Network: Sherwood").
#                DO NOT infer. If not found return null.

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
#     prev_bullet   = None

#     for word in sorted(page_words, key=lambda w: (w["top"], w["x0"])):
#         if word["x0"] >= 200:           # only look at the benefit column
#             continue
#         if word["text"] in ("•", "\u2022"):
#             prev_bullet = word          # remember this bullet position
#         elif prev_bullet and abs(word["top"] - prev_bullet["top"]) < 6:
#             # This word is on the same line as the preceding bullet
#             fontname = word.get("fontname", "")
#             if "Bold" in fontname or "bold" in fontname:
#                 bold_starters.add(word["text"])
#             prev_bullet = None
#         else:
#             prev_bullet = None          # bullet was on a different line — reset

#     return bold_starters


# def parse_benefit_cell(cell_text, bold_starters):
#     """
#     Parse the raw text of a benefit cell (column 0) into:
#       - benefit_name : top-level benefit name  (e.g. 'Dental Injury and Facility Anesthesia')
#       - services     : ordered list of (service_name, subsection_name) tuples

#     How it works:
#       - Lines without a bullet are joined to form the benefit name.
#       - Bullet items whose first word is in bold_starters are sub-section
#         headers (branch nodes in the tree) — they set the current subsection.
#       - All other bullet items are leaf services that get indexed.
#       - Wrapped continuation lines (no bullet) are appended to the current item.
#       - Cross-reference text like '(See Dental Injury...)' is stripped.
#     """
#     CROSS_REF = re.compile(r"\s*\(See.*|^(benefit for|injury and facility anesthesia benefit)", re.I)

#     benefit    = ""
#     subsection = None
#     services   = []

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
#                 subsection = item           # bold bullet → sub-section header
#             else:
#                 services.append((n(item), subsection))   # regular bullet → leaf service
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
#       'Kinwell Clinics: $0 copay\ndeductible waived\nAll Other: $25 copay\n...'
#     Example output:
#       ['Kinwell Clinics: $0 copay deductible waived All Other: $25 copay ...']
#     """
#     COST_LINE_START = re.compile(
#         r"^(\$\d|\d+%|no charge|not covered|no cost|deductible|kinwell|all other)",
#         re.I
#     )
#     TIER_MARKER = re.compile(r"^(kinwell|all other)", re.I)

#     # First pass: split into raw cost strings on line-start patterns
#     raw_costs = []
#     current   = ""
#     for line in cell_text.split("\n"):
#         line = line.strip()
#         if COST_LINE_START.match(line):
#             if current:
#                 raw_costs.append(current)
#             current = line
#         elif current:
#             current += " " + line   # wrapped continuation of the current cost
#     if current:
#         raw_costs.append(current)

#     # Second pass: merge Kinwell / All Other tier lines into the previous entry
#     # so a single service that has multiple pricing tiers is one cost string
#     merged = []
#     for cost in raw_costs:
#         if merged and TIER_MARKER.match(cost):
#             merged[-1] += "  " + cost   # append tier to previous cost
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
#     positions    = []
#     prev_bullet  = None

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
#         r"^(\$\d|\d+%|no charge|not covered|no cost|deductible|kinwell|all other)",
#         re.I
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
#         line_text  = n(" ".join(w["text"] for w in band_words))
#         line_y     = sum(w["top"] for w in band_words) / len(band_words)

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
#       Root benefit name  (bold, no bullet)
#         └─ Sub-section   (bold bullet)
#               └─ Service (regular bullet)  ← what gets indexed

#     Cost columns use bold sub-headers like 'Kinwell Clinics:' /
#     'All Other Non-Specialist:' within a single cell to show tiered
#     pricing for one service.  These tiers are merged into one string.

#     Because some services share a cost line in the PDF (e.g. 'Outpatient
#     surgery center' and 'Anesthesiologist' both map to the same cost row),
#     costs are matched to services by vertical position (y-coordinate)
#     rather than sequential counting.  Services with no direct cost match
#     inherit the previous service's cost.

#     Output schema matches the SBC indexer:
#       event, service, in_network, out_of_network, limitations
#     """
#     import pdfplumber

#     sub_index = []
#     seen      = set()

#     def add(topic, content):
#         key = json_lib.dumps(content, sort_keys=True)
#         if key not in seen:
#             seen.add(key)
#             sub_index.append({
#                 "topic":            topic,
#                 "category":         "cost",
#                 "benefit_category": "medical",
#                 "content":          content,
#                 "keywords":         get_smart_keywords(content),
#             })

#     if not pdf_path:
#         with open(sub_index_path, "w", encoding="utf-8") as f:
#             json_lib.dump(sub_index, f, indent=4)
#         return sub_index

#     # Column x-boundaries (consistent across all benefit pages in this PDF):
#     #   Col 0  (benefit names)  : x  <  200
#     #   Col 3  (in-network)     : 200 <= x < 375
#     #   Col 6  (out-of-network) : 375 <= x
#     IN_NET_X_START  = 200
#     IN_NET_X_END    = 375
#     OUT_NET_X_START = 375
#     OUT_NET_X_END   = 600

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
#             page_words   = page.extract_words(extra_attrs=["fontname"])
#             bold_starters = find_bold_bullet_starters(page_words)

#             # Get vertical positions of every cost line in each column
#             # (done once per page, shared across all rows on that page)
#             in_net_positions  = get_cost_line_positions(page_words, IN_NET_X_START,  IN_NET_X_END)
#             out_net_positions = get_cost_line_positions(page_words, OUT_NET_X_START, OUT_NET_X_END)

#             for table in tables:
#                 for row_idx, row in enumerate(table):
#                     if row_idx < 2:     # rows 0-1 are column headers — skip
#                         continue

#                     # Handle both 9-column (merged header) and 3-column layouts
#                     ncols = len(row)
#                     benefit_cell  = str(row[0] or "")
#                     in_net_cell   = str(row[3] if ncols > 6 else (row[1] if ncols > 1 else ""))
#                     out_net_cell  = str(row[6] if ncols > 6 else (row[2] if ncols > 2 else ""))

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

#                     in_net_map  = map_costs_to_services(service_positions, in_net_positions)
#                     out_net_map = map_costs_to_services(service_positions, out_net_positions)

#                     # Emit one index entry per leaf service.
#                     # If a service has no direct cost assignment (it shares a
#                     # cost row with the service above it), inherit the last seen cost.
#                     last_in_net  = ""
#                     last_out_net = ""
#                     for (service_name, subsection), svc_y in zip(services, service_positions):
#                         assigned_in  = " ".join(in_net_map.get(svc_y, []))
#                         assigned_out = " ".join(out_net_map.get(svc_y, []))

#                         if assigned_in:  last_in_net  = assigned_in
#                         if assigned_out: last_out_net = assigned_out

#                         event = f"{benefit} — {subsection}" if subsection else benefit
#                         topic = f"{event} — {service_name}" if service_name != benefit else event

#                         add(topic, {
#                             "event":          event,
#                             "service":        service_name,
#                             "in_network":     n(last_in_net),
#                             "out_of_network": n(last_out_net),
#                             "limitations":    "",
#                         })

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
