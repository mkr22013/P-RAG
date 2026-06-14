"""
infrastructure/post_index.py
────────────────────────────────────────────────────────────────────────────
Shared helper called after any indexer writes a JSON index file locally.

Handles the production cloud steps:
    1. Upload JSON index to Azure Blob Storage
    2. Upsert entry in PostgreSQL master index
    3. Send cache invalidation message to Service Bus

Blob path is derived from plan attributes — not from local file path.
Structure: indices/{year}/{group_number}/{plan_category}/{plan_category}_{plan_type}_{variant}.json

All steps are no-ops locally when Azure env vars are not set.
"""

import os
from config import settings
import json
import logging
import re

logger = logging.getLogger(__name__)


def build_blob_path(
    year: str,
    group_number: str,
    plan_category: str,
    plan: str = "",
    plan_type: str = "",
    variant: str = "",
) -> str:
    """
    Derives the blob storage index path mirroring the PDF blob structure.

    PDF path:   {year}/{group_number}/{plan_category}/{plan_name}.pdf
    Index path: {year}/{group_number}/{plan_category}/{plan_name}_index.json

    Examples:
        PDF:   2026/1000016/medical/Premera_Employees_PPO_Retiree.pdf
        Index: 2026/1000016/medical/Premera_Employees_PPO_Retiree_index.json

        PDF:   2026/1000016/dental/Willamette_Dental.pdf
        Index: 2026/1000016/dental/Willamette_Dental_index.json
    """

    def _slug(val: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", (val or "").lower()).strip("_")

    # Use plan name as filename base — same as PDF filename
    # Fall back to plan_category + plan_type + variant if plan name not available
    if plan:
        filename = _slug(plan) + "_index.json"
    else:
        parts = [_slug(plan_category)]
        if plan_type:
            parts.append(_slug(plan_type))
        if variant:
            parts.append(_slug(variant))
        filename = "_".join(parts) + "_index.json"

    return f"{year}/{group_number}/{plan_category}/{filename}"


async def post_index_upload(
    sub_index_path: str,
    year: str,
    plan_category: str,
    plan: str,
    group_number: str,
    group_name: str,
    plan_type: str = "",
    plan_tier: str = "",
    product_line: str = "",
    variant: str = "",
    network: str = "",
) -> bool:
    """
    Runs the production cloud steps after a JSON index file has been
    written to local disk.

    Returns True if all steps succeeded (or were skipped locally).
    Returns False if any production step failed.

    Local dev (AZURE_BLOB_CONNECTION_STRING not set):
        Logs a message and returns True immediately. No cloud calls made.
    """
    if not settings.AZURE_BLOB_CONNECTION_STRING:
        logger.info(
            "[post_index] Local dev — skipping cloud upload for %s",
            sub_index_path,
        )
        return True

    from infrastructure.blob_storage import upload_index
    from infrastructure.db import upsert_index_entry
    from infrastructure.service_bus import send_cache_invalidation
    from infrastructure.cache import make_redis_key

    # Derive blob path from plan attributes — not from local filename
    blob_path = build_blob_path(
        year=year,
        group_number=group_number,
        plan_category=plan_category,
        plan=plan,
        plan_type=plan_type,
        variant=variant,
    )

    # Build Redis key
    redis_key = make_redis_key(
        year,
        plan_category,
        group_number,
        variant or "standard",
    )

    # Step 1: read local JSON and upload to blob
    try:
        with open(sub_index_path, encoding="utf-8") as f:
            chunks = json.load(f)
    except Exception as exc:
        logger.error(
            "[post_index] Failed to read local index %s: %s", sub_index_path, exc
        )
        return False

    etag = await upload_index(blob_path, chunks)
    if not etag:
        logger.error("[post_index] Blob upload failed for %s", blob_path)
        return False

    logger.info("[post_index] Uploaded to blob: %s", blob_path)

    # Step 2: upsert PostgreSQL master index
    pg_ok = await upsert_index_entry(
        year=year,
        plan_category=plan_category,
        plan=plan,
        group_number=group_number,
        group_name=group_name,
        plan_type=plan_type,
        plan_tier=plan_tier,
        product_line=product_line,
        variant=variant,
        network=network,
        blob_path=blob_path,
        redis_key=redis_key,
        blob_etag=etag,
    )
    if not pg_ok:
        logger.error("[post_index] PostgreSQL upsert failed for %s", blob_path)
        return False

    logger.info(
        "[post_index] PostgreSQL updated: %s / %s / %s",
        year,
        plan_category,
        group_number,
    )

    # Step 3: send cache invalidation message
    sb_ok = await send_cache_invalidation(
        redis_key=redis_key,
        blob_path=blob_path,
        reason="reindexed",
    )
    if not sb_ok:
        logger.warning(
            "[post_index] Service Bus message failed for %s — "
            "cache not invalidated but data is consistent in blob and PostgreSQL",
            redis_key,
        )

    logger.info("[post_index] Cache invalidation sent: %s", redis_key)
    return True
