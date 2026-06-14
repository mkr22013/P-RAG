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
from config import settings
import sqlite3
import json as json_lib
import re
import asyncio

from indexers import sbc_indexer as sbc_indexer, vision_indexer
from indexers import medical_indexer as medical_indexer
from indexers import dental_indexer as dental_indexer
from indexers import vision_indexer as vision_indexer
from indexers import rx_indexer as rx_indexer

from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

IS_PRODUCTION = settings.is_production

from infrastructure.post_index import post_index_upload

DOC_BASE_DIR = os.path.abspath("./docs")
INDEX_OUTPUT_DIR = "./indices"
DB_PATH = os.path.join(os.path.dirname(__file__), "p_insurance_index.db")
LOCAL_MODEL = settings.OLLAMA_MODEL
CURRENT_YEAR_INT = datetime.now().year

# Maps folder name (lowercase) → (classify_fn, generate_index_fn)
# Add new booklet types here without touching build_all().
BOOKLET_STRATEGIES = {
    "sbc": (sbc_indexer.classify_document, sbc_indexer.generate_sub_index),
    "medical": (medical_indexer.classify_document, medical_indexer.generate_sub_index),
    "dental": (dental_indexer.classify_document, dental_indexer.generate_sub_index),
    "vision": (vision_indexer.classify_document, vision_indexer.generate_sub_index),
    "rx": (rx_indexer.classify_document, rx_indexer.generate_sub_index),
}


def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Master Index — fast routing by plan identity
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER,
            plan_category TEXT,
            group_number TEXT,
            group_name TEXT,
            plan TEXT,
            plan_type TEXT,
            plan_tier TEXT,
            product_line TEXT,
            variant TEXT,
            network TEXT,
            pdf_path TEXT UNIQUE,
            sub_index_path TEXT
        )
    """)

    # Composite index for fast plan lookup
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_plan_identity
        ON master_index (year, plan_category, plan_type, plan_tier, product_line, variant, network)
    """)

    conn.commit()
    conn.close()
    print("[*] Database ready.")


