import re
import json
import os
import ollama

from .utils import smart_match

LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

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
    follow-up clarification, acknowledgement etc.) and should NOT go
    through the RAG pipeline.
    """
    q = query.strip().lower()

    # Very short queries with no benefit keywords are likely conversational
    words = [w for w in re.sub(r"[^\w\s]", "", q).split() if len(w) > 1]
    if len(words) <= 3:
        # Check it doesn't contain any benefit signal words
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
        }
        if not any(w in benefit_signals for w in words):
            # Check against conversational patterns
            for pattern in _CONVERSATIONAL_RE:
                if pattern.match(q):
                    return True

    # Longer queries — only flag if they explicitly match conversational patterns
    for pattern in _CONVERSATIONAL_RE:
        if pattern.match(q):
            return True

    return False


def build_category_prompt(query: str) -> str:
    return f"""
        You are a strict JSON classifier for a health insurance assistant.

        Classify the query into ONE of these categories:
        medical, dental, vision, or unknown.

        Use "unknown" when:
        - The query is a greeting (hi, hello, how are you)
        - The query is a follow-up or clarification (I did not get it, can you explain)
        - The query is an acknowledgement (ok, thanks, got it)
        - The query is NOT about health insurance benefits

        Return ONLY this exact JSON format:

        {{
        "category": "medical"
        }}

        - The key MUST be "category"
        - The value MUST be one of: medical, dental, vision, unknown
        - Do NOT return anything else
        - Do NOT return null

        "{query}"
        """


def get_category_from_llm(query: str) -> str | None:
    prompt = build_category_prompt(query)
    llm_messages = [{"role": "user", "content": prompt}]

    try:
        llm_response = ollama.chat(
            model=LOCAL_MODEL,
            messages=llm_messages,
            format="json",
            options={"temperature": 0.0, "num_ctx": 8192},
        )
        content = llm_response["message"]["content"]
        print(f"[*] RAW LLM CATEGORY RESPONSE: {content}")
        data = json.loads(content)
        category = data.get("category", "").strip().lower()

        if category == "unknown":
            print("[*] LLM CATEGORY: unknown (conversational/non-benefit query)")
            return None

        if category not in {"medical", "dental", "vision"}:
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
    Used for history boundary detection to avoid LLM calls on past queries.
    """
    if any(
        w in query_words
        for w in [
            "dental",
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
        ]
    ):
        return "dental"

    if any(
        w in query_words
        for w in ["vision", "eye", "glasses", "lens", "lenses", "contacts"]
    ):
        return "vision"

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
            "pharmacy",
            "prescription",
        ]
    ):
        return "medical"

    return None  # ambiguous — caller decides


def detect_category(query_words, query):
    category = None

    if any(
        w in query_words
        for w in [
            "dental",
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
        ]
    ):
        print("[*] CATEGORY MATCH → dental")
        return "dental"

    if any(
        w in query_words
        for w in ["vision", "eye", "glasses", "lens", "lenses", "contacts"]
    ):
        print("[*] CATEGORY MATCH → vision")
        return "vision"

    _dental_proc_terms = [
        "sealant",
        "filling",
        "fluoride",
        "prophylaxis",
        "cleaning",
        "extraction",
        "periodontal",
        "scaling",
        "anesthesia",
        "sedation",
        "nitrous",
        "apicoectomy",
        "retrograde",
        "veneer",
        "onlay",
        "inlay",
    ]
    if any(smart_match(w, query_words, query.lower()) for w in _dental_proc_terms):
        print("[*] CATEGORY MATCH → dental (procedure)")
        return "dental"

    if any(
        w in query_words
        for w in [
            "medical",
            "doctor",
            "health",
            "hospital",
            "pcp",
            "emergency",
            "er",
            "urgent",
            "ambulance",
            "room",
            "immunization",
            "immunizations",
            "vaccination",
            "cancer",
        ]
    ):
        print("[*] CATEGORY MATCH → medical")
        return "medical"

    print("[*] CATEGORY NOT FOUND → CALLING LLM")
    if category is None:
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
