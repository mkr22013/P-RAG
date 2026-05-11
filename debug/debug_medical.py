"""
Find and inspect the complex benefits table pages.
Usage: python debug_medical.py path/to/medical_booklet.pdf
"""

import os
import re
import sys
import pdfplumber

# Get the directory where THIS script (debug_summary.py) is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Move up one level from 'debug' to the root, then down into 'docs'
DEFAULT_PDF = os.path.join(BASE_DIR, "..", "docs", "2026", "medical", "Medical.pdf")

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF

# with pdfplumber.open(PDF_PATH) as pdf:
#     print(f"Total pages: {len(pdf.pages)}\n")
#     for page_num, page in enumerate(pdf.pages):
#         tables = page.extract_tables() or []
#         text = (page.extract_text() or "")[:120].replace("\n", " ")
#         print(f"  Page {page_num+1:3}: tables={len(tables)}  text={text!r}")

# TARGET_PAGES = [18, 19]   # 1-indexed
# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num in TARGET_PAGES:
#         page = pdf.pages[page_num - 1]
#         tables = page.extract_tables() or []
#         print(f"\n{'='*60}")
#         print(f"PAGE {page_num} — {len(tables)} table(s)")
#         print(f"{'='*60}")

#         for ti, table in enumerate(tables):
#             ncols = max(len(r) for r in table) if table else 0
#             print(f"\nTable {ti}: {len(table)} rows x {ncols} cols")
#             for ri, row in enumerate(table):
#                 for ci, cell in enumerate(row):
#                     val = str(cell or "").strip()
#                     if val:
#                         # Show \n as literal so we can see line breaks
#                         print(f"  row{ri:3} col{ci}: {val.replace(chr(10), '|')!r}")

# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num, page in enumerate(pdf.pages):
#         tables = page.extract_tables() or []
#         if not tables:
#             continue
#         text = (page.extract_text() or "").lower()
#         if "dental injury" not in text:
#             continue

#         print(f"\n{'='*60}")
#         print(f"PAGE {page_num + 1}")
#         print(f"{'='*60}")

#         # Table cell structure
#         for table in tables:
#             print(f"\nTable: {len(table)} rows")
#             for ri, row in enumerate(table):
#                 for ci, cell in enumerate(row):
#                     val = str(cell or "").strip()
#                     if val:
#                         print(f"  row{ri} col{ci}: {val.replace(chr(10),'|')!r}")

#         # Words with bold detection
#         print(f"\n--- Words (BOLD marked) ---")
#         words = page.extract_words(extra_attrs=["fontname"])
#         for w in words:
#             fn = w.get("fontname","")
#             bold = "BOLD" if ("Bold" in fn or "bold" in fn) else "    "
#             print(f"  {bold} x={w['x0']:5.0f} y={w['top']:5.0f} {w['text']!r}")

#         break  # first matching page only


# def n(v):
#     return re.sub(r"\s+", " ", str(v or "")).strip()


# def is_bold(w):
#     fn = w.get("fontname", "")
#     return "Bold" in fn or "bold" in fn


# with pdfplumber.open(PDF_PATH) as pdf:
#     print(f"Total pages: {len(pdf.pages)}")

#     for page_num, page in enumerate(pdf.pages):
#         tables = page.extract_tables() or []
#         if not tables:
#             continue

#         flat = " ".join(n(str(c or "")) for t in tables for r in t for c in r).upper()
#         page_text = (page.extract_text() or "").upper()
#         has_share = (
#             "YOUR SHARE OF THE ALLOWED AMOUNT" in flat
#             or "YOUR SHARE OF THE ALLOWED AMOUNT" in page_text
#         )

#         if not has_share:
#             continue

#         print(f"\n{'='*60}")
#         print(f"PAGE {page_num+1} — PASSES FILTER")
#         print(f"{'='*60}")

#         # Check column x-ranges
#         words = page.extract_words(extra_attrs=["fontname"])
#         print(
#             f"\nWord x-range: {min(w['x0'] for w in words):.0f} to {max(w['x0'] for w in words):.0f}"
#         )
#         print(f"Words in col0 (x<200): {sum(1 for w in words if w['x0'] < 200)}")
#         print(
#             f"Words in col3 (200-375): {sum(1 for w in words if 200 <= w['x0'] < 375)}"
#         )
#         print(f"Words in col6 (375+): {sum(1 for w in words if w['x0'] >= 375)}")

#         # Check bold bullets
#         bold_starters = set()
#         prev_bullet = None
#         for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
#             if w["x0"] >= 200:
#                 continue
#             if w["text"] in ("•", "\u2022"):
#                 prev_bullet = w
#             elif prev_bullet and abs(w["top"] - prev_bullet["top"]) < 6:
#                 if is_bold(w):
#                     bold_starters.add(w["text"])
#                 prev_bullet = None
#             else:
#                 prev_bullet = None
#         print(f"\nBold bullet starters found: {bold_starters}")

