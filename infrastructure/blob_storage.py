"""
infrastructure/blob_storage.py
────────────────────────────────────────────────────────────────────────────
Azure Blob Storage client for reading PDFs and JSON index files.

Environment variables:
    AZURE_BLOB_CONNECTION_STRING  — Azure Storage connection string
    AZURE_BLOB_PDF_CONTAINER      — container for source PDFs (default: insurance-pdfs)
    AZURE_BLOB_INDEX_CONTAINER    — container for JSON indices (default: insurance-indices)

Local dev fallback:
    When AZURE_BLOB_CONNECTION_STRING is not set, all operations
    fall back to local filesystem. Zero impact on local development.
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

AZURE_BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING", "")
PDF_CONTAINER = os.getenv("AZURE_BLOB_PDF_CONTAINER", "insurance-pdfs")
INDEX_CONTAINER = os.getenv("AZURE_BLOB_INDEX_CONTAINER", "insurance-indices")

# ── Blob client (lazy init) ───────────────────────────────────────────────────
_blob_service_client = None


def _get_blob_service_client():
    global _blob_service_client
    if _blob_service_client is None:
        from azure.storage.blob import BlobServiceClient
        _blob_service_client = BlobServiceClient.from_connection_string(
            AZURE_BLOB_CONNECTION_STRING
        )
        logger.info("[blob] BlobServiceClient initialised.")
    return _blob_service_client


# ── Public API ─────────────────────────────────────────────────────────────────

async def download_index(blob_path: str) -> Optional[list]:
    """
    Downloads and parses a JSON index file from blob storage.

    Parameters:
        blob_path — path within the index container, e.g.
                    "2026/1000016/medical_ppo_retiree.json"

    Returns list of chunk dicts, or None on failure.

    Local dev fallback:
        When AZURE_BLOB_CONNECTION_STRING is not set, reads from local
        filesystem path directly (blob_path treated as absolute path).
    """
    if not AZURE_BLOB_CONNECTION_STRING:
        # Local dev — read from filesystem
        if os.path.exists(blob_path):
            try:
                with open(blob_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("[blob] Local file read failed %s: %s", blob_path, exc)
                return None
        logger.warning("[blob] Local file not found: %s", blob_path)
        return None

    try:
        client = _get_blob_service_client()
        blob_client = client.get_blob_client(
            container=INDEX_CONTAINER,
            blob=blob_path,
        )
        data = blob_client.download_blob().readall()
        chunks = json.loads(data.decode("utf-8"))
        logger.info("[blob] Downloaded index: %s (%d chunks)", blob_path, len(chunks))
        return chunks
    except Exception as exc:
        logger.error("[blob] download_index failed for %s: %s", blob_path, exc)
        return None


async def upload_index(blob_path: str, chunks: list) -> Optional[str]:
    """
    Uploads a JSON index file to blob storage.

    Parameters:
        blob_path — destination path within index container
        chunks    — list of chunk dicts to serialise and upload

    Returns the blob etag on success, None on failure.

    Local dev fallback:
        Writes to local filesystem path.
    """
    content = json.dumps(chunks, ensure_ascii=False, indent=2)

    if not AZURE_BLOB_CONNECTION_STRING:
        # Local dev — write to filesystem
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        try:
            with open(blob_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("[blob] Written locally: %s", blob_path)
            return "local"
        except Exception as exc:
            logger.error("[blob] Local file write failed %s: %s", blob_path, exc)
            return None

    try:
        client = _get_blob_service_client()
        blob_client = client.get_blob_client(
            container=INDEX_CONTAINER,
            blob=blob_path,
        )
        result = blob_client.upload_blob(
            content.encode("utf-8"),
            overwrite=True,
            content_settings=None,
        )
        etag = result.get("etag", "")
        logger.info("[blob] Uploaded index: %s (etag=%s)", blob_path, etag)
        return etag
    except Exception as exc:
        logger.error("[blob] upload_index failed for %s: %s", blob_path, exc)
        return None


async def get_blob_last_modified(blob_path: str, container: Optional[str] = None) -> Optional[str]:
    """
    Returns the last_modified timestamp of a blob as ISO string.
    Used by indexer to check if a PDF has changed since last indexing.

    Returns None if blob doesn't exist or on error.
    """
    if not AZURE_BLOB_CONNECTION_STRING:
        # Local dev — use file mtime
        if os.path.exists(blob_path):
            import datetime
            mtime = os.path.getmtime(blob_path)
            return datetime.datetime.utcfromtimestamp(mtime).isoformat()
        return None

    try:
        client = _get_blob_service_client()
        blob_client = client.get_blob_client(
            container=container or PDF_CONTAINER,
            blob=blob_path,
        )
        props = blob_client.get_blob_properties()
        return props.last_modified.isoformat()
    except Exception as exc:
        logger.error("[blob] get_blob_last_modified failed for %s: %s", blob_path, exc)
        return None


async def download_pdf(blob_path: str, local_temp_path: str) -> bool:
    """
    Downloads a PDF from blob storage to a local temp path for indexing.

    Parameters:
        blob_path       — source path in PDF container
        local_temp_path — destination local path for indexer to read

    Returns True on success, False on failure.

    Local dev fallback:
        blob_path is already a local path — just confirms it exists.
    """
    if not AZURE_BLOB_CONNECTION_STRING:
        return os.path.exists(blob_path)

    try:
        client = _get_blob_service_client()
        blob_client = client.get_blob_client(
            container=PDF_CONTAINER,
            blob=blob_path,
        )
        os.makedirs(os.path.dirname(local_temp_path), exist_ok=True)
        with open(local_temp_path, "wb") as f:
            f.write(blob_client.download_blob().readall())
        logger.info("[blob] Downloaded PDF: %s → %s", blob_path, local_temp_path)
        return True
    except Exception as exc:
        logger.error("[blob] download_pdf failed for %s: %s", blob_path, exc)
        return False