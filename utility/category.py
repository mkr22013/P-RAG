import re
import json
import os
from datetime import datetime, timedelta

from config import settings
from .utils import smart_match

# ── Drug name word lookup ──────────────────────────────────────────────────────
# Used to detect drug-name queries like "does metformin require prior
# authorization?" where the only signal that this is an Rx question is the
# drug name itself — no other rx keyword like "drug", "formulary", "pharmacy"
# appears in the query.
#
# Reads from a single, lightweight, plan-agnostic drug_names.json file —
# built and deduplicated at INDEX TIME by rx_indexer.py (see
# update_drug_names_file there), not parsed from the full Rx index JSONs here.
# This file answers only "is this word a drug name, from ANY booklet we've
# ever indexed" — it deliberately does NOT know which plan/tier/cost a drug
# belongs to. That lookup happens later, correctly, via member_info once
# category=rx is already confirmed.
#
# This is a cost-optimization layer, not a correctness-critical one: if a
# word is missing from this list (e.g. a brand-new drug not yet indexed),
# the query simply falls through to the LLM category fallback, which still
# resolves it correctly — just at the cost of one extra LLM call for that
# one query.
#
# Refreshed every 48 hours per server instance (TTL-based), so long-running
# instances naturally pick up newly-indexed drug names without a restart.

_DRUG_NAME_DATA: dict[str, list] = {}
_drug_words_loaded_at: datetime | None = None
_DRUG_WORDS_TTL = timedelta(hours=48)

DRUG_NAMES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "indices",
    "drug_names.json",
)


def _load_drug_name_data() -> dict:
    """
    Loads the shared drug_names.json file into memory, cached with a 48-hour
    TTL per server instance. The file is ALREADY deduplicated and classified
    at write time (see rx_indexer.py's update_drug_names_file) — no dedup or
    LLM work needed here, just load directly.

    Structure: {"metformin": ["diabetes", "blood sugar"], "ozempic": [...], ...}
    Each key is a drug name word, each value is a list of layman illness/
    condition terms that drug treats (may be empty if not yet classified,
    or if classify_illness=False was used during indexing).
    """
    global _DRUG_NAME_DATA, _drug_words_loaded_at

    now = datetime.utcnow()
    cache_is_fresh = (
        _drug_words_loaded_at is not None
        and (now - _drug_words_loaded_at) < _DRUG_WORDS_TTL
    )
    if cache_is_fresh:
        return _DRUG_NAME_DATA

    try:
        if os.path.exists(DRUG_NAMES_FILE):
            with open(DRUG_NAMES_FILE, encoding="utf-8") as f:
                data = json.load(f)
                # Backward-compat: handle old flat-list format gracefully,
                # in case this runs against a file from before the illness
                # mapping upgrade — treat each word as having no illness terms
                if isinstance(data, list):
                    data = {word: [] for word in data}
        else:
            data = {}
            print(
                f"[!] drug_names.json not found at {DRUG_NAMES_FILE} — "
                f"run the Rx indexer at least once to generate it"
            )
    except Exception as e:
        print(f"[!] Failed to load drug_names.json: {e}")
        data = (
            _DRUG_NAME_DATA or {}
        )  # keep stale cache on error rather than going empty

    _DRUG_NAME_DATA = data
    _drug_words_loaded_at = now
    illness_count = sum(1 for terms in data.values() if terms)
    print(
        f"[*] Loaded {len(data)} drug name words for rx category detection "
        f"({illness_count} with illness terms mapped)"
    )
    return _DRUG_NAME_DATA


def _load_drug_name_words() -> set:
    """
    Backward-compatible accessor — returns just the set of drug name words
    (the dict's keys), for callers that only need membership checks and
    don't care about illness terms (is_drug_name_query, correct_drug_spelling,
    is_drug_match all only need this).
    """
    return set(_load_drug_name_data().keys())


def get_illness_terms_for_word(word: str) -> list:
    """
    Returns the layman illness/condition terms associated with a drug name
    word, or an empty list if the word isn't a known drug name or hasn't
    been classified yet. Used for condition-based Rx queries like
    "is my diabetes medicine covered?" (see client.py's medicine-signal-word
    guarded condition search).
    """
    data = _load_drug_name_data()
    return data.get(word.lower(), [])


def find_drug_words_for_illness(illness_term: str) -> list:
    """
    Returns all drug name words whose illness terms include the given
    illness/condition term. Used for condition-based Rx queries.

    Example: find_drug_words_for_illness("diabetes")
        → ["metformin", "ozempic", "glipizide", ...]
    """
    data = _load_drug_name_data()
    illness_term = illness_term.lower()
    return [
        word
        for word, terms in data.items()
        if illness_term in [t.lower() for t in terms]
    ]


def is_drug_name_query(query_words: list) -> bool:
    """
    Returns True if any word in the query matches a real drug name word
    from the Rx index. Used to catch drug-specific questions that don't
    contain any other rx signal word.

    Example: "does metformin require prior authorization?"
        → "metformin" is in the Rx index → returns True
    Example: "is bariatric surgery covered?"
        → "bariatric" is not a drug name → returns False
    """
    drug_words = _load_drug_name_words()
    if not drug_words:
        return False
    return any(w in drug_words for w in query_words)


