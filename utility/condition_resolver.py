"""
condition_resolver.py — Standalone condition-to-drug mapping service.

Resolves patient condition queries to matching drug names from the formulary.

Two-pass design (built once at index time, zero LLM cost at query time):
    Pass 1: drug → illness terms         (drug_names.json)
    Pass 2: illness → synonyms           (condition_synonyms.json)

Query flow:
    "blood pressure medication"
        ↓
    extract_condition_terms() → ["blood", "pressure", "blood pressure"]  # unigrams + bigrams
        ↓
    find_canonical_condition() → "hypertension"  # synonym lookup
        ↓
    get_drugs_for_condition() → ["lisinopril", "amlodipine", "losartan"]
        ↓
    caller looks up formulary for each drug

Standalone — no dependencies on RAG pipeline, client.py, or category.py.
Can be imported by any component that needs condition→drug resolution.

LLM fallback:
    If a condition term isn't found in condition_synonyms.json,
    falls back to LLM to resolve it, then caches the result for next time.
    Over time the cache grows to cover more terms, reducing LLM calls.
"""

import os
import re
import json
from datetime import datetime, timedelta

# ── File paths ────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRUG_NAMES_FILE = os.path.join(_BASE_DIR, "indices", "drug_names.json")
CONDITION_SYNONYMS_FILE = os.path.join(_BASE_DIR, "indices", "condition_synonyms.json")

# ── In-memory cache with 48-hour TTL ─────────────────────────────────────────
_drug_names_data: dict[str, list] = {}  # drug_word → [illness terms]
_condition_synonyms_data: dict[str, list] = {}  # condition → [synonyms]
_drug_names_loaded_at: datetime | None = None
_condition_synonyms_loaded_at: datetime | None = None
_CACHE_TTL = timedelta(hours=48)

# ── Stopwords — ignored when extracting condition terms from query ─────────────
_QUERY_STOPWORDS = {
    "i",
    "my",
    "me",
    "we",
    "our",
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "does",
    "do",
    "did",
    "will",
    "can",
    "could",
    "would",
    "should",
    "have",
    "has",
    "had",
    "what",
    "which",
    "who",
    "how",
    "when",
    "where",
    "why",
    "want",
    "need",
    "know",
    "about",
    "show",
    "tell",
    "give",
    "get",
    "find",
    "for",
    "in",
    "on",
    "at",
    "to",
    "of",
    "and",
    "or",
    "but",
    "with",
    "plan",
    "covered",
    "cover",
    "covers",
    "coverage",
    "benefit",
    "benefits",
    "drug",
    "drugs",
    "medication",
    "medications",
    "medicine",
    "medicines",
    "meds",
    "pill",
    "pills",
    "prescription",
    "formulary",
    "tier",
    "cost",
    "pay",
    "copay",
    "coinsurance",
    "deductible",
    "any",
    "all",
    "some",
    "more",
    "much",
    "many",
    "few",
    "please",
    "help",
    "information",
    "info",
}


# ── Data loading ──────────────────────────────────────────────────────────────


