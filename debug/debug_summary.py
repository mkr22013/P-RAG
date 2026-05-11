"""
Debug script to see what pdfplumber extracts from the Summary of Your Costs page.
Usage: python debug_summary.py path/to/Medical.pdf
"""

import sys, pdfplumber, os

# Get the directory where THIS script (debug_summary.py) is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Move up one level from 'debug' to the root, then down into 'docs'
DEFAULT_PDF = os.path.join(BASE_DIR, "..", "docs", "2026", "medical", "Medical.pdf")

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF


# def clean(v):
#     import re

#     return re.sub(r"\s+", " ", str(v or "")).strip()


# with pdfplumber.open(PDF_PATH) as pdf:
#     for page_num, page in enumerate(pdf.pages):
#         text = page.extract_text() or ""
#         if "SUMMARY OF YOUR COSTS" not in text.upper():
#             continue

#         print(f"\n=== PAGE {page_num+1} — SUMMARY OF YOUR COSTS ===")

#         tables = page.extract_tables() or []
#         print(f"Tables found: {len(tables)}")

#         for ti, table in enumerate(tables):
#             print(
#                 f"\n--- Table {ti+1}: {len(table)} rows x {len(table[0]) if table else 0} cols ---"
#             )
#             for ri, row in enumerate(table):
#                 cleaned = [clean(c) for c in row]
#                 if any(cleaned):
#                     print(f"  row{ri}: {cleaned}")

"""
Show raw text lines from the Summary of Your Costs page.
Usage: python debug_summary.py path/to/Medical.pdf
"""

SIGNALS = [
    "individual deductible",
    "family deductible",
    "out-of-pocket maximum",
    "professional visit copay",
]

with pdfplumber.open(PDF_PATH) as pdf:
    for page_num, page in enumerate(pdf.pages):
        text = (page.extract_text() or "").lower()
        if sum(1 for s in SIGNALS if s in text) >= 2:
            print(f"\n=== PAGE {page_num+1} ===")
            for line in (page.extract_text() or "").split("\n"):
                if line.strip():
                    print(f"  {line!r}")
            print()