def correct_drug_spelling(keyword: str) -> str:
    """
    Attempts to correct a misspelled drug name keyword by finding the
    closest matching real drug name word from the Rx index.

    Returns the corrected drug name word if a close match is found,
    or the original keyword unchanged if no match meets the threshold.

    Examples:
        "ozempick"   → "ozempic"    (exact substring: "ozempic" in "ozempick" reversed? no — phonetic)
        "metaforming" → "metformin"
        "humera"      → "humira"
        "flucanazole" → "fluconazole"

    Only corrects when confident (same thresholds as is_drug_match):
        - Exact: keyword already matches a real drug word → return as-is
        - Character similarity >= 0.82 → return corrected word
        - Phonetic + character >= 0.70 → return corrected word
    If no confident match found → return original keyword unchanged
    """
    from difflib import SequenceMatcher

    kw = keyword.lower()
    drug_words = _load_drug_name_words()

    if not drug_words:
        return keyword

    # Already an exact match — no correction needed
    if kw in drug_words:
        return keyword

    best_word = None
    best_score = 0.0
    kw_soundex = _soundex(kw)

    for word in drug_words:
        if len(word) <= 4 or len(kw) <= 4:
            continue
        char_score = SequenceMatcher(None, kw, word).ratio()

        if char_score >= 0.82:
            if char_score > best_score:
                best_score = char_score
                best_word = word
        elif char_score >= 0.70 and kw_soundex == _soundex(word):
            if char_score > best_score:
                best_score = char_score
                best_word = word

    if best_word:
        print(
            f"[*] SPELLING CORRECTED: {keyword!r} → {best_word!r} (score={best_score:.2f})"
        )
        return best_word

    return keyword  # no confident match — return original unchanged


def _soundex(word: str) -> str:
    """
    Dependency-free Soundex phonetic encoder.
    Encodes a word by how it SOUNDS rather than how it's spelled —
    catches phonetic misspellings that character-similarity alone misses.

    Examples:
        "humira" → "H560"
        "humera" → "H560"  (same code, correctly identified as phonetically similar)
        "metformin" → "M316"
        "metoprolol" → "M316"  (SAME code — this is why Soundex alone is insufficient;
                                 always combine with character-similarity check)
    """
    word = word.upper()
    if not word:
        return ""
    codes = {
        "B": "1",
        "F": "1",
        "P": "1",
        "V": "1",
        "C": "2",
        "G": "2",
        "J": "2",
        "K": "2",
        "Q": "2",
        "S": "2",
        "X": "2",
        "Z": "2",
        "D": "3",
        "T": "3",
        "L": "4",
        "M": "5",
        "N": "5",
        "R": "6",
    }
    first_letter = word[0]
    encoded = [first_letter]
    prev_code = codes.get(first_letter, "")
    for char in word[1:]:
        code = codes.get(char, "")
        if code and code != prev_code:
            encoded.append(code)
        prev_code = code
    encoded = encoded + ["0", "0", "0"]
    return "".join(encoded[:4])


def is_drug_match(keyword: str, drug_name: str) -> bool:
    """
    Returns True if keyword matches drug_name exactly, by close character
    similarity, or by combined phonetic + character similarity.

    Three-tier matching:
        1. Exact substring — "metformin" in "metformin oral tablet 500 mg"
        2. Character-similarity >= 0.82 — catches minor typos like "metfromin"
        3. Phonetic + character combined — catches phonetic misspellings like
           "humera" → "humira" that pure character-similarity might miss

    IMPORTANT: phonetic match ALONE is not safe for drug names.
    "metformin" and "metoprolol" share the same Soundex code (M316) despite
    being completely different drugs. Requiring BOTH Soundex agreement AND
    a character-similarity floor of 0.70 prevents this class of false positive.
    Verified against real drug names: all 6 misspelling test cases pass,
    all 3 must-stay-distinct cases correctly rejected.
    """
    from difflib import SequenceMatcher

    kw = keyword.lower()
    dn = drug_name.lower()

    # Tier 1: Exact substring match (fast path)
    if kw in dn:
        return True

    # Tier 2 + 3: Check each word in the drug name
    for word in dn.split():
        if len(word) > 4 and len(kw) > 4:
            char_score = SequenceMatcher(None, kw, word).ratio()
            # Tier 2: Character-similarity alone — minor typos
            if char_score >= 0.82:
                return True
            # Tier 3: Phonetic + character combined — phonetic misspellings
            # Requires BOTH conditions to avoid metformin/metoprolol false positives
            if char_score >= 0.70 and _soundex(kw) == _soundex(word):
                return True

    return False


_CONVERSATIONAL_PATTERNS = [
    r"^(hi|hello|hey|howdy|greetings)\b",
    r"^how are you",
    r"^(good morning|good afternoon|good evening|good night)\b",
    r"^(thanks|thank you|thx|ty)\b",
    r"^(ok|okay|got it|understood|makes sense|alright|sure|cool|great|awesome)\b",
    r"^(yes|no|maybe|nope|yep|yeah|nah)\b",
    r"^(i did not get|i don't understand|i dont understand|can you explain|what do you mean|what did you mean)\b",
    r"^(can you repeat|repeat that|say that again)\b",
    r"^(who are you|what are you|what can you do|help me|help)\b",
    r"^(bye|goodbye|see you|later|take care)\b",
]

