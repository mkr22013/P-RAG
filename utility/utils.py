"""
Shared utilities used by all booklet indexers (SBC, Medical, Dental, etc.)
"""

import os
import re
import json as json_lib
from datetime import datetime, timedelta
from difflib import SequenceMatcher

# ── Knowledge Base ─────────────────────────────────────────────────────────────

_KB_FILE = os.path.join(os.path.dirname(__file__), "knowledge_base.json")
_kb_data: dict = {}
_kb_loaded_at: datetime | None = None
_KB_TTL = timedelta(hours=24)


def _load_knowledge_base() -> dict:
    """
    Load knowledge_base.json with 24h TTL cache.

    Knowledge base maps benefit terms to plain-language synonyms and vice versa.
    Used at two points:
        1. Query time — expand query keywords before chunk scoring (tools.py)
        2. Index time — inject synonyms into chunk_keywords (get_smart_keywords)

    Structure:
        {
            "dental":  {"prophylaxis": ["cleaning", "teeth cleaning", ...], ...},
            "medical": {"rehabilitation": ["physical therapy", ...], ...},
            "vision":  {"vision hardware": ["glasses", "frames", ...], ...},
            "shared":  {"copay": ["co-pay", "fixed amount", ...], ...}
        }
    """
    global _kb_data, _kb_loaded_at
    now = datetime.utcnow()
    if _kb_loaded_at is not None and (now - _kb_loaded_at) < _KB_TTL and _kb_data:
        return _kb_data

    try:
        with open(_KB_FILE, encoding="utf-8") as f:
            _kb_data = json_lib.load(f)
        _kb_loaded_at = now
        print(
            f"[*] Knowledge base loaded: {sum(len(v) for v in _kb_data.values())} entries"
        )
    except FileNotFoundError:
        print(
            f"[!] knowledge_base.json not found at {_KB_FILE} — KB expansion disabled"
        )
        _kb_data = {}
    except Exception as e:
        print(f"[!] Failed to load knowledge_base.json: {e}")
        _kb_data = {}

    return _kb_data


# ── KB Gap Detection ──────────────────────────────────────────────────────────

KB_GAP_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "kb_gap_log.jsonl"
)


def find_kb_gaps(
    original_keywords: list, expanded_keywords: list, benefit_category: str
) -> list:
    """
    Find keywords that had no KB match — candidates for KB growth.

    A gap is a keyword that:
    - Was NOT expanded (not found as synonym in KB)
    - Is NOT already a canonical KB key (already the right term)
    - Is meaningful (length > 3, not stop/weak word)

    These are plain-language terms the member used that have
    no mapping in the KB yet. Logged for offline batch review.

    Args:
        original_keywords:  keywords before KB expansion
        expanded_keywords:  keywords after KB expansion
        benefit_category:   "dental", "medical", "vision", "rx"

    Returns:
        List of gap terms to log
    """
    kb = _load_knowledge_base()
    category_kb = kb.get(benefit_category, {})
    shared_kb = kb.get("shared", {})

    # All canonical keys in KB — these are already correct terms, not gaps
    all_canonicals = set(k.lower() for k in category_kb.keys())
    all_canonicals |= set(k.lower() for k in shared_kb.keys())

    # All synonym values in KB — these were already matched/expanded
    all_synonyms = set()
    for synonyms in category_kb.values():
        all_synonyms.update(s.lower() for s in synonyms)
    for synonyms in shared_kb.values():
        all_synonyms.update(s.lower() for s in synonyms)

    gaps = []
    for kw in original_keywords:
        kw_lower = kw.lower().strip()

        # Skip too short — not meaningful enough for KB
        if len(kw_lower) <= 3:
            continue
        # Skip already a canonical key — already the right term
        if kw_lower in all_canonicals:
            continue
        # Skip already a synonym — was matched and expanded
        if kw_lower in all_synonyms:
            continue
        # Skip noise/weak words
        if kw_lower in NOISE_WORDS:
            continue
        # Skip purely numeric
        if kw_lower.isdigit():
            continue

        gaps.append(kw_lower)

    return gaps