def build_all():
    is_prod = IS_PRODUCTION

    # ── Dev only ──────────────────────────────────────────────────────────────
    if not is_prod:
        setup_db()
        os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        pdf_sources = _get_local_pdf_sources()
    else:
        conn = None
        print(
            "[*] Production mode — reading PDFs from blob, writing to blob + PostgreSQL."
        )
        pdf_sources = asyncio.run(_get_blob_pdf_sources())

    for pdf_source in pdf_sources:
        pdf_path = pdf_source["local_path"]
        final_plan_category = pdf_source["plan_category"]
        path_year = pdf_source.get("year")
        filename = os.path.basename(pdf_path)

        try:
            print(f"\n[*] Processing: {filename}")

            strategy = BOOKLET_STRATEGIES.get(final_plan_category)
            if not strategy:
                print(f"[!] Unknown folder type '{final_plan_category}' — skipping")
                continue

            classify_fn, generate_index_fn = strategy
            print(f"[*] USING INDEXER: {generate_index_fn.__name__}")

            # ── Classification — use blob metadata if available, else LLM ────
            plan_info = pdf_source.get("plan_info")  # populated from blob metadata
            if plan_info:
                print(
                    f"[*] Using blob metadata for {filename} — skipping LLM classification"
                )
            else:
                plan_info = classify_fn(pdf_path)
                print(f"[*] RAW PLAN INFO: {plan_info}")
                if not plan_info:
                    print(
                        f"[!] Classification failed for {filename} — skipping. Is Ollama running?"
                    )
                    continue

            final_year = path_year or (
                plan_info.get("year")
                if plan_info and plan_info.get("year")
                else CURRENT_YEAR_INT
            )
            final_group_number = (
                str(plan_info.get("group_number", "")).strip() if plan_info else ""
            )
            final_group_name = (
                str(plan_info.get("group_name", "")).strip() if plan_info else ""
            )

            VALID_TYPES = {"HMO", "PPO", "EPO", "HSA", "DENTAL", "VISION"}
            raw_type = (
                str(plan_info.get("type", "")).upper().strip() if plan_info else ""
            )
            final_type = raw_type if raw_type in VALID_TYPES else ""

            raw_tier = str(plan_info.get("tier", "")).strip() if plan_info else ""
            final_tier = (
                raw_tier.capitalize() if raw_tier and raw_tier.lower() != "none" else ""
            )

            raw_plan = str(plan_info.get("plan", "")).strip() if plan_info else ""
            final_plan = (
                raw_plan
                if raw_plan and raw_plan.lower() not in ["plan", "none"]
                else filename.replace(".pdf", "").replace("_", " ").strip().title()
            )

            raw_variant = str(plan_info.get("variant", "")).strip() if plan_info else ""
            final_variant = (
                raw_variant
                if raw_variant and raw_variant.lower() != "none"
                else "Standard"
            )

            raw_network = (
                str(plan_info.get("network", "")).strip()
                if plan_info and plan_info.get("network")
                else ""
            )
            final_network = raw_network if raw_network else ""

            print(
                f"[*] FINAL METADATA:"
                f"\n    Year: {final_year}"
                f"\n    Group #: {final_group_number}"
                f"\n    Group Name: {final_group_name}"
                f"\n    Plan: {final_plan}"
                f"\n    Type: {final_type}"
                f"\n    Tier: {final_tier}"
                f"\n    Variant: {final_variant}"
                f"\n    Network: {final_network}"
            )

            def safe_slug(v):
                return re.sub(r"\W+", "_", str(v).lower()).strip("_")

            filepath_parts = [str(final_year), final_plan_category]
            if final_type:
                filepath_parts.append(safe_slug(final_type))
            if final_tier:
                filepath_parts.append(safe_slug(final_tier))
            if final_group_number:
                filepath_parts.append(safe_slug(final_group_number))
            filepath_parts.append(safe_slug(final_plan))
            if final_variant:
                filepath_parts.append(safe_slug(final_variant))
            if final_network:
                filepath_parts.append(safe_slug(final_network))

            unique_fn = "_".join(filepath_parts) + ".json"
            sub_index_path = os.path.abspath(os.path.join(INDEX_OUTPUT_DIR, unique_fn))
            print(f"[*] SUB INDEX PATH: {sub_index_path}")

            generate_index_fn(sub_index_path, pdf_path)

            if not is_prod:
                # ── Dev: SQLite master index only ─────────────────────────────
                assert conn is not None
                conn.execute(
                    "INSERT OR REPLACE INTO master_index (year,plan_category,group_number,group_name,plan,plan_type,plan_tier,variant,network,pdf_path,sub_index_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        final_year,
                        final_plan_category,
                        final_group_number,
                        final_group_name,
                        final_plan,
                        final_type,
                        final_tier,
                        final_variant,
                        final_network,
                        pdf_path,
                        sub_index_path,
                    ),
                )
                conn.commit()

            else:
                # ── Production: blob + PostgreSQL + Service Bus ───────────────
                asyncio.run(
                    post_index_upload(
                        sub_index_path=sub_index_path,
                        year=str(final_year),
                        plan_category=final_plan_category,
                        plan=final_plan,
                        group_number=final_group_number,
                        group_name=final_group_name,
                        plan_type=final_type,
                        plan_tier=final_tier,
                        product_line="",
                        variant=final_variant,
                        network=final_network,
                    )
                )
                # Clean up temp PDF downloaded from blob
                if pdf_source.get("is_temp") and os.path.exists(pdf_path):
                    os.remove(pdf_path)

            print(f"✅ SUCCESS: {filename} -> {unique_fn}")

        except Exception as e:
            print(f"❌ FAILED {filename}: {e}")

    if not is_prod and conn:
        conn.close()


def _get_local_pdf_sources() -> list:
    """Returns PDF sources from local docs folder (dev only)."""
    sources = []
    doc_path = os.path.abspath(DOC_BASE_DIR)
    print(f"[*] Doc Path: {doc_path}")
    for root, _, files in os.walk(doc_path):
        parts = os.path.normpath(root).lower().split(os.sep)
        path_year = next((int(p) for p in parts if p.isdigit()), None)
        plan_category = next(
            (p for p in parts if p in BOOKLET_STRATEGIES),
            os.path.basename(root).lower(),
        )
        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue
            sources.append(
                {
                    "local_path": os.path.abspath(os.path.join(root, filename)),
                    "plan_category": plan_category,
                    "year": path_year,
                    "is_temp": False,
                    "plan_info": None,  # always run LLM classification in dev
                }
            )
    return sources