_CONVERSATIONAL_RE = [re.compile(p, re.IGNORECASE) for p in _CONVERSATIONAL_PATTERNS]


def is_conversational(query: str) -> bool:
    """
    Returns True if the query is conversational/non-benefit (greeting,
    follow-up, acknowledgement) and should NOT go through the RAG pipeline.
    """
    q = query.strip().lower()
    words = [w for w in re.sub(r"[^\w\s]", "", q).split() if len(w) > 1]

    benefit_signals = {
        "copay",
        "cost",
        "cover",
        "covered",
        "benefit",
        "deductible",
        "plan",
        "insurance",
        "medical",
        "dental",
        "vision",
        "pcp",
        "doctor",
        "hospital",
        "er",
        "urgent",
        "claim",
        "premium",
        "coinsurance",
        "network",
        "provider",
        "drug",
        "prescription",
        "tier",
        "generic",
        "brand",
        "pharmacy",
        "medication",
    }

    if len(words) <= 3:
        if not any(w in benefit_signals for w in words):
            for pattern in _CONVERSATIONAL_RE:
                if pattern.match(q):
                    return True

    for pattern in _CONVERSATIONAL_RE:
        if pattern.match(q):
            return True

    return False


def build_category_prompt(query: str) -> str:
    return f"""
        You are a strict JSON classifier for a health insurance assistant.

        Classify the query into ONE of these categories:
        medical, dental, vision, rx, or unknown.

        Use "rx" when the query is about:
        - A specific drug, medication, or prescription
        - Drug tiers, formulary, prior authorization for drugs
        - Generic vs brand drugs, specialty pharmacy

        Use "unknown" when:
        - The query is a greeting (hi, hello, how are you)
        - The query is a follow-up or clarification (I did not get it)
        - The query is NOT about health insurance benefits

        Return ONLY this exact JSON format:

        {{
        "category": "medical"
        }}

        - The key MUST be "category"
        - The value MUST be one of: medical, dental, vision, rx, unknown
        - Do NOT return anything else
        - Do NOT return null

        "{query}"
        """


def get_category_from_llm(query: str) -> str | None:
    from utility.llm import llm_chat

    prompt = build_category_prompt(query)
    llm_messages = [{"role": "user", "content": prompt}]

    try:
        content = llm_chat(messages=llm_messages, format="json", max_tokens=50)
        print(f"[*] RAW LLM CATEGORY RESPONSE: {content}")
        data = json.loads(content)
        category = data.get("category", "").strip().lower()

        if category == "unknown":
            print("[*] LLM CATEGORY: unknown — conversational/non-benefit query")
            return None

        if category not in {"medical", "dental", "vision", "rx"}:
            print(
                f"[WARNING] Invalid category from LLM: {category} — treating as unknown"
            )
            return None

        print(f"[*] LLM CATEGORY DETECTED: {category}")
        return category

    except Exception as e:
        print(f"[ERROR] LLM CATEGORY FAILED: {e}")
        return None


def detect_category_rule_based(query_words: list, query: str) -> str | None:
    """
    Rule-based only category detection — never calls LLM.
    Returns None when category cannot be determined from rules alone.
    Used for history boundary detection and as first pass before LLM fallback.
    """
    q = query.lower()

    # ── Rx — check first (strong signals unlikely to appear in other categories)
    if any(
        w in query_words
        for w in [
            "drug",
            "drugs",
            "medication",
            "medications",
            "formulary",
            "generic",
            "brand",
            "specialty",
            "tier",
            "refill",
            "pill",
            "tablet",
            "capsule",
            "prescription",
            "pharmacy",
            "prior auth",
        ]
    ) or any(
        # drug name patterns: "Xmg", "X mg"
        w.replace("mg", "").isdigit()
        for w in query_words
        if "mg" in w
    ):
        return "rx"

    # ── Dental
    # NOTE: "implant" deliberately excluded — see detailed comment in
    # detect_category() Step 2 above. Same reasoning applies here: it's
    # genuinely ambiguous across medical/dental and forcing it to dental
    # here would create the same false-confidence routing bug.
    if any(
        w in query_words
        for w in [
            "dental",
            "dentist",
            "ortho",
            "braces",
            "tooth",
            "teeth",
            "gum",
            "cavity",
            "filling",
            "crown",
            "denture",
            "molar",
            "canal",
            "tmj",
            "jaw",
            "orthodontic",
            "orthodontia",
            "panoramic",
            "sealant",
            "fluoride",
            "class",
            "prophylaxis",
            "cleaning",
            "extraction",
            "periodontal",
            "scaling",
            "nitrous",
            "apicoectomy",
            "retrograde",
            "bridge",
            "veneer",
            "inlay",
            "onlay",
            "prosthodontic",
            "endodontic",
            "bitewing",
            "periapical",
        ]
    ):
        return "dental"

    # ── Vision
    if any(
        w in query_words
        for w in [
            "vision",
            "eye",
            "glasses",
            "lens",
            "lenses",
            "contacts",
            "frames",
            "optometrist",
            "ophthalmologist",
            "bifocal",
            "sunglasses",
            "eyewear",
            "contact",
        ]
    ):
        return "vision"

    # ── Medical (broad — catches most benefit questions)
    if any(
        w in query_words
        for w in [
            "medical",
            "doctor",
            "hospital",
            "pcp",
            "emergency",
            "urgent",
            "ambulance",
            "immunization",
            "vaccination",
            "cancer",
            "dialysis",
            "deductible",
            "copay",
            "coinsurance",
            "surgery",
            "inpatient",
            "outpatient",
            "specialist",
            "lab",
            "imaging",
            "xray",
            "mri",
            "physical",
            "therapy",
            "mental",
            "behavioral",
            "maternity",
            "newborn",
            "transplant",
            "bariatric",
            "rehab",
            "rehabilitation",
            "skilled",
            "nursing",
            "home health",
            "hospice",
            "preventive",
            "allergy",
            "infusion",
            "chemotherapy",
            "radiation",
            "dialysis",
            "out-of-pocket",
            "oop",
            "deductible",
            "premium",
            "network",
            "prior authorization",
            "referral",
            "claim",
            "benefit",
            "coverage",
            "covered",
            "cover",
            "plan",
            "cost",
        ]
    ):
        return "medical"

    return None  # ambiguous — caller decides