def _load_drug_names() -> dict[str, list]:
    """
    Loads drug_names.json into memory with 48-hour TTL cache.
    Structure: {"metformin": ["diabetes", "blood sugar"], ...}
    """
    global _drug_names_data, _drug_names_loaded_at

    now = datetime.utcnow()
    if _drug_names_loaded_at is not None and (now - _drug_names_loaded_at) < _CACHE_TTL:
        return _drug_names_data

    try:
        if os.path.exists(DRUG_NAMES_FILE):
            with open(DRUG_NAMES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                data = {word: [] for word in data}
        else:
            data = {}
            print(
                f"[!] condition_resolver: drug_names.json not found at {DRUG_NAMES_FILE}"
            )
    except Exception as e:
        print(f"[!] condition_resolver: failed to load drug_names.json: {e}")
        data = _drug_names_data or {}

    _drug_names_data = data
    _drug_names_loaded_at = now
    return _drug_names_data


def _load_condition_synonyms() -> dict[str, list]:
    """
    Loads condition_synonyms.json into memory with 48-hour TTL cache.
    Structure: {"diabetes": ["blood sugar", "type 2", "high blood sugar"], ...}
    """
    global _condition_synonyms_data, _condition_synonyms_loaded_at

    now = datetime.utcnow()
    if (
        _condition_synonyms_loaded_at is not None
        and (now - _condition_synonyms_loaded_at) < _CACHE_TTL
    ):
        return _condition_synonyms_data

    try:
        if os.path.exists(CONDITION_SYNONYMS_FILE):
            with open(CONDITION_SYNONYMS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
            print(
                f"[!] condition_resolver: condition_synonyms.json not found — "
                f"run rx_indexer with --synonyms to generate it"
            )
    except Exception as e:
        print(f"[!] condition_resolver: failed to load condition_synonyms.json: {e}")
        data = _condition_synonyms_data or {}

    _condition_synonyms_data = data
    _condition_synonyms_loaded_at = now
    return _condition_synonyms_data


def _save_condition_synonyms(data: dict) -> None:
    """Saves condition_synonyms.json — called when LLM fallback adds new entries."""
    global _condition_synonyms_data, _condition_synonyms_loaded_at
    try:
        os.makedirs(os.path.dirname(CONDITION_SYNONYMS_FILE), exist_ok=True)
        with open(CONDITION_SYNONYMS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        _condition_synonyms_data = data
        _condition_synonyms_loaded_at = datetime.utcnow()
    except Exception as e:
        print(f"[!] condition_resolver: failed to save condition_synonyms.json: {e}")


# ── Term extraction ───────────────────────────────────────────────────────────


def extract_condition_terms(query: str) -> list[str]:
    """
    Extracts candidate condition terms from a query using unigram + bigram + trigram.

    Filters out stopwords and very short words. Returns candidates longest-first
    so phrase matches are preferred over single-word matches.

    Example:
        "I want to know about my blood pressure medication"
        → ["blood pressure", "blood", "pressure"]
          (trigrams filtered as too noisy; bigrams + unigrams returned)
    """
    # Normalize
    query_clean = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    words = [
        w for w in query_clean.split() if w not in _QUERY_STOPWORDS and len(w) >= 4
    ]

    candidates = []

    # Trigrams
    for i in range(len(words) - 2):
        candidates.append(f"{words[i]} {words[i+1]} {words[i+2]}")

    # Bigrams
    for i in range(len(words) - 1):
        candidates.append(f"{words[i]} {words[i+1]}")

    # Unigrams
    candidates.extend(words)

    # Deduplicate preserving order (longest first due to tri→bi→uni ordering)
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)

    return result


# ── Core resolution ───────────────────────────────────────────────────────────


def find_canonical_condition(term: str) -> str | None:
    """
    Given a patient term (e.g. "blood pressure"), returns the canonical
    condition name (e.g. "hypertension") by scanning condition_synonyms.json.

    Checks both the canonical keys AND their synonym lists.

    Returns None if the term is not found in any synonym list.
    """
    synonyms_data = _load_condition_synonyms()
    term_lower = term.lower().strip()

    # Direct key match (term IS a canonical condition)
    if term_lower in synonyms_data:
        return term_lower

    # Scan synonym lists
    for canonical, synonyms in synonyms_data.items():
        if term_lower in [s.lower() for s in synonyms]:
            return canonical

    return None


def get_drugs_for_condition(condition: str) -> list[str]:
    """
    Returns all drug words from drug_names.json whose illness terms
    include the given condition or any of its synonyms.

    Example:
        get_drugs_for_condition("hypertension")
        → ["lisinopril", "amlodipine", "losartan", "metoprolol"]

        get_drugs_for_condition("blood pressure")  # synonym of hypertension
        → same list
    """
    drug_data = _load_drug_names()
    synonyms_data = _load_condition_synonyms()

    condition_lower = condition.lower().strip()

    # Build the full set of terms to match against:
    # the condition itself + all its synonyms
    match_terms = {condition_lower}
    if condition_lower in synonyms_data:
        match_terms.update(s.lower() for s in synonyms_data[condition_lower])
    else:
        # Check if it's a synonym of something
        for canonical, synonyms in synonyms_data.items():
            if condition_lower in [s.lower() for s in synonyms]:
                match_terms.add(canonical)
                match_terms.update(s.lower() for s in synonyms)
                break

    # Find all drugs whose illness terms intersect with match_terms
    matching_drugs = []
    for drug_word, illness_terms in drug_data.items():
        drug_illness_lower = [t.lower() for t in illness_terms]
        if any(term in drug_illness_lower for term in match_terms):
            matching_drugs.append(drug_word)

    return sorted(matching_drugs)


def get_conditions_for_drug(drug: str) -> list[str]:
    """
    Returns the illness terms for a given drug word.

    Example:
        get_conditions_for_drug("metformin")
        → ["diabetes", "blood sugar", "high blood sugar"]
    """
    drug_data = _load_drug_names()
    return drug_data.get(drug.lower(), [])


def expand_condition(term: str) -> list[str]:
    """
    Returns all synonyms for a given condition term, including the term itself.

    Example:
        expand_condition("hypertension")
        → ["hypertension", "blood pressure", "high blood pressure", "high bp", "bp"]

        expand_condition("blood pressure")  # synonym → expands to same set
        → ["hypertension", "blood pressure", "high blood pressure", "high bp", "bp"]
    """
    synonyms_data = _load_condition_synonyms()
    term_lower = term.lower().strip()

    # Direct match
    if term_lower in synonyms_data:
        return [term_lower] + synonyms_data[term_lower]

    # Reverse lookup — term is a synonym
    for canonical, synonyms in synonyms_data.items():
        if term_lower in [s.lower() for s in synonyms]:
            return [canonical] + synonyms

    return [term_lower]  # unknown term — return as-is


def resolve_query_to_drugs(query: str, use_llm_fallback: bool = True) -> list[str]:
    """
    Main entry point — resolves a free-text query to matching drug words.

    Steps:
        1. Extract condition terms (unigrams + bigrams + trigrams)
        2. For each candidate term, look up condition_synonyms.json
        3. If found → get all drugs for that condition
        4. If not found and use_llm_fallback=True → call LLM, cache result

    Returns deduplicated list of drug words, or [] if no match found.

    Example:
        resolve_query_to_drugs("I want to know about my blood pressure medication")
        → ["amlodipine", "lisinopril", "losartan", "metoprolol"]
    """
    candidates = extract_condition_terms(query)

    if not candidates:
        return []

    # Try each candidate — return on first match (longest match wins due to ordering)
    for term in candidates:
        canonical = find_canonical_condition(term)
        if canonical:
            drugs = get_drugs_for_condition(canonical)
            if drugs:
                print(
                    f"[*] condition_resolver: '{term}' → '{canonical}' → {len(drugs)} drugs"
                )
                return drugs

    # LLM fallback — ask LLM what condition the query is about
    if use_llm_fallback:
        condition = _resolve_condition_via_llm(query, candidates)
        if condition:
            drugs = get_drugs_for_condition(condition)
            if drugs:
                print(
                    f"[*] condition_resolver: LLM resolved to '{condition}' → {len(drugs)} drugs"
                )
                return drugs

    print(f"[*] condition_resolver: no condition match found for query: '{query}'")
    return []


# ── LLM fallback ─────────────────────────────────────────────────────────────


def _resolve_condition_via_llm(query: str, candidates: list[str]) -> str | None:
    """
    Calls LLM to identify the medical condition in the query.
    Caches the result in condition_synonyms.json for future use.

    Only called when condition_synonyms.json lookup fails.
    """
    try:
        from utility.llm import llm_chat

        candidate_str = ", ".join(candidates[:5])  # top 5 candidates
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical terminology assistant. "
                    "Given a patient query, identify the ONE main medical condition being asked about. "
                    "Return ONLY a JSON object in this exact format:\n"
                    '{"condition": "diabetes", "synonyms": ["blood sugar", "type 2", "high blood sugar"]}\n\n'
                    "Rules:\n"
                    "- condition: the canonical medical name (1-3 words, lowercase)\n"
                    "- synonyms: 3-6 patient-friendly ways to say the same thing\n"
                    '- If no clear medical condition, return: {"condition": "", "synonyms": []}\n'
                    "- Return ONLY the JSON, nothing else"
                ),
            },
            {
                "role": "user",
                "content": f"Query: {query}\nCandidate terms: {candidate_str}",
            },
        ]

        response = llm_chat(messages=messages, format="json", max_tokens=100)
        if not response:
            return None

        data = json.loads(response.strip())
        condition = data.get("condition", "").strip().lower()
        synonyms = data.get("synonyms", [])

        if not condition:
            return None

        # Cache in condition_synonyms.json
        synonyms_data = _load_condition_synonyms()
        if condition not in synonyms_data:
            synonyms_data[condition] = [s.lower() for s in synonyms if s.strip()]
        else:
            # Merge new synonyms with existing
            existing = set(synonyms_data[condition])
            existing.update(s.lower() for s in synonyms if s.strip())
            synonyms_data[condition] = sorted(existing)

        # Also add reverse mappings for each candidate that led here
        for candidate in candidates[:3]:
            if candidate not in synonyms_data.get(condition, []):
                synonyms_data.setdefault(condition, [])
                if candidate not in synonyms_data[condition]:
                    synonyms_data[condition].append(candidate)

        _save_condition_synonyms(synonyms_data)
        print(
            f"[*] condition_resolver: LLM identified '{condition}' → cached in condition_synonyms.json"
        )
        return condition

    except Exception as e:
        print(f"[!] condition_resolver: LLM fallback failed: {e}")
        return None
