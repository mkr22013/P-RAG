"""
Print exact raw cell text for rows containing rehabilitation, acupuncture, dental injury.
Usage: python debug_cells.py path/to/Medical.pdf
"""

import sys, pdfplumber, re, os

# Get the directory where THIS script (debug_summary.py) is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Move up one level from 'debug' to the root, then down into 'docs'
DEFAULT_PDF = os.path.join(BASE_DIR, "..", "docs", "2026", "medical", "Medical.pdf")

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF
# KEYWORDS = ["rehabilitation", "acupuncture", "dental injury"]

# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num, page in enumerate(pdf.pages):
#         text = (page.extract_text() or "").lower()
#         if not any(k in text for k in KEYWORDS):
#             continue
#         if "your share" not in text:
#             continue

#         tables = page.extract_tables() or []
#         for table in tables:
#             for ri, row in enumerate(table):
#                 if ri < 2: continue
#                 c0 = str(row[0] or "")
#                 if not any(k in c0.lower() for k in KEYWORDS):
#                     continue
#                 print(f"\n=== Page {page_num+1} row {ri} ===")
#                 print("RAW CELL TEXT (each \\n shown as newline):")
#                 for line in c0.split("\n"):
#                     print(f"  {line!r}")

# KEYWORD  = sys.argv[2].lower() if len(sys.argv) > 2 else "emergency"

# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num, page in enumerate(pdf.pages):
#         text = (page.extract_text() or "").lower()
#         if KEYWORD not in text or "your share" not in text:
#             continue
#         for table in (page.extract_tables() or []):
#             for ri, row in enumerate(table):
#                 if ri < 2: continue
#                 c0 = str(row[0] or "")
#                 if KEYWORD not in c0.lower(): continue
#                 print(f"\n=== Page {page_num+1} row {ri} ===")
#                 for line in c0.split("\n"):
#                     print(f"  {line!r}")

# CROSS_REF  = re.compile(r"\s*\(See.*", re.I)
# LIMIT_NOTE = re.compile(r"^(calendar\s+year|day\s+limit|visit\s+limit|no\s+limit|limited\s+to|up\s+to\s+\d)", re.I)
# BENEFIT_NOTE = re.compile(r"^(you\s+may|the\s+copay|this\s+plan|see\s+the\s+|covers\s+routine|includes\s+|for\s+permanent|care\s+during|calendar\s+year|day\s+limit|visit\s+limit|lifetime\s+limit)", re.I)
# SERVICE_NOTE = re.compile(r"\s+(calendar\s+year|no\s+limit|day\s+limit|visit\s+limit|you\s+may|the\s+copay|this\s+plan|see\s+those|no\s+charge\s+on|\*all\s+approved|also\s+covered|limit\s+per|limit\s+\$|special\s+criteria|for\s+coverage\s+details|virtual\s+pediatric|for\s+members\s+\d).*", re.I)

# def clean(t): return re.sub(r"\s+", " ", str(t or "")).strip()

# def get_subsection_headers(page):
#     bullet_ys = [c["top"] for c in page.chars if c["text"] == "•" and c["x0"] < 200]
#     if not bullet_ys: return set()
#     words = page.extract_words(extra_attrs=["fontname"])
#     starters = set()
#     for word in words:
#         if word["x0"] >= 200: continue
#         for by in bullet_ys:
#             if abs(word["top"] - by) <= 4:
#                 if "Bold" in word.get("fontname", ""): starters.add(word["text"])
#                 break
#     return starters
# def _old_get_subsection_headers(page):
#     starters  = set()
#     all_chars = sorted(page.chars, key=lambda c: (c["top"], c["x0"]))
#     for idx, char in enumerate(all_chars):
#         if char["text"] != "•" or char["x0"] >= 200:
#             continue
#         first_word = ""; first_word_is_bold = False
#         for next_char in all_chars[idx + 1:]:
#             if next_char["x0"] >= 200: break
#             if abs(next_char["top"] - char["top"]) > 6: break
#             if not next_char["text"].strip():
#                 if first_word: break
#                 continue
#             first_word += next_char["text"]
#             if len(first_word) == 1:
#                 first_word_is_bold = "Bold" in next_char.get("fontname", "")
#         if first_word and first_word_is_bold:
#             starters.add(first_word)
#     return starters