def detect_category(query_words, query):
    category = None

    # ── Step 1: Rx signals (before dental/medical to avoid ambiguity)
    if any(
        w in query_words
        for w in [
            "drug",
            "drugs",
            "medication",
            "medications",
            "formulary",
            "generic",
            "brand",
            "tier",
            "refill",
            "pill",
            "tablet",
            "capsule",
            "pharmacy",
            "prior auth",
        ]
    ) or any(w.replace("mg", "").isdigit() for w in query_words if "mg" in w):
        print("[*] CATEGORY MATCH → rx")
        return "rx"

    # ── Step 1b: Drug name match — catches queries with no other rx signal
    # e.g. "does metformin require prior authorization?"
    # Checked against real drug names from the Rx index — not guessed patterns
    if is_drug_name_query(query_words):
        print("[*] CATEGORY MATCH → rx (drug name found in query)")
        return "rx"

    # NOTE: Step 1c (phonetic/fuzzy drug name match for category detection)
    # was removed — confirmed to cause false positives on ordinary English
    # words that coincidentally resemble a drug name by character similarity
    # alone (e.g. "testing" vs "estring" — a real drug — scored 0.857 on
    # SequenceMatcher, above the 0.82 threshold, despite having NO phonetic
    # relationship via Soundex). This broke medical category routing for
    # queries like "allergy testing and treatment cost".
    # Misspelled drug names now correctly fall through to the LLM category
    # fallback instead — a small token cost for that specific case, but
    # removes the risk of false-positive category misrouting for ordinary
    # medical/dental/vision queries. correct_drug_spelling() and is_drug_match()
    # remain in use within the Rx dual-index query path itself (client.py),
    # where they are scoped only to confirmed Rx queries — that usage is
    # unaffected by this change.

    # ── Step 2: Dental procedure terms (high precision)
    # NOTE: "implant" deliberately removed from this list. Unlike the other
    # terms here, "implant" is genuinely ambiguous across categories —
    # medical also legitimately uses it (hearing implants, joint replacement
    # implants, breast reconstruction implants, cochlear implants). Keeping
    # it here would FORCE every bare "implant"/"implants" query to dental,
    # even when the member means a medical implant. A query like "is there
    # a maximum benefit for implants?" has no way to know which the member
    # means without more context — so it correctly falls through to the LLM
    # fallback instead of being forced into one category with false
    # confidence. "dental implant" / "tooth implant" still correctly routes
    # to dental via the "dental"/"tooth" terms already in this list.
    _dental_proc_terms = [
        "dental",
        "dentist",
        "tooth",
        "teeth",
        "gum",
        "cavity",
        "filling",
        "crown",
        "denture",
        "molar",
        "canal",
        "tmj",
        "jaw",
        "orthodontic",
        "orthodontia",
        "panoramic",
        "sealant",
        "fluoride",
        "class",
        "prophylaxis",
        "cleaning",
        "extraction",
        "periodontal",
        "scaling",
        "nitrous",
        "apicoectomy",
        "retrograde",
        "bridge",
        "veneer",
        "inlay",
        "onlay",
        "prosthodontic",
        "endodontic",
        "bitewing",
        "periapical",
        "braces",
        "ortho",
    ]

    if any(w in query_words for w in _dental_proc_terms):
        print("[*] CATEGORY MATCH → dental (procedure)")
        return "dental"

    # ── Step 3: Vision
    if any(
        w in query_words
        for w in [
            "vision",
            "eye",
            "glasses",
            "lens",
            "lenses",
            "contacts",
            "frames",
            "optometrist",
            "ophthalmologist",
            "bifocal",
            "sunglasses",
            "eyewear",
            "contact",
        ]
    ):
        print("[*] CATEGORY MATCH → vision")
        return "vision"

    # ── Step 4: Medical — specific medical terms only
    # Note: generic words like "covered", "plan", "benefit", "network" removed
    # to avoid false positives on drug name queries like "is vivjoa covered?"
    _medical_terms = [
        "medical",
        "doctor",
        "hospital",
        "pcp",
        "emergency",
        "urgent",
        "ambulance",
        "immunization",
        "vaccination",
        "cancer",
        "dialysis",
        "deductible",
        "copay",
        "coinsurance",
        "surgery",
        "inpatient",
        "outpatient",
        "specialist",
        "imaging",
        "xray",
        "mri",
        "therapy",
        "mental",
        "behavioral",
        "maternity",
        "newborn",
        "transplant",
        "bariatric",
        "rehab",
        "rehabilitation",
        "skilled",
        "nursing",
        "hospice",
        "preventive",
        "allergy",
        "infusion",
        "chemotherapy",
        "radiation",
        # Additional unambiguously medical terms — eliminates LLM category calls
        "immunotherapy",
        "therapeutic",
        "vasectomy",
        "dialysis",
        "reconstruction",
        "gender",
        "affirming",
        "clinical",
        "trial",
        "trials",
        "foot",
        "home",
        "health",
        "electronic",
        "virtual",
        "nicotine",
        "psychological",
        "blood",
        "products",
        "transplants",
        "authorization",
        "transportation",
        "newborn",
        "impatient",
    ]

    if any(w in query_words for w in _medical_terms):
        print(f"[*] CATEGORY MATCH → medical")
        return "medical"

    # NOTE: Step 5 (last-resort phonetic drug-name match, placed AFTER
    # Steps 1-4 so it can't steal a legitimately medical/dental/vision
    # query) was attempted and reverted tonight. The STRUCTURAL placement
    # was confirmed correct — it never fired on a query any other category
    # would have claimed. But it surfaced a real, different problem:
    # common English words can be phonetically/character-similar to drug
    # BRAND NAME FRAGMENTS, not just generic dosage-form words we can
    # safely stoplist.
    #   - "breast" → "breath" (0.83): fixed — "breath" was a dosage-form
    #     descriptor ("...AEROSOL POWDR BREATH ACTIVATED...") and has been
    #     added to the stoplist in rx_indexer.py.
    #   - "maintenance" → "maintena" (0.84): NOT fixable the same way —
    #     "MAINTENA" is a genuine fragment of the real brand name "ABILIFY
    #     MAINTENA". Stoplisting it risks breaking legitimate lookups for
    #     that drug. "maintenance" itself is an ordinary English word with
    #     zero relationship to medication — it should never have been
    #     checked against drug names in the first place.
    # The right fix is a dedicated common-English-word EXCLUSION list
    # (similar in spirit to utils.py's NOISE_WORDS) checked BEFORE
    # attempting phonetic correction in Step 5 — not a per-collision
    # stoplist patch. Needs proper design time, not a late-session rush.
    # See RX_INDEXER_FLOW.md for write-up; revisit next session.

    # ── LLM fallback — only when rule-based fails
    print("[*] CATEGORY NOT FOUND → CALLING LLM")
    return get_category_from_llm(query)


