import re
import json
import os
import glob

from config import settings
from .utils import smart_match

# ── Drug name word lookup ──────────────────────────────────────────────────────
# Built once from all Rx index files on disk. Used to detect drug-name queries
# like "does metformin require prior authorization?" where the only signal
# that this is an Rx question is the drug name itself — no other rx keyword
# like "drug", "formulary", "pharmacy" appears in the query.
#
# This is safe (not fragile) because it checks against REAL drug names from
# the actual indexed documents — not guessed patterns. "metformin" matches
# because it IS a drug name in the index. "bariatric" never matches because
# it is not a drug name anywhere in the Rx index.

_DRUG_NAME_WORDS: set[str] = set()
_drug_words_loaded = False

# Common English/medical words that appear in drug names but aren't drug names
# themselves — must be excluded so they don't cause false positives elsewhere
_DRUG_WORD_STOPLIST = {
    "oral",
    "tablet",
    "tablets",
    "capsule",
    "capsules",
    "injection",
    "injectable",
    "solution",
    "suspension",
    "cream",
    "ointment",
    "gel",
    "patch",
    "spray",
    "drops",
    "extended",
    "release",
    "delayed",
    "for",
    "and",
    "with",
    "the",
    "mg",
    "ml",
    "mcg",
    "unit",
    "units",
    "per",
    "reconstitution",
    "dispersion",
    "topical",
    "inhalation",
    "nasal",
    "vaginal",
    "rectal",
    "subcutaneous",
    "intramuscular",
    "intravenous",
    # Generic descriptor words that appear in drug names but aren't drug
    # identifiers themselves — would cause false positives in category detection
    "dental",
    "paste",
    "sensitive",
    "defense",
    "protect",
    "booster",
    "kids",
    "daily",
    "plus",
    "starter",
    "package",
    "kit",
    "complete",
    "implant",
    "maintenance",
    "emergency",
    "fluoride",
    "nicotine",
}


def _load_drug_name_words() -> set:
    """
    Loads all drug name words from Rx index files in the indices/ folder.
    Cached after first call — only loads once per process.
    """
    global _DRUG_NAME_WORDS, _drug_words_loaded

    if _drug_words_loaded:
        return _DRUG_NAME_WORDS

    indices_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "indices"
    )
    rx_index_files = glob.glob(os.path.join(indices_dir, "*rx*.json"))

    words = set()
    for index_path in rx_index_files:
        try:
            with open(index_path, encoding="utf-8") as f:
                chunks = json.load(f)
            for chunk in chunks:
                content = chunk.get("content", {})
                if not isinstance(content, dict):
                    continue
                drug_name = content.get("drug_name", "")
                if not drug_name:
                    continue
                for word in re.findall(r"[a-zA-Z]+", drug_name.lower()):
                    if len(word) > 4 and word not in _DRUG_WORD_STOPLIST:
                        words.add(word)
        except Exception as e:
            print(f"[!] Failed to load drug names from {index_path}: {e}")

    _DRUG_NAME_WORDS = words
    _drug_words_loaded = True
    print(f"[*] Loaded {len(words)} drug name words for rx category detection")
    return _DRUG_NAME_WORDS


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


# ── Conversational patterns — queries that are NOT benefit questions ──────────
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
            "implant",
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

    # ── Step 2: Dental procedure terms (high precision)
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
        "implant",
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


##===========================Previous working code before making LLM cost changes===========================##
# # import re
# # import json
# # import os
# # from config import settings
# # import ollama

# # from .utils import smart_match

# # LOCAL_MODEL = settings.OLLAMA_MODEL

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
# #     follow-up clarification, acknowledgement etc.) and should NOT go
# #     through the RAG pipeline.
# #     """
# #     q = query.strip().lower()

# #     # Very short queries with no benefit keywords are likely conversational
# #     words = [w for w in re.sub(r"[^\w\s]", "", q).split() if len(w) > 1]
# #     if len(words) <= 3:
# #         # Check it doesn't contain any benefit signal words
# #         benefit_signals = {
# #             "copay",
# #             "cost",
# #             "cover",
# #             "covered",
# #             "benefit",
# #             "deductible",
# #             "plan",
# #             "insurance",
# #             "medical",
# #             "dental",
# #             "vision",
# #             "pcp",
# #             "doctor",
# #             "hospital",
# #             "er",
# #             "urgent",
# #             "claim",
# #             "premium",
# #             "coinsurance",
# #             "network",
# #             "provider",
# #             "drug",
# #             "prescription",
# #         }
# #         if not any(w in benefit_signals for w in words):
# #             # Check against conversational patterns
# #             for pattern in _CONVERSATIONAL_RE:
# #                 if pattern.match(q):
# #                     return True