async def _get_blob_pdf_sources() -> list:
    """
    Lists PDFs from blob with change detection (production only).
    Skips unchanged PDFs. Downloads changed ones to temp files.
    """
    import tempfile
    from infrastructure.blob_storage import list_pdf_blobs, download_pdf
    from infrastructure.db import get_last_indexed

    blobs = await list_pdf_blobs()
    sources = []

    for blob in blobs:
        blob_name = blob["name"]
        blob_modified = blob["last_modified"]

        parts = blob_name.replace("\\", "/").split("/")
        if len(parts) < 3:
            print(f"[!] Unexpected blob path: {blob_name} — skipping")
            continue

        year = parts[0] if parts[0].isdigit() else None
        plan_category = parts[2].lower() if len(parts) >= 3 else None
        group_number = parts[1] if len(parts) >= 2 else ""

        if not plan_category or plan_category not in BOOKLET_STRATEGIES:
            print(f"[!] Unknown category '{plan_category}' in {blob_name} — skipping")
            continue

        last_indexed = await get_last_indexed(
            year=year or str(CURRENT_YEAR_INT),
            plan_category=plan_category,
            group_number=group_number,
        )

        if last_indexed and last_indexed >= blob_modified:
            print(f"[*] SKIP (unchanged): {blob_name}")
            continue

        print(f"[*] CHANGED (re-indexing): {blob_name}")

        # ── Read blob metadata if available ──────────────────────────────────
        # Metadata is attached by the upload process and eliminates the LLM
        # classification call. Minimum required: year, group_number,
        # plan_category, plan. Everything else is supplementary.
        # TODO: confirm exact metadata key names with the upload team.
        blob_meta = blob.get("metadata", {})
        plan_info = None
        if blob_meta and all(
            k in blob_meta for k in ("year", "group_number", "plan_category", "plan")
        ):
            plan_info = {
                "year": blob_meta.get("year", year),
                "group_number": blob_meta.get("group_number", group_number),
                "group_name": blob_meta.get("group_name", ""),
                "plan_category": blob_meta.get("plan_category", plan_category),
                "plan": blob_meta.get("plan", ""),
                "type": blob_meta.get("plan_type", ""),
                "tier": blob_meta.get("plan_tier", ""),
                "variant": blob_meta.get("variant", "Standard"),
                "network": blob_meta.get("network", ""),
            }
            print(
                f"[*] Blob metadata found — skipping LLM classification for {blob_name}"
            )
        else:
            print(f"[*] No blob metadata — LLM classification will run for {blob_name}")

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        ok = await download_pdf(blob_name, tmp.name)
        if not ok:
            print(f"[!] Failed to download {blob_name} — skipping")
            continue

        sources.append(
            {
                "local_path": tmp.name,
                "blob_name": blob_name,
                "plan_category": plan_category,
                "year": int(year) if year and year.isdigit() else None,
                "is_temp": True,
                "plan_info": plan_info,  # None if no metadata — LLM runs in build_all
            }
        )

    print(f"[*] {len(sources)} PDF(s) to re-index out of {len(blobs)} total")
    return sources


if __name__ == "__main__":
    build_all()

##=========================================Previously working code before Rx indexer addition.=========================================##
# # """
# # Main indexer — entry point for all plan document types.

# # Folder structure determines which indexer is used:
# #     docs/
# #         sbc/          → sbc_indexer   (docling + markdown parsing)
# #         medical/      → medical_indexer (pdfplumber + bold detection)
# #         dental/       → dental_indexer  (add when ready)

# # To add a new booklet type:
# #     1. Create <type>_indexer.py with classify_document() and generate_sub_index()
# #     2. Add its folder name to BOOKLET_STRATEGIES below
# #     3. Import the module
# # """

# # import os
# # from config import settings
# # import sqlite3
# # import json as json_lib
# # import re
# # import asyncio

# # from indexers import sbc_indexer as sbc_indexer, vision_indexer
# # from indexers import medical_indexer as medical_indexer
# # from indexers import dental_indexer as dental_indexer
# # from indexers import vision_indexer as vision_indexer

# # from datetime import datetime
# # from dotenv import load_dotenv

# # load_dotenv()

# # IS_PRODUCTION = settings.is_production

# # from infrastructure.post_index import post_index_upload

# # DOC_BASE_DIR = os.path.abspath("./docs")
# # INDEX_OUTPUT_DIR = "./indices"
# # DB_PATH = os.path.join(os.path.dirname(__file__), "p_insurance_index.db")
# # LOCAL_MODEL = settings.OLLAMA_MODEL
# # CURRENT_YEAR_INT = datetime.now().year