def detect_category_from_history(history, limit=3):
    for msg in reversed(history[-limit:]):
        if msg["role"] == "user":
            query_lower = msg["content"].lower()
            query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]
            cat = detect_category(query_words, query_lower)
            if cat:
                return cat
    return None


def extract_user_queries(recent_history):
    queries = []
    for msg in recent_history:
        if msg.get("role") == "user":
            queries.append(msg.get("content", "").lower())
    return queries


# # ====================================Previous working version before adding full spelling mistake check for Rx=========================
# # import re
# # import json
# # import os
# # import glob

# # from config import settings
# # from .utils import smart_match

# # # ── Drug name word lookup ──────────────────────────────────────────────────────
# # # Built once from all Rx index files on disk. Used to detect drug-name queries
# # # like "does metformin require prior authorization?" where the only signal
# # # that this is an Rx question is the drug name itself — no other rx keyword
# # # like "drug", "formulary", "pharmacy" appears in the query.
# # #
# # # This is safe (not fragile) because it checks against REAL drug names from
# # # the actual indexed documents — not guessed patterns. "metformin" matches
# # # because it IS a drug name in the index. "bariatric" never matches because
# # # it is not a drug name anywhere in the Rx index.

# # _DRUG_NAME_WORDS: set[str] = set()
# # _drug_words_loaded = False

# # # Common English/medical words that appear in drug names but aren't drug names
# # # themselves — must be excluded so they don't cause false positives elsewhere
# # _DRUG_WORD_STOPLIST = {
# #     "oral",
# #     "tablet",
# #     "tablets",
# #     "capsule",
# #     "capsules",
# #     "injection",
# #     "injectable",
# #     "solution",
# #     "suspension",
# #     "cream",
# #     "ointment",
# #     "gel",
# #     "patch",
# #     "spray",
# #     "drops",
# #     "extended",
# #     "release",
# #     "delayed",
# #     "for",
# #     "and",
# #     "with",
# #     "the",
# #     "mg",
# #     "ml",
# #     "mcg",
# #     "unit",
# #     "units",
# #     "per",
# #     "reconstitution",
# #     "dispersion",
# #     "topical",
# #     "inhalation",
# #     "nasal",
# #     "vaginal",
# #     "rectal",
# #     "subcutaneous",
# #     "intramuscular",
# #     "intravenous",
# #     # Generic descriptor words that appear in drug names but aren't drug
# #     # identifiers themselves — would cause false positives in category detection
# #     "dental",
# #     "paste",
# #     "sensitive",
# #     "defense",
# #     "protect",
# #     "booster",
# #     "kids",
# #     "daily",
# #     "plus",
# #     "starter",
# #     "package",
# #     "kit",
# #     "complete",
# #     "implant",
# #     "maintenance",
# #     "emergency",
# #     "fluoride",
# #     "nicotine",
# # }


