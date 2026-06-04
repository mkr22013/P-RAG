# """
# Full debug script for the Premera Dental Plan booklet.
# Shows: total pages, all tables found, and text preview of every page.
# Usage: python debug_dental.py path/to/Dental.pdf
# """
# import sys, pdfplumber, os

# # Get the directory where THIS script (debug_summary.py) is located
# BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# # Move up one level from 'debug' to the root, then down into 'docs'
# DEFAULT_PDF = os.path.join(BASE_DIR, "..", "docs", "2026", "dental", "Dental.pdf")

# PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF

# with pdfplumber.open(PDF_PATH) as pdf:
#     print(f"Total pages: {len(pdf.pages)}\n")

#     for page_num, page in enumerate(pdf.pages):
#         text   = page.extract_text() or ""
#         tables = page.extract_tables() or []

#         print(f"{'='*60}")
#         print(f"PAGE {page_num+1}  |  tables={len(tables)}")
#         print(f"{'='*60}")

#         # First 400 chars of text as preview
#         preview = " | ".join(
#             l.strip() for l in text.split("\n") if l.strip()
#         )[:400]
#         print(f"TEXT: {preview!r}")

#         # Print every table found
#         for ti, table in enumerate(tables):
#             ncols = len(table[0]) if table else 0
#             print(f"\n  TABLE {ti+1}: {len(table)} rows x {ncols} cols")
#             for ri, row in enumerate(table):
#                 cleaned = [
#                     str(c or "").replace("\n", " | ").strip()[:60]
#                     for c in row
#                 ]
#                 if any(cleaned):
#                     print(f"    row{ri}: {cleaned}")
#         print()

# """
# Debug script for the Premera Dental Plan booklet.
# Shows full raw text for benefit pages so we can understand
# exactly how pdfplumber extracts class sections and bullet points.

# Usage: python debug_premera_dental.py
#     or: python debug_premera_dental.py path/to/Dental.pdf
# """

# import sys, pdfplumber, os

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DEFAULT_PDF = os.path.join(BASE_DIR, "..", "docs", "2026", "dental", "555017B_Dental.pdf")
# PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF

# # Keywords that tell us a page has benefit content we care about
# BENEFIT_KEYWORDS = [
#     "Class I",
#     "Class II",
#     "Class III",
#     "DESCRIPTION OF COVERED SERVICES",
#     "BENEFIT PERCENTAGES",
#     "ORTHODONTIA",
#     "Calendar Year Deductible",
#     "Dental Benefit Maximum",
#     "Family Dental Deductible",
# ]

# with pdfplumber.open(PDF_PATH) as pdf:
#     print(f"Total pages: {len(pdf.pages)}\n")

#     for page_num, page in enumerate(pdf.pages):
#         text = page.extract_text() or ""
#         tables = page.extract_tables() or []

#         is_benefit_page = any(kw.lower() in text.lower() for kw in BENEFIT_KEYWORDS)

#         print(f"{'='*70}")
#         print(
#             f"PAGE {page_num+1}  |  tables={len(tables)}  |  benefit_page={is_benefit_page}"
#         )
#         print(f"{'='*70}")

#         if is_benefit_page:
#             # Show FULL raw text with explicit newlines marked
#             # so we can see exactly how pdfplumber structures the output
#             print("FULL RAW TEXT:")
#             for i, line in enumerate(text.split("\n")):
#                 print(f"  L{i+1:03d}: {repr(line)}")
#         else:
#             # Non-benefit pages — just show short preview
#             preview = " | ".join(l.strip() for l in text.split("\n") if l.strip())[:200]
#             print(f"TEXT PREVIEW: {preview!r}")

#         # Tables on every page
#         for ti, table in enumerate(tables):
#             ncols = len(table[0]) if table else 0
#             print(f"\n  TABLE {ti+1}: {len(table)} rows x {ncols} cols")
#             for ri, row in enumerate(table):
#                 cleaned = [str(c or "").replace("\n", " | ").strip()[:80] for c in row]
#                 if any(cleaned):
#                     print(f"    row{ri:02d}: {cleaned}")

#         print()




# # """
# # Quick check of what's in the Premera dental index.
# # Shows event counts and sample entries per event.

# # Usage: python check_dental_index.py
# #     or: python check_dental_index.py path/to/index.json
# # """

# import sys, json, os
# from collections import Counter

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DEFAULT_INDEX = os.path.join(
#     BASE_DIR, "indices", "2026_dental_1000016_premera_dental_plan_standard"
# )

# INDEX_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INDEX

# data = json.load(open(INDEX_PATH, encoding="utf-8"))

# cost = [e for e in data if e.get("category") == "cost"]
# info = [e for e in data if e.get("category") == "info"]

# print(f"Total entries : {len(data)}")
# print(f"Cost entries  : {len(cost)}")
# print(f"Info entries  : {len(info)}")
# print()

# # Count per event
# events = Counter(e["content"]["event"] for e in cost)
# print("Cost entries by event:")
# for event, count in sorted(events.items()):
#     print(f"  {count:3d}  {event}")
# print(f"  ---")
# print(f"  {sum(events.values()):3d}  TOTAL")
# print()

# # Show all services per event
# for event in sorted(events.keys()):
#     entries = [e for e in cost if e["content"]["event"] == event]
#     print(f"{'='*60}")
#     print(f"EVENT: {event}  ({len(entries)} entries)")
#     print(f"{'='*60}")
#     for e in entries:
#         c = e["content"]
#         print(f"  service   : {c['service']}")
#         print(f"  in_network: {c['in_network']}")
#         print(f"  limitation: {c['limitations'][:80]}...")
#         print()



import sys, json, os

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DEFAULT_INDEX = os.path.join(
#     BASE_DIR, "..", "indices", "2026_dental_1000016_premera_dental_plan_standard"
# )

# index_path = "C:\\Personal\\AI\\P-RAG\\indices\\2026_dental_1000016_premera_dental_plan_standard.json"
# data = json.load(open(index_path, encoding="utf-8"))

# for e in data:
#     svc = e.get("content", {}).get("service", "")
#     if "implant" in svc.lower():
#         print(f"topic:    {e.get('topic')}")
#         print(f"keywords: {e.get('keywords')}")
#         print(f"service:  {svc}")
#         print()


import pdfplumber

pdf_path = "C:\\Personal\\AI\\P-RAG\\docs\\2026\\dental\\Dental.pdf"

with pdfplumber.open(pdf_path) as pdf:
    for i, page in enumerate(pdf.pages[:10], 1):
        text = (page.extract_text() or "")[:200]
        print(f"PDF page {i}: {text[:80]}")