# # # Maps folder name (lowercase) → (classify_fn, generate_index_fn)
# # # Add new booklet types here without touching build_all().
# # BOOKLET_STRATEGIES = {
# #     "sbc": (sbc_indexer.classify_document, sbc_indexer.generate_sub_index),
# #     "medical": (medical_indexer.classify_document, medical_indexer.generate_sub_index),
# #     "dental": (dental_indexer.classify_document, dental_indexer.generate_sub_index),
# #     "vision": (vision_indexer.classify_document, vision_indexer.generate_sub_index),
# # }


# # def setup_db():
# #     conn = sqlite3.connect(DB_PATH)
# #     cursor = conn.cursor()

# #     # Master Index — fast routing by plan identity
# #     cursor.execute("""
# #         CREATE TABLE IF NOT EXISTS master_index (
# #             id INTEGER PRIMARY KEY AUTOINCREMENT,
# #             year INTEGER,
# #             plan_category TEXT,
# #             group_number TEXT,
# #             group_name TEXT,
# #             plan TEXT,
# #             plan_type TEXT,
# #             plan_tier TEXT,
# #             product_line TEXT,
# #             variant TEXT,
# #             network TEXT,
# #             pdf_path TEXT UNIQUE,
# #             sub_index_path TEXT
# #         )
# #     """)

# #     # Composite index for fast plan lookup
# #     cursor.execute("""
# #         CREATE INDEX IF NOT EXISTS idx_plan_identity
# #         ON master_index (year, plan_category, plan_type, plan_tier, product_line, variant, network)
# #     """)

# #     conn.commit()
# #     conn.close()
# #     print("[*] Database ready.")


# # def build_all():
# #     is_prod = IS_PRODUCTION

# #     # ── Dev only ──────────────────────────────────────────────────────────────
# #     if not is_prod:
# #         setup_db()
# #         os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)
# #         conn = sqlite3.connect(DB_PATH)
# #         pdf_sources = _get_local_pdf_sources()
# #     else:
# #         conn = None
# #         print(
# #             "[*] Production mode — reading PDFs from blob, writing to blob + PostgreSQL."
# #         )
# #         pdf_sources = asyncio.run(_get_blob_pdf_sources())

# #     for pdf_source in pdf_sources:
# #         pdf_path = pdf_source["local_path"]
# #         final_plan_category = pdf_source["plan_category"]
# #         path_year = pdf_source.get("year")
# #         filename = os.path.basename(pdf_path)

# #         try:
# #             print(f"\n[*] Processing: {filename}")

# #             strategy = BOOKLET_STRATEGIES.get(final_plan_category)
# #             if not strategy:
# #                 print(f"[!] Unknown folder type '{final_plan_category}' — skipping")
# #                 continue

# #             classify_fn, generate_index_fn = strategy
# #             print(f"[*] USING INDEXER: {generate_index_fn.__name__}")

# #             # ── Classification — use blob metadata if available, else LLM ────
# #             plan_info = pdf_source.get("plan_info")  # populated from blob metadata
# #             if plan_info:
# #                 print(
# #                     f"[*] Using blob metadata for {filename} — skipping LLM classification"
# #                 )
# #             else:
# #                 plan_info = classify_fn(pdf_path)
# #                 print(f"[*] RAW PLAN INFO: {plan_info}")
# #                 if not plan_info:
# #                     print(
# #                         f"[!] Classification failed for {filename} — skipping. Is Ollama running?"
# #                     )
# #                     continue

# #             final_year = path_year or (
# #                 plan_info.get("year")
# #                 if plan_info and plan_info.get("year")
# #                 else CURRENT_YEAR_INT
# #             )
# #             final_group_number = (
# #                 str(plan_info.get("group_number", "")).strip() if plan_info else ""
# #             )
# #             final_group_name = (
# #                 str(plan_info.get("group_name", "")).strip() if plan_info else ""
# #             )

# #             VALID_TYPES = {"HMO", "PPO", "EPO", "HSA", "DENTAL", "VISION"}
# #             raw_type = (
# #                 str(plan_info.get("type", "")).upper().strip() if plan_info else ""
# #             )
# #             final_type = raw_type if raw_type in VALID_TYPES else ""

