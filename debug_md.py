"""
Run this once to dump the actual docling markdown so we can see exactly
what the parser receives for the problem rows.

Usage:
    python debug_md.py
"""

import os, re
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableStructureOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

PDF_PATH = r"docs\2026\SBC\SBC Summary of Benefits.pdf"  # adjust path

pipeline_options = PdfPipelineOptions(
    table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST)
)
converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend
        )
    }
)

result = converter.convert(PDF_PATH)
md = result.document.export_to_markdown()

lines = md.split("\n")

# ── Print lines around "Are there other deductibles" ──────────────────────
# print("=" * 60)
# print("CONTEXT: 'Are there other deductibles'")
# print("=" * 60)
# for i, line in enumerate(lines):
#     if "are there other" in line.lower() or "deductibles for specific" in line.lower():
#         start = max(0, i - 3)
#         end = min(len(lines), i + 5)
#         for j in range(start, end):
#             marker = ">>>" if j == i else "   "
#             print(f"{marker} {j:4}: {lines[j]!r}")
#         print()

# # ── Print lines around "Imaging" ──────────────────────────────────────────
# print("=" * 60)
# print("CONTEXT: 'Imaging (CT/PET)'")
# print("=" * 60)
# for i, line in enumerate(lines):
#     if "imaging" in line.lower() and ("ct" in line.lower() or "pet" in line.lower()):
#         start = max(0, i - 3)
#         end = min(len(lines), i + 5)
#         for j in range(start, end):
#             marker = ">>>" if j == i else "   "
#             print(f"{marker} {j:4}: {lines[j]!r}")
#         print()

# # ── Also dump all lines that contain "|" near those sections ──────────────
# print("=" * 60)
# print("All table lines between 'If you have a test' and 'If you need drugs'")
# print("=" * 60)
# in_section = False
# for i, line in enumerate(lines):
#     if "if you have a test" in line.lower():
#         in_section = True
#     if in_section and "if you need drugs" in line.lower():
#         break
#     if in_section:
#         print(f"  {i:4}: {line!r}")
# print("=== All lines from 'Excluded Services' to 'Your Rights' ===")
# printing = False
# for i, line in enumerate(lines):
#     if "excluded services" in line.lower():
#         printing = True
#     if printing:
#         print(f"  {i:4}: {line!r}")
#     if printing and "your rights to continue coverage" in line.lower():
#         break

# Print every line from "What You Will Pay" through "Excluded Services"
# with its index, so we can see exact column structure
printing = False
for i, line in enumerate(lines):
    if "what you will pay" in line.lower():
        printing = True
    if printing:
        # Show pipe count and repr so column positions are clear
        pipes = line.count("|")
        cols = [c.strip() for c in line.split("|")] if "|" in line else []
        non_empty = [c for c in cols if c]
        print(f"{i:4} [pipes={pipes:2}] {line!r}")
    if printing and "excluded services" in line.lower():
        break