# # def _load_drug_name_words() -> set:
# #     """
# #     Loads all drug name words from Rx index files in the indices/ folder.
# #     Cached after first call — only loads once per process.
# #     """
# #     global _DRUG_NAME_WORDS, _drug_words_loaded

# #     if _drug_words_loaded:
# #         return _DRUG_NAME_WORDS

# #     indices_dir = os.path.join(
# #         os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "indices"
# #     )
# #     rx_index_files = glob.glob(os.path.join(indices_dir, "*rx*.json"))

# #     words = set()
# #     for index_path in rx_index_files:
# #         try:
# #             with open(index_path, encoding="utf-8") as f:
# #                 chunks = json.load(f)
# #             for chunk in chunks:
# #                 content = chunk.get("content", {})
# #                 if not isinstance(content, dict):
# #                     continue
# #                 drug_name = content.get("drug_name", "")
# #                 if not drug_name:
# #                     continue
# #                 for word in re.findall(r"[a-zA-Z]+", drug_name.lower()):
# #                     if len(word) > 4 and word not in _DRUG_WORD_STOPLIST:
# #                         words.add(word)
# #         except Exception as e:
# #             print(f"[!] Failed to load drug names from {index_path}: {e}")

# #     _DRUG_NAME_WORDS = words
# #     _drug_words_loaded = True
# #     print(f"[*] Loaded {len(words)} drug name words for rx category detection")
# #     return _DRUG_NAME_WORDS


# # def is_drug_name_query(query_words: list) -> bool:
# #     """
# #     Returns True if any word in the query matches a real drug name word
# #     from the Rx index. Used to catch drug-specific questions that don't
# #     contain any other rx signal word.

# #     Example: "does metformin require prior authorization?"
# #         → "metformin" is in the Rx index → returns True
# #     Example: "is bariatric surgery covered?"
# #         → "bariatric" is not a drug name → returns False
# #     """
# #     drug_words = _load_drug_name_words()
# #     if not drug_words:
# #         return False
# #     return any(w in drug_words for w in query_words)


# # # ── Conversational patterns — queries that are NOT benefit questions ──────────
# # _CONVERSATIONAL_PATTERNS = [
# #     r"^(hi|hello|hey|howdy|greetings)\b",
# #     r"^how are you",
# #     r"^(good morning|good afternoon|good evening|good night)\b",
# #     r"^(thanks|thank you|thx|ty)\b",
# #     r"^(ok|okay|got it|understood|makes sense|alright|sure|cool|great|awesome)\b",
# #     r"^(yes|no|maybe|nope|yep|yeah|nah)\b",
# #     r"^(i did not get|i don't understand|i dont understand|can you explain|what do you mean|what did you mean)\b",
# #     r"^(can you repeat|repeat that|say that again)\b",
# #     r"^(who are you|what are you|what can you do|help me|help)\b",
# #     r"^(bye|goodbye|see you|later|take care)\b",
# # ]

# # _CONVERSATIONAL_RE = [re.compile(p, re.IGNORECASE) for p in _CONVERSATIONAL_PATTERNS]


# # def is_conversational(query: str) -> bool:
# #     """
# #     Returns True if the query is conversational/non-benefit (greeting,
# #     follow-up, acknowledgement) and should NOT go through the RAG pipeline.
# #     """
# #     q = query.strip().lower()
# #     words = [w for w in re.sub(r"[^\w\s]", "", q).split() if len(w) > 1]

# #     benefit_signals = {
# #         "copay",
# #         "cost",
# #         "cover",
# #         "covered",
# #         "benefit",
# #         "deductible",
# #         "plan",
# #         "insurance",
# #         "medical",
# #         "dental",
# #         "vision",
# #         "pcp",
# #         "doctor",
# #         "hospital",
# #         "er",
# #         "urgent",
# #         "claim",
# #         "premium",
# #         "coinsurance",
# #         "network",
# #         "provider",
# #         "drug",
# #         "prescription",
# #         "tier",
# #         "generic",
# #         "brand",
# #         "pharmacy",
# #         "medication",
# #     }

# #     if len(words) <= 3:
# #         if not any(w in benefit_signals for w in words):
# #             for pattern in _CONVERSATIONAL_RE:
# #                 if pattern.match(q):
# #                     return True

# #     for pattern in _CONVERSATIONAL_RE:
# #         if pattern.match(q):
# #             return True

# #     return False


# # def build_category_prompt(query: str) -> str:
# #     return f"""
# #         You are a strict JSON classifier for a health insurance assistant.

# #         Classify the query into ONE of these categories:
# #         medical, dental, vision, rx, or unknown.

# #         Use "rx" when the query is about:
# #         - A specific drug, medication, or prescription
# #         - Drug tiers, formulary, prior authorization for drugs
# #         - Generic vs brand drugs, specialty pharmacy

# #         Use "unknown" when:
# #         - The query is a greeting (hi, hello, how are you)
# #         - The query is a follow-up or clarification (I did not get it)
# #         - The query is NOT about health insurance benefits

# #         Return ONLY this exact JSON format:

# #         {{
# #         "category": "medical"
# #         }}

