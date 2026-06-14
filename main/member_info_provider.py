import os
import urllib.request
import urllib.parse
import json as _json

MEMBER_INFO_API_URL = os.getenv("MEMBER_INFO_API_URL", "")


def get_member_info(member_key: str = "", group_number: str = "") -> dict:
    """
    Returns member plan info.

    Parameters:
        member_key   — prefix+identification+suffix from insurance card or auth token
        group_number — group number from insurance card or auth token

    Currently returns hardcoded data when no external API is configured.
    When MEMBER_INFO_API_URL is set, calls external API with member_key + group_number.
    Return shape must remain stable — only content changes.
    """

    if MEMBER_INFO_API_URL and member_key:
        try:
            params = urllib.parse.urlencode(
                {
                    "member_key": member_key,
                    "group_number": group_number,
                }
            )
            url = f"{MEMBER_INFO_API_URL}?{params}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            print(f"[!] Member info API failed: {e} — falling back to hardcoded")

    return {
        "year": "2026",
        "member_key": member_key,
        "group_number": group_number or "1000016",
        "plans": {
            "medical": {
                "plan_category": "medical",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees Health Plan",
                "plan": "Premera Employees Health Plan – Standard PPO Retiree Plan",
                "plan_type": "PPO",
                "plan_tier": "",
                "product_line": "Null",
                "variant": "Retiree",
                "network": "",
                "page_offset": 4,
            },
            "dental": {
                "plan_category": "dental",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees Health Plan",
                "plan": "Willamette Dental Plan",
                "plan_type": "",
                "plan_tier": "",
                "product_line": "Null",
                "variant": "Standard",
                "network": "",
                "page_offset": 5,
            },
            # "dental": {
            #     "plan_category": "dental",
            #     "group_number": group_number or "1000016",
            #     "group_name": "Premera Employees Health Plan",
            #     "plan": "Premera Dental Plan",
            #     "plan_type": "",
            #     "plan_tier": "",
            #     "product_line": "Null",
            #     "variant": "Standard",
            #     "network": "",
            #     "page_offset": 5,
            # },
            "vision": {
                "plan_category": "vision",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees Health Plan",
                "plan": "Vision Plan",
                "plan_type": "",
                "plan_tier": "",
                "product_line": "Null",
                "variant": "Standard",
                "network": "",
                "page_offset": 6,
            },
            "sbc": {
                "plan_category": "sbc",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees",
                "plan": "",
                "plan_type": "PPO",
                "plan_tier": "",
                "product_line": "Your Future HSA Qualified Agg NGF - SF",
                "variant": "Standard",
                "network": "",
            },
            "rx": {
                "plan_category": "rx",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees Health Plan",
                "plan": "Essentials Formulary Drug List",
                "plan_type": "",
                "plan_tier": "",
                "product_line": "",
                "variant": "E4",  # from member insurance card: Rx Formulary E4
                "network": "",
            },
        },
    }


# # ===================================================Previous working version before adding Rx related changes=================================================

# # import os
# # from config import settings
# # import logging
# # import time
# # from typing import Optional

# # import httpx
# # from tenacity import (
# #     retry,
# #     stop_after_attempt,
# #     wait_exponential,
# #     retry_if_exception_type,
# #     before_sleep_log,
# # )

# # logger = logging.getLogger(__name__)

# # # ── Configuration from environment ────────────────────────────────────────────
# # MEMBER_INFO_API_URL = settings.MEMBER_INFO_API_URL
# # API_CONNECT_TIMEOUT = settings.MEMBER_API_CONNECT_TIMEOUT
# # API_READ_TIMEOUT = settings.MEMBER_API_READ_TIMEOUT
# # API_MAX_RETRIES = settings.MEMBER_API_MAX_RETRIES
# # API_RETRY_MIN_WAIT = settings.MEMBER_API_RETRY_MIN_WAIT
# # API_RETRY_MAX_WAIT = settings.MEMBER_API_RETRY_MAX_WAIT

# # # Shared async client — connection pooling, reused across requests
# # _client: Optional[httpx.AsyncClient] = None


# # def _get_client() -> httpx.AsyncClient:
# #     global _client
# #     if _client is None or _client.is_closed:
# #         _client = httpx.AsyncClient(
# #             timeout=httpx.Timeout(
# #                 connect=API_CONNECT_TIMEOUT,
# #                 read=API_READ_TIMEOUT,
# #                 write=5.0,
# #                 pool=5.0,
# #             ),
# #             limits=httpx.Limits(
# #                 max_connections=20,
# #                 max_keepalive_connections=10,
# #                 keepalive_expiry=30,
# #             ),
# #             headers={"Content-Type": "application/json"},
# #         )
# #     return _client


# # async def close_client() -> None:
# #     """Call on application shutdown to cleanly close the connection pool."""
# #     global _client
# #     if _client and not _client.is_closed:
# #         await _client.aclose()
# #         _client = None


# # def _is_retryable(exc: BaseException) -> bool:
# #     if isinstance(exc, httpx.TransportError):
# #         return True
# #     if isinstance(exc, httpx.HTTPStatusError):
# #         return exc.response.status_code >= 500
# #     return False


