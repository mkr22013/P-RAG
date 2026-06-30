"""
Shared utilities used by all booklet indexers (SBC, Medical, Dental, etc.)
"""

import re
import json as json_lib
from difflib import SequenceMatcher


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
        "pcp": r"\bpcp\b|primary[- ]?care",
        "specialist": r"specialist",
        "in-network": r"in[- ]?network",
        "out-of-network": r"out[- ]?of[- ]?network",
        "copay": r"co[- ]?pay|copay",
        "deductible": r"deductible",
        "coinsurance": r"co[- ]?insurance",
        "emergency": r"emergency|medical[- ]?attention",
        "urgent-care": r"urgent[- ]?care",
        "pharmacy": r"pharmacy|prescription|rx",
        "dental": r"dental|dentist|ortho|braces",
        "vision": r"vision|eye|glasses",
        "imaging": r"imaging|mri|ct\s?scan|pet\s?scan",
        "diagnostic": r"diagnostic|x-ray|blood\s?work|blood\s?products|\bblood\b",
        "mental-health": r"mental|behavioral|substance|abuse",
        "therapy": r"rehab|physical|speech|occupational",
    }
    found = [label for label, pat in patterns.items() if re.search(pat, text_lower)]

    if len(found) < 10:
        for word in re.findall(r"\b\w{7,}\b", text_lower):
            if word not in found and len(found) < 10:
                found.append(word)

    return found[:10]


# Single source of truth for noise/generic words.
# Used by both residual keyword extraction (topic_resolver)
# and LLM keyword cleaning (client).
NOISE_WORDS = {
    "what",
    "which",
    "how",
    "does",
    "do",
    "did",
    "is",
    "are",
    "was",
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "from",
    "by",
    "with",
    "that",
    "this",
    "can",
    "will",
    "would",
    "should",
    "could",
    "my",
    "your",
    "our",
    "its",
    "under",
    "about",
    "tell",
    "know",
    "want",
    "need",
    "get",
    "show",
    "covered",
    "coverage",
    "cover",
    "covers",
    "plan",
    "plans",
    "benefit",
    "benefits",
    "service",
    "services",
    "cost",
    "costs",
    "price",
    "fee",
    "amount",
    "amounts",
    "pay",
    "paying",
    "charge",
    "charges",
    "any",
    "some",
    "all",
    "option",
    "options",
    "information",
    "info",
    "detail",
    "details",
    "type",
    "types",
    "kind",
    "kinds",
    "question",
    "much",
    "many",
    "more",
    "have",
    "that",
    "them",
    "they",
    "been",
    "when",
    "where",
    "there",
    "teeth",
    "tooth",
    "treatment",
    "therapy",
    "procedure",
    "happens",
    "dentist",
    "program",
    "programs",
    "care",
    "health",
    "insurance",
    "policy",
    "related",
    "general",
    "standard",
    "other",
    "various",
    "max",
    "min",
    "per",
    "dental",
    "medical",
    "vision",
    "overview",
    "summary",
    "understand",
    "understanding",
    "communication",
    "saying",
    "asking",
    "talking",
    "explain",
    "meaning",
    "answer",
    "getting",
    "right",
    "wrong",
    "think",
    "feel",
    "know",
    "just",
    "really",
    "actually",
    "mean",
    "whats",
    "what" "wat",
    "unknown",
    "during",
    "stay",
    "provide",
    "provides",
    "provided",
    "use",
    "using",
    "give",
    "given",
    "make",
    "made",
    "take",
    "taken",
    "include",
    "includes",
    "included",
    "available",
    "apply",
    "applies",
    "applicable",
}