# #         - The key MUST be "category"
# #         - The value MUST be one of: medical, dental, vision, rx, unknown
# #         - Do NOT return anything else
# #         - Do NOT return null

# #         "{query}"
# #         """


# # def get_category_from_llm(query: str) -> str | None:
# #     from utility.llm import llm_chat

# #     prompt = build_category_prompt(query)
# #     llm_messages = [{"role": "user", "content": prompt}]

# #     try:
# #         content = llm_chat(messages=llm_messages, format="json", max_tokens=50)
# #         print(f"[*] RAW LLM CATEGORY RESPONSE: {content}")
# #         data = json.loads(content)
# #         category = data.get("category", "").strip().lower()

# #         if category == "unknown":
# #             print("[*] LLM CATEGORY: unknown — conversational/non-benefit query")
# #             return None

# #         if category not in {"medical", "dental", "vision", "rx"}:
# #             print(
# #                 f"[WARNING] Invalid category from LLM: {category} — treating as unknown"
# #             )
# #             return None

# #         print(f"[*] LLM CATEGORY DETECTED: {category}")
# #         return category

# #     except Exception as e:
# #         print(f"[ERROR] LLM CATEGORY FAILED: {e}")
# #         return None


# # def detect_category_rule_based(query_words: list, query: str) -> str | None:
# #     """
# #     Rule-based only category detection — never calls LLM.
# #     Returns None when category cannot be determined from rules alone.
# #     Used for history boundary detection and as first pass before LLM fallback.
# #     """
# #     q = query.lower()

# #     # ── Rx — check first (strong signals unlikely to appear in other categories)
# #     if any(
# #         w in query_words
# #         for w in [
# #             "drug",
# #             "drugs",
# #             "medication",
# #             "medications",
# #             "formulary",
# #             "generic",
# #             "brand",
# #             "specialty",
# #             "tier",
# #             "refill",
# #             "pill",
# #             "tablet",
# #             "capsule",
# #             "prescription",
# #             "pharmacy",
# #             "prior auth",
# #         ]
# #     ) or any(
# #         # drug name patterns: "Xmg", "X mg"
# #         w.replace("mg", "").isdigit()
# #         for w in query_words
# #         if "mg" in w
# #     ):
# #         return "rx"

# #     # ── Dental
# #     if any(
# #         w in query_words
# #         for w in [
# #             "dental",
# #             "dentist",
# #             "ortho",
# #             "braces",
# #             "tooth",
# #             "teeth",
# #             "gum",
# #             "cavity",
# #             "filling",
# #             "crown",
# #             "denture",
# #             "molar",
# #             "canal",
# #             "implant",
# #             "tmj",
# #             "jaw",
# #             "orthodontic",
# #             "orthodontia",
# #             "panoramic",
# #             "sealant",
# #             "fluoride",
# #             "class",
# #             "prophylaxis",
# #             "cleaning",
# #             "extraction",
# #             "periodontal",
# #             "scaling",
# #             "nitrous",
# #             "apicoectomy",
# #             "retrograde",
# #             "bridge",
# #             "veneer",
# #             "inlay",
# #             "onlay",
# #             "prosthodontic",
# #             "endodontic",
# #             "bitewing",
# #             "periapical",
# #         ]
# #     ):
# #         return "dental"

# #     # ── Vision
# #     if any(
# #         w in query_words
# #         for w in [
# #             "vision",
# #             "eye",
# #             "glasses",
# #             "lens",
# #             "lenses",
# #             "contacts",
# #             "frames",
# #             "optometrist",
# #             "ophthalmologist",
# #             "bifocal",
# #             "sunglasses",
# #             "eyewear",
# #             "contact",
# #         ]
# #     ):
# #         return "vision"

# #     # ── Medical (broad — catches most benefit questions)
# #     if any(
# #         w in query_words
# #         for w in [
# #             "medical",
# #             "doctor",
# #             "hospital",
# #             "pcp",
# #             "emergency",
# #             "urgent",
# #             "ambulance",
# #             "immunization",
# #             "vaccination",
# #             "cancer",
# #             "dialysis",
# #             "deductible",
# #             "copay",
# #             "coinsurance",
# #             "surgery",
# #             "inpatient",
# #             "outpatient",
# #             "specialist",
# #             "lab",
# #             "imaging",
# #             "xray",
# #             "mri",
# #             "physical",
# #             "therapy",
# #             "mental",
# #             "behavioral",
# #             "maternity",
# #             "newborn",
# #             "transplant",
# #             "bariatric",
# #             "rehab",
# #             "rehabilitation",
# #             "skilled",
# #             "nursing",
# #             "home health",
# #             "hospice",
# #             "preventive",
# #             "allergy",
# #             "infusion",
# #             "chemotherapy",
# #             "radiation",
# #             "dialysis",
# #             "out-of-pocket",
# #             "oop",
# #             "deductible",
# #             "premium",
# #             "network",
# #             "prior authorization",
# #             "referral",
# #             "claim",
# #             "benefit",
# #             "coverage",
# #             "covered",
# #             "cover",
# #             "plan",
# #             "cost",
# #         ]
# #     ):
# #         return "medical"

# #     return None  # ambiguous — caller decides


# # def detect_category(query_words, query):
# #     category = None

