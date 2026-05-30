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
            },
            # "dental": {
            #     "plan_category": "dental",
            #     "group_number": group_number or "1000016",
            #     "group_name": "Premera Employees Health Plan",
            #     "plan": "Willamette Dental Plan",
            #     "plan_type": "",
            #     "plan_tier": "",
            #     "product_line": "Null",
            #     "variant": "Standard",
            #     "network": "",
            # },
            "dental": {
                "plan_category": "dental",
                "group_number": group_number or "1000016",
                "group_name": "Premera Employees Health Plan",
                "plan": "Premera Dental Plan",
                "plan_type": "",
                "plan_tier": "",
                "product_line": "Null",
                "variant": "Standard",
                "network": "",
            },
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
        },
    }