# def parse_benefit_cell(cell_text, subsection_headers):
#     benefit = ""; benefit_done = False; current_subsection = None; services = []
#     lines_ = cell_text.split("\n"); i = 0
#     while i < len(lines_):
#         line = lines_[i].strip()
#         if not line: i += 1; continue
#         if line.startswith("•"):
#             benefit_done = True
#             bullet_text = re.sub(r"^•\s*", "", line)
#             item = bullet_text; continuation_count = 0; item_lines = []; i += 1
#             while i < len(lines_) and not lines_[i].strip().startswith("•"):
#                 cont = lines_[i].strip()
#                 if cont and not CROSS_REF.match(cont):
#                     item += " " + cont; continuation_count += 1; item_lines.append(cont)
#                 i += 1
#             had_cross_ref = bool(re.search(r'\(See', item, re.I))
#             item = CROSS_REF.sub("", item).strip()
#             first_word = (CROSS_REF.sub("", bullet_text).strip().split() or [""])[0]
#             has_limit_continuations = any(LIMIT_NOTE.match(l) for l in item_lines)
#             is_subsection = (had_cross_ref or first_word in subsection_headers or
#                              (continuation_count >= 2 and has_limit_continuations))
#             if is_subsection:
#                 current_subsection = clean(CROSS_REF.sub("", bullet_text).strip())
#             elif item:
#                 item = SERVICE_NOTE.sub("", item).strip()
#                 if item: services.append((clean(item), current_subsection))
#         else:
#             if not benefit_done and not BENEFIT_NOTE.match(line):
#                 benefit = (benefit + " " + line).strip()
#             i += 1
#     return benefit, services

# last_benefit = ""
# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num, page in enumerate(pdf.pages):
#         text = (page.extract_text() or "").upper()
#         if "YOUR SHARE OF THE ALLOWED AMOUNT" not in text:
#             continue
#         subsection_headers = get_subsection_headers(page)
#         if subsection_headers:
#             print(f"\n  [Page {page_num+1} bold headers: {subsection_headers}]")
#         for table in (page.extract_tables() or []):
#             for ri, row in enumerate(table):
#                 if ri < 2: continue
#                 c0 = str(row[0] or "")
#                 if not c0.strip(): continue
#                 benefit, services = parse_benefit_cell(c0, subsection_headers)
#                 if not benefit and last_benefit: benefit = last_benefit
#                 if benefit: last_benefit = benefit
#                 print(f"\n--- Page {page_num+1} row {ri} ---")
#                 print(f"  BENEFIT : {benefit!r}")
#                 for svc, sub in services:
#                     print(f"  SERVICE : {svc!r}  (subsection={sub!r})")
#                 if not services:
#                     print(f"  (no services parsed)")


KEYWORD = " ".join(sys.argv[2:]).lower() if len(sys.argv) > 2 else "neurodevelopmental"

with pdfplumber.open(PDF_PATH) as pdf:
    for page_num, page in enumerate(pdf.pages):
        text = (page.extract_text() or "").lower()
        if KEYWORD not in text or "your share" not in text:
            continue
        for table in page.extract_tables() or []:
            for ri, row in enumerate(table):
                if ri < 2:
                    continue
                c0 = str(row[0] or "")
                c3 = str(row[3] if len(row) > 6 else (row[1] if len(row) > 1 else ""))
                c6 = str(row[6] if len(row) > 6 else (row[2] if len(row) > 2 else ""))
                if KEYWORD not in c0.lower():
                    continue
                print(f"\n=== Page {page_num+1} row {ri} ===")
                print("COL0:")
                [print(f"  {l!r}") for l in c0.split("\n")]
                print(f"COL3: {c3.replace(chr(10),'|')!r}")
                print(f"COL6: {c6.replace(chr(10),'|')!r}")