async def _log_kb_gaps_async(gaps: list, category: str, query: str) -> None:
    """
    Fire-and-forget async gap logger.

    Appends unmatched keywords to kb_gap_log.jsonl for offline
    batch processing by build_kb.py.

    Called via asyncio.create_task() — never awaited, never blocks
    the query response. Silent fail on any error.

    Log format (one JSON object per line):
        {
            "timestamp": "2026-07-20T15:30:00",
            "category":  "dental",
            "query":     "what does a tooth cap cost?",
            "gaps":      ["cap"]
        }
    """
    try:
        if not gaps:
            return

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "category": category,
            "query": query,
            "gaps": gaps,
        }

        # Append one line — JSONL format, safe for concurrent writes
        with open(KB_GAP_LOG, "a", encoding="utf-8") as f:
            f.write(json_lib.dumps(entry) + "\n")

        print(f"[KB GAP] {len(gaps)} gap(s) logged for {category}: {gaps}")

    except Exception:
        # Silent fail — gap logging must NEVER impact query response
        pass


def expand_query_keywords(keywords: list, benefit_category: str) -> list:
    """
    Expands query keywords using knowledge base synonyms.

    Called by tools.py before chunk scoring to bridge the gap between
    what members say and what the index contains:

        Member says:    "teeth cleaning"
        Index contains: "prophylaxis"

        Without expansion: 0 keyword matches → wrong chunk
        With expansion:    "cleaning" → KB → adds "prophylaxis"
                           → correct chunk scores higher ✅

    FORWARD-ONLY lookup (synonym → canonical):
        keyword is a VALUE (synonym) → member used lay term → add canonical KEY
        keyword is a KEY (canonical) → member used technical term → no expansion

        "cleaning"    → found in VALUES of "prophylaxis" → add "prophylaxis" ✅
        "prophylaxis" → found as KEY → already canonical → skip ✅
        "allergy"     → found as KEY → already canonical → skip ✅
        "allergen"    → found in VALUES of "allergy" → add "allergy" ✅
        "anesthesia"  → found as KEY → already canonical → skip ✅
        "sedation"    → found in VALUES of "anesthesia" → add "anesthesia" ✅

    Why NOT reverse (canonical → synonyms):
        Reverse expansion causes cross-chunk contamination:
        "allergy" (canonical) → would add "allergy shots", "allergen testing"
        → matches preventive care/office visit chunks incorrectly
        "anesthesia" (canonical) → would add "nitrous oxide", "sedation"
        → nitrous query pulls in general anesthesia chunk and vice versa
        When member uses technical term, no expansion needed — they're already precise.

    Also checks "shared" category for terms common across all categories
    (copay, deductible, in-network, etc.)

    Pure dict lookup — 0 tokens, 0 LLM calls, microseconds.
    If word not in KB → use as-is, normal scoring (graceful degradation).

    Args:
        keywords:         list of query keywords from topic_resolver
        benefit_category: "dental", "medical", "vision", "rx"

    Returns:
        Expanded list with KB synonyms added (deduped, order preserved)
    """
    kb = _load_knowledge_base()
    if not kb:
        return keywords

    category_kb = kb.get(benefit_category, {})
    shared_kb = kb.get("shared", {})

    expanded = list(keywords)

    def _add_if_new(term: str):
        if term and term not in expanded:
            expanded.append(term)

    for keyword in list(keywords):  # iterate original list only
        keyword_lower = keyword.lower().strip()

        # Forward-only lookup: synonym → canonical
        # Check category-specific KB + shared KB
        for kb_section in [category_kb, shared_kb]:
            for canonical, synonyms in kb_section.items():
                synonyms_lower = [s.lower() for s in synonyms]

                # Forward ONLY: keyword is a synonym → add canonical
                # e.g. "cleaning" → found in prophylaxis synonyms → add "prophylaxis"
                # Reverse deliberately removed — canonical → synonyms causes
                # cross-chunk contamination (see docstring above)
                if keyword_lower in synonyms_lower:
                    _add_if_new(canonical)

    return expanded