# Rx-specific noise words — words that are common in Rx queries but carry
# NO drug-name signal. Filtered from rx_keywords (client.py) AND from
# strong_query_words scoring against Rx chunk content (tools.py), so both
# layers stay in sync. This is the single source of truth — adding a word
# here automatically protects BOTH the keyword-extraction path in client.py
# and the chunk-scoring path in tools.py from the same false-positive class.
#
# Found via a real bug: "I want to know about my preventive drugs?" matched
# almost EVERY Rx chunk because "drugs" (plural) soft-matched against every
# drug's drug_category/drug_subcategory field (e.g. "...Immunosuppressant
# DRUGS"), since tools.py scored raw query_words independently from
# client.py's already-filtered rx_keywords — two separate filtering systems
# that had silently diverged. This shared constant closes that gap.
RX_NOISE_WORDS = {
    "what",
    "tier",
    "tiers",
    "covered",
    "cost",
    "much",
    "show",
    "does",
    "plan",
    "cover",
    "drug",
    "drugs",
    "prescription",
    "medication",
    "formulary",
    "generic",
    "brand",
    "pharmacy",
    "refill",
    "mean",
    "means",
    "explain",
    "definition",
    "list",
    "benefits",
    "need",
    "needs",
    "require",
    "requires",
    "requirement",
    "prior",
    "authorization",
    "step",
    "therapy",
    "quantity",
    "limit",
    "specialty",
    "under",
    "your",
    "this",
    # Conversational filler words — carry no drug-name signal
    "want",
    "know",
    "about",
    "tell",
    "please",
    "could",
    "would",
    "like",
    "they",
    "them",
    "have",
    "give",
    "more",
    "information",
    "details",
    "find",
    "looking",
    # General concept words also in GENERAL_RX_TERMS — these
    # signal an info question, not a drug name, so they
    # should never survive into rx_keywords either
    "preventive",
    "exception",
    "optional",
    "chemotherapy",
}


def fuzzy_match(a, b, threshold=0.8):
    return SequenceMatcher(None, a, b).ratio() >= threshold


def smart_match(term, query_words, query_lower):
    """
    Priority:
    1. Exact phrase match
    2. Exact word match
    3. Fuzzy match (safe)
    """
    term = term.lower()

    if " " in term:
        return term in query_lower

    if term in query_words:
        return True

    for w in query_words:
        if len(w) >= 4 and len(term) >= 4:
            if fuzzy_match(w, term):
                return True

    return False


def flatten_message_content(content):
    """
    Forces any Ollama response (List, Dict, or None) into a plain string.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return " ".join(parts).strip()
    return str(content).strip()


# # ================================Previously working code==============================================
# # """
# # Shared utilities used by all booklet indexers (SBC, Medical, Dental, etc.)
# # """

# # import re
# # import json as json_lib
# # from difflib import SequenceMatcher


# # def get_smart_keywords(text):
# #     """
# #     Extract up to 10 keywords from a chunk of text or dict content.
# #     First matches known insurance domain patterns, then falls back to
# #     any word with 7+ characters to fill remaining slots.
# #     """
# #     if isinstance(text, dict):
# #         text = json_lib.dumps(text)

# #     text_lower = text.lower()
# #     patterns = {
# #         "pcp": r"\bpcp\b|primary[- ]?care",
# #         "specialist": r"specialist",
# #         "in-network": r"in[- ]?network",
# #         "out-of-network": r"out[- ]?of[- ]?network",
# #         "copay": r"co[- ]?pay|copay",
# #         "deductible": r"deductible",
# #         "coinsurance": r"co[- ]?insurance",
# #         "emergency": r"emergency|medical[- ]?attention",
# #         "urgent-care": r"urgent[- ]?care",
# #         "pharmacy": r"pharmacy|prescription|rx",
# #         "dental": r"dental|dentist|ortho|braces",
# #         "vision": r"vision|eye|glasses",
# #         "imaging": r"imaging|mri|ct\s?scan|pet\s?scan",
# #         "diagnostic": r"diagnostic|x-ray|blood\s?work|blood\s?products|\bblood\b",
# #         "mental-health": r"mental|behavioral|substance|abuse",
# #         "therapy": r"rehab|physical|speech|occupational",
# #     }
# #     found = [label for label, pat in patterns.items() if re.search(pat, text_lower)]

# #     if len(found) < 10:
# #         for word in re.findall(r"\b\w{7,}\b", text_lower):
# #             if word not in found and len(found) < 10:
# #                 found.append(word)

# #     return found[:10]


