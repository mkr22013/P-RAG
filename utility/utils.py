"""
Shared utilities used by all booklet indexers (SBC, Medical, Dental, etc.)
"""

import re
import json as json_lib
from difflib import SequenceMatcher


def get_smart_keywords(text):
    """
    Extract up to 10 meaningful keywords from a chunk of text or dict content.

    Two-phase extraction:
    1. Domain pattern matching — insurance-specific terms that are most useful
       for scoring. Each pattern maps to a clean label used as the keyword.
    2. Word length fallback — any word 7+ chars not already captured,
       EXCLUDING JSON field names that leak from dict serialization.

    Why this matters:
        chunk_keywords are used by tools.py to score chunks against query keywords.
        Poor keywords (e.g. JSON field names like "in_network", "limitations")
        cause all chunks to score equally — the correct chunk can't rank higher.
        Good keywords (e.g. "hospital", "inpatient", "hospice") allow the scoring
        to differentiate between similar chunks and return the most relevant one.

    Example fix:
        Before: Hospital chunk keywords = ['deductible', 'coinsurance', 'service',
                'in_network', 'out_of_network', 'limitations', 'hospital', 'inpatient']
                Hospice chunk keywords  = ['deductible', 'coinsurance', 'hospice',
                'inpatient', 'service', 'in_network', ...]
                → Both score equally for query "inpatient hospital stay"

        After:  Hospital chunk keywords = ['hospital', 'inpatient', 'deductible',
                'coinsurance']
                Hospice chunk keywords  = ['hospice', 'inpatient', 'deductible',
                'coinsurance', 'terminal', 'lifetime']
                → Hospital chunk scores higher for query containing "hospital"
    """
    if isinstance(text, dict):
        text = json_lib.dumps(text)

    text_lower = text.lower()

    # JSON field name noise — these leak into keywords when dict is serialized
    # and add no scoring value since they appear in EVERY chunk
    JSON_FIELD_NOISE = {
        "in_network",
        "out_of_network",
        "limitations",
        "service",
        "in_net",
        "out_net",
        "event",
        "notes",
        "page_number",
        "information",
        "benefit_category",
        "category",
        "topic",
        "data_not",
        "not_found",
    }

    # Domain-specific patterns — ordered by specificity (most specific first)
    # Each key is the clean keyword label stored in chunk_keywords
    patterns = {
        # Facility / care setting — most important for distinguishing chunks
        "hospital": r"\bhospital\b(?!\s+stay)",  # hospital (not "hospital stay")
        "inpatient": r"\binpatient\b",
        "outpatient": r"\boutpatient\b",
        "hospice": r"\bhospice\b",
        "surgical": r"\bsurg(ery|ical|eries)\b",
        "transplant": r"\btransplant",
        "maternity": r"\bmaternity\b|\bobstetric",
        "newborn": r"\bnewborn\b|\bneonatal\b",
        "dialysis": r"\bdialysis\b",
        "rehabilitation": r"\brehabilitation\b|\brehab\b",
        "skilled nursing": r"\bskilled\s+nursing\b",
        "home health": r"\bhome\s+health\b",
        "hospice care": r"\bhospice\s+care\b",
        # Provider type
        "pcp": r"\bpcp\b|primary[- ]?care\s+physician",
        "specialist": r"\bspecialist\b",
        "emergency": r"\bemergency\b|medical[- ]?attention",
        "urgent-care": r"\burgent[- ]?care\b",
        # Cost sharing
        "copay": r"\bco[- ]?pay\b|\bcopay\b",
        "deductible": r"\bdeductible\b",
        "coinsurance": r"\bco[- ]?insurance\b|\bcoinsurance\b",
        "out-of-pocket": r"\bout[- ]?of[- ]?pocket\b",
        "prior-auth": r"\bprior\s+auth",
        # Service categories
        "pharmacy": r"\bpharmacy\b|\bprescription\b|\brx\b",
        "dental": r"\bdental\b|\bdentist\b|\bortho\b|\bbraces\b",
        "vision": r"\bvision\b|\beye\b|\bglasses\b",
        "imaging": r"\bimaging\b|\bmri\b|\bct\s?scan\b|\bpet\s?scan\b",
        "diagnostic": r"\bdiagnostic\b|\bx-ray\b|\bblood\s?work\b",
        "mental-health": r"\bmental\b|\bbehavioral\b|\bsubstance\b|\babuse\b",
        "therapy": r"\bphysical\s+therapy\b|\bspeech\s+therapy\b|\boccupational\b",
        "preventive": r"\bpreventive\b|\bwellness\b|\bscreening\b",
        "allergy": r"\ballergy\b|\ballergic\b|\ballergi",
        "gender-affirming": r"\bgender\s+affirm",
        "bariatric": r"\bbariatric\b",
        "clinical-trials": r"\bclinical\s+trial",
        "transportation": r"\btransportation\b|\bambulance\b",
        "nicotine": r"\bnicotine\b|\bsmoking\b|\btobacco\b",
        "virtual-care": r"\bvirtual\s+care\b|\btelehealth\b|\btelemedicine\b",
        "electronic-visit": r"\belectronic\s+visit\b|\be-visit\b",
        "foot-care": r"\bfoot\s+care\b|\bpodiatry\b",
    }

    found = []
    for label, pat in patterns.items():
        if re.search(pat, text_lower) and label not in found:
            found.append(label)
        if len(found) >= 10:
            break

    # Word length fallback — fill remaining slots with 7+ char words
    # excluding JSON field names and already-found keywords
    if len(found) < 10:
        for word in re.findall(r"\b[a-z]\w{6,}\b", text_lower):
            if word not in found and word not in JSON_FIELD_NOISE and len(found) < 10:
                found.append(word)

    return found[:10]