def get_smart_keywords(text, benefit_category: str | None = None) -> list:
    """
    Extract up to 15 meaningful keywords from a chunk of text or dict content.

    Three-phase extraction:

    Phase 1 — Domain pattern matching (runs on full content):
        Insurance-specific terms mapped to clean labels.
        Patterns run against full serialized dict — catches domain terms
        even when they appear in limitations/description text.

    Phase 2 — Word fallback (runs on event+service ONLY):
        Any 7+ char word not already captured, from event and service
        fields only. Excludes limitations prose to avoid noise like
        "rheumatoid", "example", "comparable" leaking in from description text.
        Also excludes JSON field names and generic stopwords.

    Phase 3 — Knowledge base synonym injection (category-aware):
        For each keyword found in Phases 1+2, looks up KB synonyms
        and injects them into chunk_keywords.
        "prophylaxis" → injects ["cleaning", "teeth cleaning", "polish"]
        Makes chunk findable via plain-language member queries.
        benefit_category determines which KB section to use.

    Why benefit_category matters:
        "prophylaxis" means dental cleaning in dental context
        "prophylaxis" means preventive care in medical context
        Category-aware KB prevents cross-category synonym injection.

    Args:
        text:             dict (chunk content) or str
        benefit_category: "dental", "medical", "vision", "sbc", None

    Returns:
        List of up to 15 keywords (10 base + up to 5 KB synonyms)
    """
    if isinstance(text, dict):
        full_text = json_lib.dumps(text)
        # Extract event+service only for word fallback (no limitations prose)
        fallback_text = f"{text.get('event', '')} {text.get('service', '')}".lower()
    else:
        full_text = text
        fallback_text = text.lower()

    full_text_lower = full_text.lower()

    # JSON field name noise — excluded from fallback
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

    # Generic prose words that add no scoring value — excluded from fallback
    FALLBACK_STOPWORDS = JSON_FIELD_NOISE | {
        "either",
        "comparable",
        "including",
        "focused",
        "complete",
        "series",
        "application",
        "polishing",
        "additional",
        "following",
        "members",
        "calendar",
        "limited",
        "covered",
        "amount",
        "based",
        "allowed",
        "benefits",
        "services",
        "treatment",
        "certain",
        "provides",
        "provided",
        "another",
        "between",
        "through",
        "without",
        "whether",
        "however",
        "because",
        "subject",
        "requires",
        "required",
        "received",
        "percent",
        "applies",
        "applicable",
        "available",
        "according",
        "described",
        "specified",
        "standard",
        "general",
        "specific",
        "includes",
        "included",
        "related",
        "relevant",
        "details",
        "further",
        "please",
        "contact",
        "section",
        "booklet",
        "summary",
        "applies",
    }

    # ── Shared patterns — run for ALL categories ──────────────────────────────
    # Cost sharing terms appear in every category — always meaningful
    # Emergency/urgent also shared — dental has D9440 emergency visits
    SHARED_PATTERNS = {
        # Cost sharing
        "copay": r"\bco[- ]?pay\b|\bcopay\b",
        "deductible": r"\bdeductible\b",
        "coinsurance": r"\bco[- ]?insurance\b|\bcoinsurance\b",
        "out-of-pocket": r"\bout[- ]?of[- ]?pocket\b",
        "prior-auth": r"\bprior\s+auth",
        # Provider type (shared across all)
        "specialist": r"\bspecialist\b",
        "emergency": r"\bemergency\b|medical[- ]?attention",
    }

    # ── Medical-specific patterns ──────────────────────────────────────────────
    # Only injected when benefit_category == "medical"
    # Prevents dental/vision chunks from getting medical keywords
    MEDICAL_PATTERNS = {
        # Facility / care setting
        "hospital": r"\bhospital\b(?!\s+stay)",
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
        "urgent-care": r"\burgent[- ]?care\b",
        # Service categories
        "pharmacy": r"\bpharmacy\b|\bprescription\b|\brx\b",
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

    # ── Dental-specific patterns ───────────────────────────────────────────────
    # Covers all common dental procedure codes and benefit names
    # Prevents generic words like "either", "comparable", "series" from
    # appearing as keywords when the real term (e.g. "panoramic") is available
    DENTAL_PATTERNS = {
        # Procedures
        "prophylaxis": r"\bprophylaxis\b",
        "extraction": r"\bextraction\b",
        "root-canal": r"\broot\s+canal\b|\bendodontic\b",
        "crown": r"\bcrown\b|\bporcelain\b|\bstainless\s+steel\b",
        "bridge": r"\bbridge\b|\bpontic\b",
        "implant": r"\bimplant\b",
        "denture": r"\bdenture\b",
        "sealant": r"\bsealant\b",
        "fluoride": r"\bfluoride\b",
        "orthodontic": r"\bordodontic\b|\borthodontic\b|\baligner\b",
        "periodontic": r"\bperiodontic\b|\bscaling\b|\broot\s+planing\b|\bgingivectomy\b",
        "tmj": r"\btmj\b|\btemporomandibular\b",
        "apicoectomy": r"\bapicoectomy\b|\bapical\b",
        "amalgam": r"\bamalgam\b",
        "composite": r"\bcomposite\b|\bresin\b",
        "anesthesia": r"\banesthesia\b|\bsedation\b",  # general anesthesia/sedation only
        "nitrous": r"\bnitrous\b|\banalgesia\b|\banxiolysis\b",  # nitrous oxide specific
        "pulp": r"\bpulp\s+cap\b|\bpulpotomy\b|\bpulpectomy\b",
        # X-rays
        "panoramic": r"\bpanoramic\b|\bpanorex\b",
        "bitewing": r"\bbitewing\b",
        "periapical": r"\bperiapical\b",
        "cone-beam": r"\bcone\s+beam\b|\bcbct\b",
        # Benefit classes
        "class-i": r"\bclass\s+i\b|\bclass\s+1\b|\bdiagnostic\s+and\s+preventive\b",
        "class-ii": r"\bclass\s+ii\b|\bclass\s+2\b|\bbasic\s+services\b",
        "class-iii": r"\bclass\s+iii\b|\bclass\s+3\b|\bmajor\s+services\b",
        # Dental-specific cost
        "annual-maximum": r"\bannual\s+max\b|\byearly\s+max\b|\bbenefit\s+max\b",
        "dental-deductible": r"\bdental\s+deductible\b|\bcalendar\s+year\s+deductible\b",
        # General
        "dental": r"\bdental\b|\bdentist\b",
        "oral": r"\boral\b|\bmouth\b|\bgum\b|\btooth\b|\bteeth\b",
    }

    # ── Vision-specific patterns ───────────────────────────────────────────────
    # Covers vision exam, hardware, contact lenses, and provider types
    VISION_PATTERNS = {
        # Exam
        "vision-exam": r"\bvision\s+exam\b|\beye\s+exam\b|\boptometry\b",
        # Hardware
        "vision-hardware": r"\bvision\s+hardware\b|\beyeglass\b|\bspectacle\b"
        r"|\bcontact\b|\blens\b|\bfitting\b|\bframes\b"
        r"|\bglasses\b|\bprogressive\b|\bbifocal\b"
        r"|\btrifocal\b|\ballowance\b|\bcontacts\b",
        "frames": r"\bframes\b|\beyeglass\s+frame\b",
        "lenses": r"\blenses\b|\beyeglass\s+lens\b",
        "contact-lenses": r"\bcontact\s+lens\b|\bcontacts\b|\bsoft\s+lens\b",
        "bifocal": r"\bbifocal\b",
        "progressive": r"\bprogressive\b|\bvarifocal\b|\bno[- ]line\b",
        "low-vision": r"\blow\s+vision\b|\bvision\s+impairment\b",
        # Provider
        "optometrist": r"\boptometrist\b|\bod\b",
        "ophthalmologist": r"\bophthalmologist\b",
        # Coverage
        "out-of-area": r"\bout.of.area\b|\boutside\s+washington\b|\btravel\s+vision\b",
        "hardware-limit": r"\bhardware\s+limit\b|\bannual\s+limit\b|\bvision\s+allowance\b",
        # General
        "vision": r"\bvision\b|\beye\b|\bglasses\b|\bsunglasses\b",
    }

    # ── Select patterns based on benefit_category ──────────────────────────────
    # Always start with shared patterns (cost sharing + emergency)
    # Then add category-specific patterns
    # This prevents cross-category contamination:
    #   dental chunk should NOT get "hospital", "inpatient" patterns
    #   medical chunk should NOT get "prophylaxis", "panoramic" patterns
    if benefit_category == "dental":
        active_patterns = {**SHARED_PATTERNS, **DENTAL_PATTERNS}
    elif benefit_category == "vision":
        active_patterns = {**SHARED_PATTERNS, **VISION_PATTERNS}
    elif benefit_category in ("medical", "sbc"):
        active_patterns = {**SHARED_PATTERNS, **MEDICAL_PATTERNS}
    else:
        # Unknown or no category — run all patterns (safe fallback)
        active_patterns = {
            **SHARED_PATTERNS,
            **MEDICAL_PATTERNS,
            **DENTAL_PATTERNS,
            **VISION_PATTERNS,
        }

    # Phase 1 — category-aware domain patterns on full content
    found = []
    for label, pat in active_patterns.items():
        if re.search(pat, full_text_lower) and label not in found:
            found.append(label)
        if len(found) >= 10:
            break

    # Phase 2 — word fallback from event+service ONLY (no limitations prose)
    if len(found) < 10:
        for word in re.findall(r"\b[a-z]\w{6,}\b", fallback_text):
            if word not in found and word not in FALLBACK_STOPWORDS and len(found) < 10:
                found.append(word)

    # Phase 3 — KB synonym injection (category-aware, forward-only)
    # Injects canonical terms so chunk is findable via plain-language member queries.
    #
    # FORWARD-ONLY: chunk has synonym → inject canonical
    #   "cleaning" in chunk keywords → inject "prophylaxis" ✅
    #   "tooth removal" in chunk keywords → inject "extraction" ✅
    #
    # NOT reverse: chunk has canonical → do NOT inject synonyms
    #   "allergy" in chunk keywords → do NOT inject "allergy shots", "allergen testing" ❌
    #   "anesthesia" in chunk keywords → do NOT inject "nitrous oxide", "sedation" ❌
    #
    # Why: reverse injection causes cross-chunk contamination.
    # "Professional Visits" chunk has "allergy" keyword (from limitations text).
    # Reverse would inject "allergy testing", "allergy shots" → now this chunk
    # scores high for allergy queries → wrong chunk returned.
    # Forward-only keeps injections targeted — only the specific chunk that
    # uses lay terminology gets enriched with its canonical term.
    if benefit_category and found:
        kb = _load_knowledge_base()
        category_kb = kb.get(benefit_category, {})
        shared_kb = kb.get("shared", {})
        enriched = list(found)

        for keyword in found:
            keyword_lower = keyword.lower()
            # Forward-only: check if keyword appears as a VALUE (synonym)
            # in any KB entry → inject the canonical KEY
            for kb_section in [category_kb, shared_kb]:
                for canonical, synonyms in kb_section.items():
                    if keyword_lower in [s.lower() for s in synonyms]:
                        if canonical not in enriched and len(enriched) < 15:
                            enriched.append(canonical)

        found = enriched

    return found[:15]


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