# #     # ── Step 1: Rx signals (before dental/medical to avoid ambiguity)
# #     if any(
# #         w in query_words
# #         for w in [
# #             "drug",
# #             "drugs",
# #             "medication",
# #             "medications",
# #             "formulary",
# #             "generic",
# #             "brand",
# #             "tier",
# #             "refill",
# #             "pill",
# #             "tablet",
# #             "capsule",
# #             "pharmacy",
# #             "prior auth",
# #         ]
# #     ) or any(w.replace("mg", "").isdigit() for w in query_words if "mg" in w):
# #         print("[*] CATEGORY MATCH → rx")
# #         return "rx"

# #     # ── Step 1b: Drug name match — catches queries with no other rx signal
# #     # e.g. "does metformin require prior authorization?"
# #     # Checked against real drug names from the Rx index — not guessed patterns
# #     if is_drug_name_query(query_words):
# #         print("[*] CATEGORY MATCH → rx (drug name found in query)")
# #         return "rx"

# #     # ── Step 2: Dental procedure terms (high precision)
# #     _dental_proc_terms = [
# #         "dental",
# #         "dentist",
# #         "tooth",
# #         "teeth",
# #         "gum",
# #         "cavity",
# #         "filling",
# #         "crown",
# #         "denture",
# #         "molar",
# #         "canal",
# #         "implant",
# #         "tmj",
# #         "jaw",
# #         "orthodontic",
# #         "orthodontia",
# #         "panoramic",
# #         "sealant",
# #         "fluoride",
# #         "class",
# #         "prophylaxis",
# #         "cleaning",
# #         "extraction",
# #         "periodontal",
# #         "scaling",
# #         "nitrous",
# #         "apicoectomy",
# #         "retrograde",
# #         "bridge",
# #         "veneer",
# #         "inlay",
# #         "onlay",
# #         "prosthodontic",
# #         "endodontic",
# #         "bitewing",
# #         "periapical",
# #         "braces",
# #         "ortho",
# #     ]

# #     if any(w in query_words for w in _dental_proc_terms):
# #         print("[*] CATEGORY MATCH → dental (procedure)")
# #         return "dental"

# #     # ── Step 3: Vision
# #     if any(
# #         w in query_words
# #         for w in [
# #             "vision",
# #             "eye",
# #             "glasses",
# #             "lens",
# #             "lenses",
# #             "contacts",
# #             "frames",
# #             "optometrist",
# #             "ophthalmologist",
# #             "bifocal",
# #             "sunglasses",
# #             "eyewear",
# #             "contact",
# #         ]
# #     ):
# #         print("[*] CATEGORY MATCH → vision")
# #         return "vision"

# #     # ── Step 4: Medical — specific medical terms only
# #     # Note: generic words like "covered", "plan", "benefit", "network" removed
# #     # to avoid false positives on drug name queries like "is vivjoa covered?"
# #     _medical_terms = [
# #         "medical",
# #         "doctor",
# #         "hospital",
# #         "pcp",
# #         "emergency",
# #         "urgent",
# #         "ambulance",
# #         "immunization",
# #         "vaccination",
# #         "cancer",
# #         "dialysis",
# #         "deductible",
# #         "copay",
# #         "coinsurance",
# #         "surgery",
# #         "inpatient",
# #         "outpatient",
# #         "specialist",
# #         "imaging",
# #         "xray",
# #         "mri",
# #         "therapy",
# #         "mental",
# #         "behavioral",
# #         "maternity",
# #         "newborn",
# #         "transplant",
# #         "bariatric",
# #         "rehab",
# #         "rehabilitation",
# #         "skilled",
# #         "nursing",
# #         "hospice",
# #         "preventive",
# #         "allergy",
# #         "infusion",
# #         "chemotherapy",
# #         "radiation",
# #         # Additional unambiguously medical terms — eliminates LLM category calls
# #         "immunotherapy",
# #         "therapeutic",
# #         "vasectomy",
# #         "dialysis",
# #         "reconstruction",
# #         "gender",
# #         "affirming",
# #         "clinical",
# #         "trial",
# #         "trials",
# #         "foot",
# #         "home",
# #         "health",
# #         "electronic",
# #         "virtual",
# #         "nicotine",
# #         "psychological",
# #         "blood",
# #         "products",
# #         "transplants",
# #         "authorization",
# #         "transportation",
# #         "newborn",
# #         "impatient",
# #     ]

# #     if any(w in query_words for w in _medical_terms):
# #         print(f"[*] CATEGORY MATCH → medical")
# #         return "medical"

# #     # ── LLM fallback — only when rule-based fails
# #     print("[*] CATEGORY NOT FOUND → CALLING LLM")
# #     return get_category_from_llm(query)


# # def detect_category_from_history(history, limit=3):
# #     for msg in reversed(history[-limit:]):
# #         if msg["role"] == "user":
# #             query_lower = msg["content"].lower()
# #             query_words = [re.sub(r"[^\w\s]", "", w) for w in query_lower.split()]
# #             cat = detect_category(query_words, query_lower)
# #             if cat:
# #                 return cat
# #     return None


# # def extract_user_queries(recent_history):
# #     queries = []
# #     for msg in recent_history:
# #         if msg.get("role") == "user":
# #             queries.append(msg.get("content", "").lower())
# #     return queries