# Single source of truth for noise/generic words.
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
    "just",
    "really",
    "actually",
    "mean",
    "whats",
    "wat",
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
# strong_query_words scoring against Rx chunk content (tools.py).
# This is the single source of truth — adding a word here automatically
# protects BOTH the keyword-extraction path in client.py and the
# chunk-scoring path in tools.py.
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
    "medications",
    "medicine",
    "medicines",
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
    # Conversational filler words
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
    # General concept words
    "preventive",
    "exception",
    "optional",
    "chemotherapy",
    # Response text words — prevent pasted bot responses becoming keywords
    "here",
    "are",
    "the",
    "may",
    "available",
    "ask",
    "specific",
    "requirements",
    "status",
    "coverage",
    "and",
    "see",
    "its",
    "not",
    "for",
    "any",
    "from",
    "with",
    "that",
    "also",
    "only",
    "each",
    "these",
    "those",
    "which",
    "when",
    "where",
    "how",
    "all",
    "some",
    "new",
    "per",
    "can",
    "will",
    "get",
}


def get_benefit_context_prefix(query: str, topics: list) -> str:
    """
    Generates a helpful context prefix when the member's query term does not
    directly match the benefit topic name returned by topic resolution.

    Problem this solves:
        Member asks: "is chiropractic care covered?"
        System finds: Rehabilitation Therapy benefit (correct — chiropractic
                      is covered under rehab therapy in most plans)
        Without prefix: Member sees a table about "Rehabilitation Therapy"
                        and wonders why — they asked about chiropractic
        With prefix:    "Your question about chiropractic care is covered
                        under Rehabilitation Therapy on your plan."

    Why not hardcode "chiropractic → Rehabilitation Therapy"?
        Because benefit mappings are plan-specific. Another plan might cover
        chiropractic under "Office Visits" or not at all. We use the actual
        topic returned by the system (which read the real plan data) rather
        than assuming a mapping.

    When prefix is added:
        - topics were resolved (not empty)
        - query term doesn't appear in any topic name
        - This signals the member asked about X but system found it under Y

    When prefix is NOT added:
        - "show me dialysis benefits" + topic="dialysis" → query matches topic
        - "my urgent care cost" + topic="urgent care" → query matches topic
        - topics is empty (LLM fallback handled it differently)

    Args:
        query:  The original member query string
        topics: List of resolved topic strings from topic_resolver

    Returns:
        A markdown prefix string, or empty string if no prefix needed.
    """
    if not topics:
        return ""

    query_lower = query.lower()

    # Check if any topic name appears in the query — if so, no prefix needed
    for topic in topics:
        topic_words = topic.lower().replace("-", " ").split()
        if any(w in query_lower for w in topic_words if len(w) > 3):
            return ""

    # Query term doesn't match topic — generate helpful context
    # Use the first/primary topic for the prefix
    topic_display = topics[0].replace("-", " ").title()
    return f"Your question is covered under **{topic_display}** on your plan.\n\n"


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