# #     # Longer queries — only flag if they explicitly match conversational patterns
# #     for pattern in _CONVERSATIONAL_RE:
# #         if pattern.match(q):
# #             return True

# #     return False


# # def build_category_prompt(query: str) -> str:
# #     return f"""
# #         You are a strict JSON classifier for a health insurance assistant.

# #         Classify the query into ONE of these categories:
# #         medical, dental, vision, or unknown.

# #         Use "unknown" when:
# #         - The query is a greeting (hi, hello, how are you)
# #         - The query is a follow-up or clarification (I did not get it, can you explain)
# #         - The query is an acknowledgement (ok, thanks, got it)
# #         - The query is NOT about health insurance benefits

# #         Return ONLY this exact JSON format:

# #         {{
# #         "category": "medical"
# #         }}

# #         - The key MUST be "category"
# #         - The value MUST be one of: medical, dental, vision, unknown
# #         - Do NOT return anything else
# #         - Do NOT return null

# #         "{query}"
# #         """


# # def get_category_from_llm(query: str) -> str | None:
# #     prompt = build_category_prompt(query)
# #     llm_messages = [{"role": "user", "content": prompt}]

# #     try:
# #         llm_response = ollama.chat(
# #             model=LOCAL_MODEL,
# #             messages=llm_messages,
# #             format="json",
# #             options={"temperature": 0.0, "num_ctx": 8192},
# #         )
# #         content = llm_response["message"]["content"]
# #         print(f"[*] RAW LLM CATEGORY RESPONSE: {content}")
# #         data = json.loads(content)
# #         category = data.get("category", "").strip().lower()

# #         if category == "unknown":
# #             print("[*] LLM CATEGORY: unknown (conversational/non-benefit query)")
# #             return None

# #         if category not in {"medical", "dental", "vision"}:
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
# #     Used for history boundary detection to avoid LLM calls on past queries.
# #     """
# #     if any(
# #         w in query_words
# #         for w in [
# #             "dental",
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
# #         ]
# #     ):
# #         return "dental"

# #     if any(
# #         w in query_words
# #         for w in ["vision", "eye", "glasses", "lens", "lenses", "contacts"]
# #     ):
# #         return "vision"

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
# #             "pharmacy",
# #             "prescription",
# #         ]
# #     ):
# #         return "medical"

# #     return None  # ambiguous — caller decides


# # def detect_category(query_words, query):
# #     category = None

# #     if any(
# #         w in query_words
# #         for w in [
# #             "dental",
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
# #         ]
# #     ):
# #         print("[*] CATEGORY MATCH → dental")
# #         return "dental"

# #     if any(
# #         w in query_words
# #         for w in ["vision", "eye", "glasses", "lens", "lenses", "contacts"]
# #     ):
# #         print("[*] CATEGORY MATCH → vision")
# #         return "vision"

# #     _dental_proc_terms = [
# #         "sealant",
# #         "filling",
# #         "fluoride",
# #         "prophylaxis",
# #         "cleaning",
# #         "extraction",
# #         "periodontal",
# #         "scaling",
# #         "anesthesia",
# #         "sedation",
# #         "nitrous",
# #         "apicoectomy",
# #         "retrograde",
# #         "veneer",
# #         "onlay",
# #         "inlay",
# #     ]
# #     if any(smart_match(w, query_words, query.lower()) for w in _dental_proc_terms):
# #         print("[*] CATEGORY MATCH → dental (procedure)")
# #         return "dental"

# #     if any(
# #         w in query_words
# #         for w in [
# #             "medical",
# #             "doctor",
# #             "health",
# #             "hospital",
# #             "pcp",
# #             "emergency",
# #             "er",
# #             "urgent",
# #             "ambulance",
# #             "room",
# #             "immunization",
# #             "immunizations",
# #             "vaccination",
# #             "cancer",
# #         ]
# #     ):
# #         print("[*] CATEGORY MATCH → medical")
# #         return "medical"

# #     print("[*] CATEGORY NOT FOUND → CALLING LLM")
# #     if category is None:
# #         return get_category_from_llm(query)


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
