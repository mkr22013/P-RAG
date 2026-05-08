"""
Shared utilities used by all booklet indexers (SBC, Medical, Dental, etc.)
"""
import re
import json as json_lib


def get_smart_keywords(text):
    """
    Extract up to 10 keywords from a chunk of text or dict content.
    First matches known insurance domain patterns, then falls back to
    any word with 7+ characters to fill remaining slots.
    """
    if isinstance(text, dict):
        text = json_lib.dumps(text)

    text_lower = text.lower()
    patterns = {
        "pcp":           r"\bpcp\b|primary[- ]?care",
        "specialist":    r"specialist",
        "in-network":    r"in[- ]?network",
        "out-of-network":r"out[- ]?of[- ]?network",
        "copay":         r"co[- ]?pay|copay",
        "deductible":    r"deductible",
        "coinsurance":   r"co[- ]?insurance",
        "emergency":     r"emergency|medical[- ]?attention",
        "urgent-care":   r"urgent[- ]?care",
        "pharmacy":      r"pharmacy|prescription|rx",
        "dental":        r"dental|dentist|ortho|braces",
        "vision":        r"vision|eye|glasses",
        "imaging":       r"imaging|mri|ct\s?scan|pet\s?scan",
        "diagnostic":    r"diagnostic|x-ray|blood\s?work",
        "mental-health": r"mental|behavioral|substance|abuse",
        "therapy":       r"rehab|physical|speech|occupational",
    }
    found = [label for label, pat in patterns.items() if re.search(pat, text_lower)]

    if len(found) < 10:
        for word in re.findall(r"\b\w{7,}\b", text_lower):
            if word not in found and len(found) < 10:
                found.append(word)

    return found[:10]