# # # Single source of truth for noise/generic words.
# # # Used by both residual keyword extraction (topic_resolver)
# # # and LLM keyword cleaning (client).
# # NOISE_WORDS = {
# #     "what",
# #     "which",
# #     "how",
# #     "does",
# #     "do",
# #     "did",
# #     "is",
# #     "are",
# #     "was",
# #     "the",
# #     "a",
# #     "an",
# #     "and",
# #     "or",
# #     "but",
# #     "in",
# #     "on",
# #     "at",
# #     "to",
# #     "for",
# #     "of",
# #     "from",
# #     "by",
# #     "with",
# #     "that",
# #     "this",
# #     "can",
# #     "will",
# #     "would",
# #     "should",
# #     "could",
# #     "my",
# #     "your",
# #     "our",
# #     "its",
# #     "under",
# #     "about",
# #     "tell",
# #     "know",
# #     "want",
# #     "need",
# #     "get",
# #     "show",
# #     "covered",
# #     "coverage",
# #     "cover",
# #     "covers",
# #     "plan",
# #     "plans",
# #     "benefit",
# #     "benefits",
# #     "service",
# #     "services",
# #     "cost",
# #     "costs",
# #     "price",
# #     "fee",
# #     "amount",
# #     "amounts",
# #     "pay",
# #     "paying",
# #     "charge",
# #     "charges",
# #     "any",
# #     "some",
# #     "all",
# #     "option",
# #     "options",
# #     "information",
# #     "info",
# #     "detail",
# #     "details",
# #     "type",
# #     "types",
# #     "kind",
# #     "kinds",
# #     "question",
# #     "much",
# #     "many",
# #     "more",
# #     "have",
# #     "that",
# #     "them",
# #     "they",
# #     "been",
# #     "when",
# #     "where",
# #     "there",
# #     "teeth",
# #     "tooth",
# #     "treatment",
# #     "therapy",
# #     "procedure",
# #     "happens",
# #     "dentist",
# #     "program",
# #     "programs",
# #     "care",
# #     "health",
# #     "insurance",
# #     "policy",
# #     "related",
# #     "general",
# #     "standard",
# #     "other",
# #     "various",
# #     "max",
# #     "min",
# #     "per",
# #     "dental",
# #     "medical",
# #     "vision",
# #     "overview",
# #     "summary",
# #     "understand",
# #     "understanding",
# #     "communication",
# #     "saying",
# #     "asking",
# #     "talking",
# #     "explain",
# #     "meaning",
# #     "answer",
# #     "getting",
# #     "right",
# #     "wrong",
# #     "think",
# #     "feel",
# #     "know",
# #     "just",
# #     "really",
# #     "actually",
# #     "mean",
# #     "whats",
# #     "what" "wat",
# #     "unknown",
# #     "during",
# #     "stay",
# #     "provide",
# #     "provides",
# #     "provided",
# #     "use",
# #     "using",
# #     "give",
# #     "given",
# #     "make",
# #     "made",
# #     "take",
# #     "taken",
# #     "include",
# #     "includes",
# #     "included",
# #     "available",
# #     "apply",
# #     "applies",
# #     "applicable",
# # }


# # def fuzzy_match(a, b, threshold=0.8):
# #     return SequenceMatcher(None, a, b).ratio() >= threshold


# # def smart_match(term, query_words, query_lower):
# #     """
# #     Priority:
# #     1. Exact phrase match
# #     2. Exact word match
# #     3. Fuzzy match (safe)
# #     """
# #     term = term.lower()

# #     if " " in term:
# #         return term in query_lower

# #     if term in query_words:
# #         return True

# #     for w in query_words:
# #         if len(w) >= 4 and len(term) >= 4:
# #             if fuzzy_match(w, term):
# #                 return True

# #     return False


# # def flatten_message_content(content):
# #     """
# #     Forces any Ollama response (List, Dict, or None) into a plain string.
# #     """
# #     if not content:
# #         return ""
# #     if isinstance(content, str):
# #         return content
# #     if isinstance(content, list):
# #         parts = []
# #         for item in content:
# #             if isinstance(item, dict):
# #                 parts.append(item.get("text", str(item)))
# #             else:
# #                 parts.append(str(item))
# #         return " ".join(parts).strip()
# #     return str(content).strip()