# # @retry(
# #     stop=stop_after_attempt(API_MAX_RETRIES),
# #     wait=wait_exponential(
# #         multiplier=1,
# #         min=API_RETRY_MIN_WAIT,
# #         max=API_RETRY_MAX_WAIT,
# #     ),
# #     retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
# #     before_sleep=before_sleep_log(logger, logging.WARNING),
# #     reraise=True,
# # )
# # async def _api_get(url: str, params: dict) -> dict:
# #     client = _get_client()
# #     start = time.monotonic()
# #     try:
# #         response = await client.get(url, params=params)
# #         duration_ms = int((time.monotonic() - start) * 1000)
# #         logger.info(
# #             "[member_info_provider] GET %s status=%d duration=%dms",
# #             url,
# #             response.status_code,
# #             duration_ms,
# #         )
# #         response.raise_for_status()
# #         return response.json()
# #     except httpx.HTTPStatusError as exc:
# #         logger.error(
# #             "[member_info_provider] HTTP error %d for %s",
# #             exc.response.status_code,
# #             url,
# #         )
# #         raise
# #     except httpx.TransportError as exc:
# #         logger.error("[member_info_provider] Transport error for %s: %s", url, exc)
# #         raise


# # async def get_member_info(member_key: str = "", group_number: str = "") -> dict:
# #     """
# #     Returns member plan info.
# #     Calls external API when MEMBER_INFO_API_URL is configured.
# #     Falls back to hardcoded data for local development.
# #     """
# #     if MEMBER_INFO_API_URL and member_key:
# #         try:
# #             return await _api_get(
# #                 MEMBER_INFO_API_URL,
# #                 {"member_key": member_key, "group_number": group_number},
# #             )
# #         except Exception as exc:
# #             logger.error(
# #                 "[member_info_provider] get_member_info failed for key=%s: %s "
# #                 "— falling back to hardcoded",
# #                 member_key,
# #                 exc,
# #             )

# #     return {
# #         "year": "2026",
# #         "member_key": member_key,
# #         "group_number": group_number or "1000016",
# #         "plans": {
# #             "medical": {
# #                 "plan_category": "medical",
# #                 "group_number": group_number or "1000016",
# #                 "group_name": "Premera Employees Health Plan",
# #                 "plan": "Premera Employees Health Plan \u2013 Standard PPO Retiree Plan",
# #                 "plan_type": "PPO",
# #                 "plan_tier": "",
# #                 "product_line": "Null",
# #                 "variant": "Retiree",
# #                 "network": "",
# #                 "page_offset": 4,
# #             },
# #             "dental": {
# #                 "plan_category": "dental",
# #                 "group_number": group_number or "1000016",
# #                 "group_name": "Premera Employees Health Plan",
# #                 "plan": "Willamette Dental Plan",
# #                 "plan_type": "",
# #                 "plan_tier": "",
# #                 "product_line": "Null",
# #                 "variant": "Standard",
# #                 "network": "",
# #                 "page_offset": 5,
# #             },
# #             # "dental": {
# #             #     "plan_category": "dental",
# #             #     "group_number": group_number or "1000016",
# #             #     "group_name": "Premera Employees Health Plan",
# #             #     "plan": "Premera Dental Plan",
# #             #     "plan_type": "",
# #             #     "plan_tier": "",
# #             #     "product_line": "Null",
# #             #     "variant": "Standard",
# #             #     "network": "",
# #             #     "page_offset": 5,
# #             # },
# #             "vision": {
# #                 "plan_category": "vision",
# #                 "group_number": group_number or "1000016",
# #                 "group_name": "Premera Employees Health Plan",
# #                 "plan": "Vision Plan",
# #                 "plan_type": "",
# #                 "plan_tier": "",
# #                 "product_line": "Null",
# #                 "variant": "Standard",
# #                 "network": "",
# #                 "page_offset": 6,
# #             },
# #             "sbc": {
# #                 "plan_category": "sbc",
# #                 "group_number": group_number or "1000016",
# #                 "group_name": "Premera Employees",
# #                 "plan": "",
# #                 "plan_type": "PPO",
# #                 "plan_tier": "",
# #                 "product_line": "Your Future HSA Qualified Agg NGF - SF",
# #                 "variant": "Standard",
# #                 "network": "",
# #             },
# #         },
# #     }


# # async def validate_dependent(
# #     scanned_member_key: str,
# #     group_number: str,
# # ) -> Optional[dict]:
# #     """
# #     Validates whether a scanned card belongs to a dependent of the primary member.

# #     Returns dict with shape:
# #         {
# #             "dependent":      <member info — same shape as get_member_info>,
# #             "primary_holder": { "member_key": str }
# #         }
# #     Returns None if not found or API unavailable.

# #     Caller checks: result["primary_holder"]["member_key"] in memberKeys
# #     """
# #     if not MEMBER_INFO_API_URL:
# #         logger.warning(
# #             "[member_info_provider] validate_dependent: MEMBER_INFO_API_URL "
# #             "not configured — returning None (stub mode)"
# #         )
# #         return None

# #     try:
# #         return await _api_get(
# #             f"{MEMBER_INFO_API_URL}/validate-dependent",
# #             {
# #                 "scanned_member_key": scanned_member_key,
# #                 "group_number": group_number,
# #             },
# #         )
# #     except httpx.HTTPStatusError as exc:
# #         if exc.response.status_code == 404:
# #             logger.info(
# #                 "[member_info_provider] validate_dependent: key=%s not found",
# #                 scanned_member_key,
# #             )
# #             return None
# #         logger.error("[member_info_provider] validate_dependent HTTP error: %s", exc)
# #         return None
# #     except Exception as exc:
# #         logger.error("[member_info_provider] validate_dependent error: %s", exc)
# #         return None
