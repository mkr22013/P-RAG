"""
indexers/run_indexer.py
────────────────────────────────────────────────────────────────────────────
Shared standalone runner for all individual indexers.

Called from each indexer's __main__ block. One place to maintain
the full dev/prod flow — any change here applies to all indexers.

Dev flow (AZURE_BLOB_CONNECTION_STRING not set):
    pdf_path = local file path
    → classify_document(pdf_path)
    → generate_sub_index(output_path, pdf_path)
    → post_index_upload() is a no-op

Prod flow (AZURE_BLOB_CONNECTION_STRING set):
    pdf_path = blob path e.g. "2026/1000016/medical/Medical.pdf"
    → download_pdf(blob_path) → temp local file
    → classify_document(temp_file)
    → generate_sub_index(output_path, temp_file)
    → post_index_upload() → blob + PostgreSQL + Service Bus
    → cleanup temp file
"""

import os
import sys
import json
import asyncio
import tempfile
from dotenv import load_dotenv

load_dotenv()


def run(plan_category: str, classify_fn, generate_fn):
    """
    Standalone runner shared by all indexers.

    Parameters:
        plan_category — e.g. "medical", "dental", "vision", "sbc"
        classify_fn   — classify_document function from the indexer
        generate_fn   — generate_sub_index function from the indexer

    CLI args:
        argv[1] = pdf_path   (local file in dev, blob path in prod)
        argv[2] = output_path (local JSON output path)
    """
    if len(sys.argv) < 3:
        print(
            f"Usage: python -m indexers.{plan_category}_indexer <pdf_path> <output_json_path>"
        )
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2]
    temp_pdf = None

    # ── Production: blob path → download to temp local file ──────────────────
    if os.getenv("AZURE_BLOB_CONNECTION_STRING"):
        from infrastructure.blob_storage import download_pdf

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        print(f"[*] Downloading from blob: {pdf_path} → {tmp.name}")
        ok = asyncio.run(download_pdf(pdf_path, tmp.name))
        if not ok:
            print(f"[!] Failed to download PDF from blob: {pdf_path}")
            sys.exit(1)
        temp_pdf = tmp.name
        pdf_path = tmp.name

    # ── Step 1: classify ──────────────────────────────────────────────────────
    print(f"[*] Classifying: {pdf_path}")
    meta = classify_fn(pdf_path)
    if not meta:
        print("[!] Classification failed — cannot proceed.")
        _cleanup(temp_pdf)
        sys.exit(1)

    # ── Step 2: generate index (writes JSON to output_path) ───────────────────
    print(f"[*] Indexing: {pdf_path} → {output_path}")
    generate_fn(output_path, pdf_path)

    # ── Step 3: upload to blob + PostgreSQL + Service Bus (no-op in dev) ──────
    from infrastructure.post_index import post_index_upload

    success = asyncio.run(
        post_index_upload(
            sub_index_path=output_path,
            year=meta.get("year", ""),
            plan_category=plan_category,
            plan=meta.get("plan", ""),
            group_number=meta.get("group_number", ""),
            group_name=meta.get("group_name", ""),
            plan_type=meta.get("type", ""),
            plan_tier=meta.get("tier", ""),
            product_line=meta.get("product_line", ""),
            variant=meta.get("variant", ""),
            network=meta.get("network", "") or "",
        )
    )

    # ── Step 3b: sync drug_names.json to Blob (Rx only) ────────────────────────
    # The shared, plan-agnostic drug name word list used by category.py for
    # Rx category detection and spelling correction. Already written locally
    # by generate_fn() above (via rx_indexer.py's update_drug_names_file).
    # In production, also upload the updated file to Blob so other server
    # instances pick it up on their next TTL refresh (see category.py).
    # No-op locally — upload_index() already handles the local-dev fallback,
    # but drug_names.json only exists for Rx, so we only attempt this for rx.
    if plan_category == "rx":
        try:
            from infrastructure.blob_storage import upload_index
            from utility.category import DRUG_NAMES_FILE

            if os.path.exists(DRUG_NAMES_FILE):
                with open(DRUG_NAMES_FILE, encoding="utf-8") as f:
                    drug_words = json.load(f)
                asyncio.run(upload_index("drug_names.json", drug_words))
                print(f"[*] drug_names.json synced ({len(drug_words)} words)")
        except Exception as e:
            print(f"[!] drug_names.json blob sync skipped: {e}")

    _cleanup(temp_pdf)

    if success:
        print(f"✅ Done: {output_path}")
    else:
        print(f"⚠️  Indexed locally but cloud upload had issues: {output_path}")
        sys.exit(1)