# #             raw_tier = str(plan_info.get("tier", "")).strip() if plan_info else ""
# #             final_tier = (
# #                 raw_tier.capitalize() if raw_tier and raw_tier.lower() != "none" else ""
# #             )

# #             raw_plan = str(plan_info.get("plan", "")).strip() if plan_info else ""
# #             final_plan = (
# #                 raw_plan
# #                 if raw_plan and raw_plan.lower() not in ["plan", "none"]
# #                 else filename.replace(".pdf", "").replace("_", " ").strip().title()
# #             )

# #             raw_variant = str(plan_info.get("variant", "")).strip() if plan_info else ""
# #             final_variant = (
# #                 raw_variant
# #                 if raw_variant and raw_variant.lower() != "none"
# #                 else "Standard"
# #             )

# #             raw_network = (
# #                 str(plan_info.get("network", "")).strip()
# #                 if plan_info and plan_info.get("network")
# #                 else ""
# #             )
# #             final_network = raw_network if raw_network else ""

# #             print(
# #                 f"[*] FINAL METADATA:"
# #                 f"\n    Year: {final_year}"
# #                 f"\n    Group #: {final_group_number}"
# #                 f"\n    Group Name: {final_group_name}"
# #                 f"\n    Plan: {final_plan}"
# #                 f"\n    Type: {final_type}"
# #                 f"\n    Tier: {final_tier}"
# #                 f"\n    Variant: {final_variant}"
# #                 f"\n    Network: {final_network}"
# #             )

# #             def safe_slug(v):
# #                 return re.sub(r"\W+", "_", str(v).lower()).strip("_")

# #             filepath_parts = [str(final_year), final_plan_category]
# #             if final_type:
# #                 filepath_parts.append(safe_slug(final_type))
# #             if final_tier:
# #                 filepath_parts.append(safe_slug(final_tier))
# #             if final_group_number:
# #                 filepath_parts.append(safe_slug(final_group_number))
# #             filepath_parts.append(safe_slug(final_plan))
# #             if final_variant:
# #                 filepath_parts.append(safe_slug(final_variant))
# #             if final_network:
# #                 filepath_parts.append(safe_slug(final_network))

# #             unique_fn = "_".join(filepath_parts) + ".json"
# #             sub_index_path = os.path.abspath(os.path.join(INDEX_OUTPUT_DIR, unique_fn))
# #             print(f"[*] SUB INDEX PATH: {sub_index_path}")

# #             generate_index_fn(sub_index_path, pdf_path)

# #             if not is_prod:
# #                 # ── Dev: SQLite master index only ─────────────────────────────
# #                 assert conn is not None
# #                 conn.execute(
# #                     "INSERT OR REPLACE INTO master_index (year,plan_category,group_number,group_name,plan,plan_type,plan_tier,variant,network,pdf_path,sub_index_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
# #                     (
# #                         final_year,
# #                         final_plan_category,
# #                         final_group_number,
# #                         final_group_name,
# #                         final_plan,
# #                         final_type,
# #                         final_tier,
# #                         final_variant,
# #                         final_network,
# #                         pdf_path,
# #                         sub_index_path,
# #                     ),
# #                 )
# #                 conn.commit()

# #             else:
# #                 # ── Production: blob + PostgreSQL + Service Bus ───────────────
# #                 asyncio.run(
# #                     post_index_upload(
# #                         sub_index_path=sub_index_path,
# #                         year=str(final_year),
# #                         plan_category=final_plan_category,
# #                         plan=final_plan,
# #                         group_number=final_group_number,
# #                         group_name=final_group_name,
# #                         plan_type=final_type,
# #                         plan_tier=final_tier,
# #                         product_line="",
# #                         variant=final_variant,
# #                         network=final_network,
# #                     )
# #                 )
# #                 # Clean up temp PDF downloaded from blob
# #                 if pdf_source.get("is_temp") and os.path.exists(pdf_path):
# #                     os.remove(pdf_path)

# #             print(f"✅ SUCCESS: {filename} -> {unique_fn}")

# #         except Exception as e:
# #             print(f"❌ FAILED {filename}: {e}")

# #     if not is_prod and conn:
# #         conn.close()