#         # Check table structure
#         for ti, table in enumerate(tables):
#             print(
#                 f"\nTable {ti}: {len(table)} rows x {len(table[0]) if table else 0} cols"
#             )
#             for ri, row in enumerate(table[:4]):
#                 if ri < 2:
#                     print(
#                         f"  [HEADER row{ri}] {[n(str(c or ''))[:30] for c in row if n(str(c or ''))]}"
#                     )
#                     continue
#                 ncols = len(row)
#                 c0 = str(row[0] or "")
#                 c3 = str(row[3] if ncols > 6 else (row[1] if ncols > 1 else ""))
#                 c6 = str(row[6] if ncols > 6 else (row[2] if ncols > 2 else ""))
#                 print(f"  row{ri}: c0={c0[:40]!r}")
#                 print(f"         c3={c3[:40]!r}")
#                 print(f"         c6={c6[:40]!r}")

#                 # Simulate parse_benefit_cell
#                 lines = c0.split("\n")
#                 has_bullet = any(l.strip().startswith("•") for l in lines)
#                 first_bold_bullet = next(
#                     (
#                         re.sub(r"^•\s*", "", l.strip()).split()[0]
#                         for l in lines
#                         if l.strip().startswith("•")
#                         and any(
#                             w["text"] == re.sub(r"^•\s*", "", l.strip()).split()[0]
#                             for w in words
#                             if is_bold(w)
#                         )
#                     ),
#                     None,
#                 )
#                 print(
#                     f"         has_bullet={has_bullet}, first_bold_bullet={first_bold_bullet!r}"
#                 )

#         # Check leaf service positions
#         positions = []
#         prev_bullet = None
#         for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
#             if w["x0"] >= 200:
#                 continue
#             if w["text"] in ("•", "\u2022"):
#                 prev_bullet = w
#             elif prev_bullet and abs(w["top"] - prev_bullet["top"]) < 6:
#                 if not is_bold(w):
#                     positions.append(prev_bullet["top"])
#                 prev_bullet = None
#             else:
#                 prev_bullet = None
#         print(f"\nLeaf service positions found: {len(positions)} — {positions[:5]}")

#         if page_num >= 12:  # limit output
#             break

"""
Trace get_subsection_headers and parse_benefit_cell for the dental row.
Usage: python debug_dental.py path/to/Medical.pdf
"""

CROSS_REF = re.compile(r"\s*\(See.*", re.I)


def get_subsection_headers(page):
    lines = {}
    for char in page.chars:
        if char["x0"] >= 200:
            continue
        y = round(char["top"] / 2) * 2
        lines.setdefault(y, []).append(char)

    starters = set()
    for y in sorted(lines):
        chars = sorted(lines[y], key=lambda c: c["x0"])
        line_text = "".join(c["text"] for c in chars).strip()
        if not line_text.startswith("•"):
            continue
        after = [c for c in chars if c["text"] not in ("•", " ")]
        is_bold = any(
            "Bold" in c.get("fontname", "") or "bold" in c.get("fontname", "")
            for c in after
        )
        first_word = (
            line_text.lstrip("• ").split()[0] if line_text.lstrip("• ").split() else ""
        )
        print(
            f"  bullet: {line_text[:45]!r:47} bold={is_bold}  first_word={first_word!r}"
        )
        if is_bold and first_word:
            starters.add(first_word)
    return starters


def parse_benefit_cell(cell_text, subsection_headers):
    benefit = ""
    current_subsection = None
    services = []
    lines = cell_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("•"):
            item = re.sub(r"^•\s*", "", line)
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("•"):
                cont = lines[i].strip()
                if cont and not CROSS_REF.match(cont):
                    item += " " + cont
                i += 1
            had_cross_ref = bool(re.search(r"\(See", item, re.I))
            item = CROSS_REF.sub("", item).strip()
            first_word = item.split()[0] if item else ""
            if had_cross_ref or first_word in subsection_headers:
                print(f"    → SUBSECTION: {item!r}  (cross_ref={had_cross_ref})")
                current_subsection = item
            elif item:
                print(
                    f"    → SERVICE: {item!r}  under subsection={current_subsection!r}"
                )
                services.append((item, current_subsection))
        else:
            benefit = (benefit + " " + line).strip()
            i += 1
    return benefit, services


with pdfplumber.open(PDF_PATH) as pdf:
    for page_num, page in enumerate(pdf.pages):
        text = (page.extract_text() or "").upper()
        if "DENTAL INJURY" not in text or "YOUR SHARE" not in text:
            continue

        print(f"\n{'='*60}")
        print(f"PAGE {page_num+1}")
        print(f"{'='*60}")

        print("\n--- get_subsection_headers ---")
        headers = get_subsection_headers(page)
        print(f"\nFINAL subsection_headers = {headers}\n")

        tables = page.extract_tables() or []
        for table in tables:
            for ri, row in enumerate(table):
                if ri < 2:
                    continue
                c0 = str(row[0] or "")
                if "dental injury" not in c0.lower():
                    continue
                print(f"--- parse_benefit_cell ---")
                benefit, services = parse_benefit_cell(c0, headers)
                print(f"\nbenefit = {benefit!r}")
                print(f"services ({len(services)}):")
                for svc, sub in services:
                    print(f"  service={svc!r}  subsection={sub!r}")
        break