def _cleanup(temp_pdf: str | None):
    if temp_pdf and os.path.exists(temp_pdf):
        os.remove(temp_pdf)
        print(f"[*] Cleaned up temp file: {temp_pdf}")


# # ==============================================Previously working code==============================================

# # """
# # indexers/run_indexer.py
# # ────────────────────────────────────────────────────────────────────────────
# # Shared standalone runner for all individual indexers.

# # Called from each indexer's __main__ block. One place to maintain
# # the full dev/prod flow — any change here applies to all indexers.

# # Dev flow (AZURE_BLOB_CONNECTION_STRING not set):
# #     pdf_path = local file path
# #     → classify_document(pdf_path)
# #     → generate_sub_index(output_path, pdf_path)
# #     → post_index_upload() is a no-op

# # Prod flow (AZURE_BLOB_CONNECTION_STRING set):
# #     pdf_path = blob path e.g. "2026/1000016/medical/Medical.pdf"
# #     → download_pdf(blob_path) → temp local file
# #     → classify_document(temp_file)
# #     → generate_sub_index(output_path, temp_file)
# #     → post_index_upload() → blob + PostgreSQL + Service Bus
# #     → cleanup temp file
# # """

# # import os
# # import sys
# # import asyncio
# # import tempfile
# # from dotenv import load_dotenv

# # load_dotenv()


# # def run(plan_category: str, classify_fn, generate_fn):
# #     """
# #     Standalone runner shared by all indexers.

# #     Parameters:
# #         plan_category — e.g. "medical", "dental", "vision", "sbc"
# #         classify_fn   — classify_document function from the indexer
# #         generate_fn   — generate_sub_index function from the indexer

# #     CLI args:
# #         argv[1] = pdf_path   (local file in dev, blob path in prod)
# #         argv[2] = output_path (local JSON output path)
# #     """
# #     if len(sys.argv) < 3:
# #         print(
# #             f"Usage: python -m indexers.{plan_category}_indexer <pdf_path> <output_json_path>"
# #         )
# #         sys.exit(1)

# #     pdf_path = sys.argv[1]
# #     output_path = sys.argv[2]
# #     temp_pdf = None

# #     # ── Production: blob path → download to temp local file ──────────────────
# #     if os.getenv("AZURE_BLOB_CONNECTION_STRING"):
# #         from infrastructure.blob_storage import download_pdf

# #         tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
# #         tmp.close()
# #         print(f"[*] Downloading from blob: {pdf_path} → {tmp.name}")
# #         ok = asyncio.run(download_pdf(pdf_path, tmp.name))
# #         if not ok:
# #             print(f"[!] Failed to download PDF from blob: {pdf_path}")
# #             sys.exit(1)
# #         temp_pdf = tmp.name
# #         pdf_path = tmp.name

# #     # ── Step 1: classify ──────────────────────────────────────────────────────
# #     print(f"[*] Classifying: {pdf_path}")
# #     meta = classify_fn(pdf_path)
# #     if not meta:
# #         print("[!] Classification failed — cannot proceed.")
# #         _cleanup(temp_pdf)
# #         sys.exit(1)

# #     # ── Step 2: generate index (writes JSON to output_path) ───────────────────
# #     print(f"[*] Indexing: {pdf_path} → {output_path}")
# #     generate_fn(output_path, pdf_path)

# #     # ── Step 3: upload to blob + PostgreSQL + Service Bus (no-op in dev) ──────
# #     from infrastructure.post_index import post_index_upload

# #     success = asyncio.run(
# #         post_index_upload(
# #             sub_index_path=output_path,
# #             year=meta.get("year", ""),
# #             plan_category=plan_category,
# #             plan=meta.get("plan", ""),
# #             group_number=meta.get("group_number", ""),
# #             group_name=meta.get("group_name", ""),
# #             plan_type=meta.get("type", ""),
# #             plan_tier=meta.get("tier", ""),
# #             product_line=meta.get("product_line", ""),
# #             variant=meta.get("variant", ""),
# #             network=meta.get("network", "") or "",
# #         )
# #     )

# #     _cleanup(temp_pdf)

# #     if success:
# #         print(f"✅ Done: {output_path}")
# #     else:
# #         print(f"⚠️  Indexed locally but cloud upload had issues: {output_path}")
# #         sys.exit(1)


# # def _cleanup(temp_pdf: str | None):
# #     if temp_pdf and os.path.exists(temp_pdf):
# #         os.remove(temp_pdf)
# #         print(f"[*] Cleaned up temp file: {temp_pdf}")