# # def _get_local_pdf_sources() -> list:
# #     """Returns PDF sources from local docs folder (dev only)."""
# #     sources = []
# #     doc_path = os.path.abspath(DOC_BASE_DIR)
# #     print(f"[*] Doc Path: {doc_path}")
# #     for root, _, files in os.walk(doc_path):
# #         parts = os.path.normpath(root).lower().split(os.sep)
# #         path_year = next((int(p) for p in parts if p.isdigit()), None)
# #         plan_category = next(
# #             (p for p in parts if p in BOOKLET_STRATEGIES),
# #             os.path.basename(root).lower(),
# #         )
# #         for filename in files:
# #             if not filename.lower().endswith(".pdf"):
# #                 continue
# #             sources.append(
# #                 {
# #                     "local_path": os.path.abspath(os.path.join(root, filename)),
# #                     "plan_category": plan_category,
# #                     "year": path_year,
# #                     "is_temp": False,
# #                     "plan_info": None,  # always run LLM classification in dev
# #                 }
# #             )
# #     return sources


# # async def _get_blob_pdf_sources() -> list:
# #     """
# #     Lists PDFs from blob with change detection (production only).
# #     Skips unchanged PDFs. Downloads changed ones to temp files.
# #     """
# #     import tempfile
# #     from infrastructure.blob_storage import list_pdf_blobs, download_pdf
# #     from infrastructure.db import get_last_indexed

# #     blobs = await list_pdf_blobs()
# #     sources = []

# #     for blob in blobs:
# #         blob_name = blob["name"]
# #         blob_modified = blob["last_modified"]

# #         parts = blob_name.replace("\\", "/").split("/")
# #         if len(parts) < 3:
# #             print(f"[!] Unexpected blob path: {blob_name} — skipping")
# #             continue

# #         year = parts[0] if parts[0].isdigit() else None
# #         plan_category = parts[2].lower() if len(parts) >= 3 else None
# #         group_number = parts[1] if len(parts) >= 2 else ""

# #         if not plan_category or plan_category not in BOOKLET_STRATEGIES:
# #             print(f"[!] Unknown category '{plan_category}' in {blob_name} — skipping")
# #             continue

# #         last_indexed = await get_last_indexed(
# #             year=year or str(CURRENT_YEAR_INT),
# #             plan_category=plan_category,
# #             group_number=group_number,
# #         )

# #         if last_indexed and last_indexed >= blob_modified:
# #             print(f"[*] SKIP (unchanged): {blob_name}")
# #             continue

# #         print(f"[*] CHANGED (re-indexing): {blob_name}")

# #         # ── Read blob metadata if available ──────────────────────────────────
# #         # Metadata is attached by the upload process and eliminates the LLM
# #         # classification call. Minimum required: year, group_number,
# #         # plan_category, plan. Everything else is supplementary.
# #         # TODO: confirm exact metadata key names with the upload team.
# #         blob_meta = blob.get("metadata", {})
# #         plan_info = None
# #         if blob_meta and all(
# #             k in blob_meta for k in ("year", "group_number", "plan_category", "plan")
# #         ):
# #             plan_info = {
# #                 "year": blob_meta.get("year", year),
# #                 "group_number": blob_meta.get("group_number", group_number),
# #                 "group_name": blob_meta.get("group_name", ""),
# #                 "plan_category": blob_meta.get("plan_category", plan_category),
# #                 "plan": blob_meta.get("plan", ""),
# #                 "type": blob_meta.get("plan_type", ""),
# #                 "tier": blob_meta.get("plan_tier", ""),
# #                 "variant": blob_meta.get("variant", "Standard"),
# #                 "network": blob_meta.get("network", ""),
# #             }
# #             print(
# #                 f"[*] Blob metadata found — skipping LLM classification for {blob_name}"
# #             )
# #         else:
# #             print(f"[*] No blob metadata — LLM classification will run for {blob_name}")

# #         tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
# #         tmp.close()
# #         ok = await download_pdf(blob_name, tmp.name)
# #         if not ok:
# #             print(f"[!] Failed to download {blob_name} — skipping")
# #             continue

# #         sources.append(
# #             {
# #                 "local_path": tmp.name,
# #                 "blob_name": blob_name,
# #                 "plan_category": plan_category,
# #                 "year": int(year) if year and year.isdigit() else None,
# #                 "is_temp": True,
# #                 "plan_info": plan_info,  # None if no metadata — LLM runs in build_all
# #             }
# #         )

# #     print(f"[*] {len(sources)} PDF(s) to re-index out of {len(blobs)} total")
# #     return sources


# # if __name__ == "__main__":
# #     build_all()
