"""
condition_resolver.py — Standalone condition-to-drug mapping service.

Resolves patient condition queries to matching drug names from the formulary.

Architecture:
    drug_words.json         → {word: {entry_type, full_names, illnesses[]}}
    condition_synonyms.json → {condition: [synonyms]}

Query flow:
    "blood pressure medication"
        ↓
    extract_condition_terms() → ["blood pressure", "blood", "pressure"]
        ↓
    find_canonical_condition() → "Hypertension"   ← from condition_synonyms.json
        ↓
    get_drugs_for_condition() → ["lisinopril", "amlodipine", "losartan"]
        ↓
    caller looks up formulary for each drug

Status of illnesses[]:
    - Empty until build_rxclass_lookup.py runs (requires UMLS RxNorm files)
    - When empty, condition resolution returns [] gracefully — no crash
    - LLM fallback in resolve_query_to_drugs() still works but also
      returns [] because illnesses[] is empty
    - Full resolution works once build_rxclass_lookup.py populates illnesses[]

Standalone — no dependencies on RAG pipeline, client.py, or category.py.
"""

import os
import re
import json
from datetime import datetime, timedelta

# -- File paths ----------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRUG_WORDS_FILE = os.path.join(_BASE_DIR, "indices", "drug_words.json")
CONDITION_SYNONYMS_FILE = os.path.join(_BASE_DIR, "indices", "condition_synonyms.json")

# -- In-memory cache with 48-hour TTL -----------------------------------------
_drug_illness_data: dict = {}  # drug_word -> [illness_terms]
_drug_words_full_data: dict = {}  # drug_word -> full entry (entry_type, illnesses)
_condition_synonyms_data: dict = {}  # condition -> [synonyms]
_drug_illness_loaded_at: datetime | None = None
_condition_synonyms_loaded_at: datetime | None = None
_CACHE_TTL = timedelta(hours=48)

# -- Stopwords -----------------------------------------------------------------
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
    "treat",
    "treating",
    "treatment",
    "used",
    "take",
    "taking",
}


# -- Normalization -------------------------------------------------------------


def _normalize_condition_name(name: str) -> str:
    """
    Normalize condition names to consistent format.
    MED-RT uses commas:  "Diabetes Mellitus, Type 2"
    LLM drops commas:    "Diabetes Mellitus Type 2"
    We normalize to no-comma format so both map to same key.

    Fix: preserve the type qualifier after the comma.
        "Diabetes Mellitus, Type 2" -> "Diabetes Mellitus Type 2"  (comma removed)
        "Arthritis, Rheumatoid"     -> "Arthritis, Rheumatoid"     (kept — not a type qualifier)
    """
    # Remove comma ONLY before "Type N" qualifiers
    name = re.sub(r",\s*(Type\s+\d)", r" \1", name)
    return name.strip()


# -- Data loading --------------------------------------------------------------


def _load_drug_illness() -> dict:
    """
    Load drug_words.json and extract {drug_word: [illness_terms]}.
    Also populates _drug_words_full_data for entry_type filtering.

    Returns empty dict if illnesses[] not yet populated.
    Does NOT crash — callers handle empty gracefully.
    """
    global _drug_illness_data, _drug_words_full_data, _drug_illness_loaded_at

    now = datetime.utcnow()
    if (
        _drug_illness_loaded_at is not None
        and (now - _drug_illness_loaded_at) < _CACHE_TTL
    ):
        return _drug_illness_data

    data = {}
    full_data = {}
    try:
        if os.path.exists(DRUG_WORDS_FILE):
            with open(DRUG_WORDS_FILE, encoding="utf-8") as f:
                raw = json.load(f)

            if raw:
                first_val = list(raw.values())[0]
                if isinstance(first_val, dict) and "illnesses" in first_val:
                    for word, entry in raw.items():
                        full_data[word] = entry  # preserve full entry
                        illnesses = entry.get("illnesses", [])
                        if illnesses:
                            # Normalize condition names on load
                            data[word] = [
                                _normalize_condition_name(ill) for ill in illnesses
                            ]
                elif isinstance(first_val, list):
                    # Old format {word: [illnesses]}
                    data = {k: v for k, v in raw.items() if v}

            drugs_with_illnesses = len(data)
            total = len(raw) if raw else 0
            if drugs_with_illnesses == 0:
                print(
                    f"[!] condition_resolver: drug_words.json has {total} drugs "
                    f"but illnesses[] is empty — run build_rxclass_lookup.py first"
                )
            else:
                print(
                    f"[*] condition_resolver: {drugs_with_illnesses}/{total} "
                    f"drugs have illness mappings"
                )
        else:
            print(
                f"[!] condition_resolver: drug_words.json not found — "
                f"run rx_indexer first"
            )

    except Exception as e:
        print(f"[!] condition_resolver: failed to load drug_words.json: {e}")
        data = _drug_illness_data or {}
        full_data = _drug_words_full_data or {}

    _drug_illness_data = data
    _drug_words_full_data = full_data
    _drug_illness_loaded_at = now
    return _drug_illness_data


def _load_condition_synonyms() -> dict:
    """
    Load condition_synonyms.json.
    Returns empty dict if not yet generated — callers handle gracefully.
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
                raw = json.load(f)
            # Normalize keys so "Diabetes Mellitus, Type 2" and
            # "Diabetes Mellitus Type 2" both map to same entry
            data = {}
            for key, synonyms in raw.items():
                norm_key = _normalize_condition_name(
                    key
                ).lower()  # lowercase for consistent matching
                if norm_key not in data:
                    data[norm_key] = synonyms
                else:
                    existing = set(data[norm_key])
                    existing.update(synonyms)
                    data[norm_key] = sorted(existing)
        else:
            data = {}
            print(
                f"[!] condition_resolver: condition_synonyms.json not found — "
                f"run: python -m indexers.rx_classifier --synonyms"
            )
    except Exception as e:
        print(f"[!] condition_resolver: failed to load condition_synonyms.json: {e}")
        data = _condition_synonyms_data or {}

    _condition_synonyms_data = data
    _condition_synonyms_loaded_at = now
    return _condition_synonyms_data


def _save_condition_synonyms(data: dict) -> None:
    """Save condition_synonyms.json after LLM fallback adds new entries."""
    global _condition_synonyms_data, _condition_synonyms_loaded_at
    try:
        os.makedirs(os.path.dirname(CONDITION_SYNONYMS_FILE), exist_ok=True)
        with open(CONDITION_SYNONYMS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        _condition_synonyms_data = data
        _condition_synonyms_loaded_at = datetime.utcnow()
    except Exception as e:
        print(f"[!] condition_resolver: failed to save condition_synonyms.json: {e}")


def invalidate_cache() -> None:
    """Force cache reload on next access — call after updating drug_words.json."""
    global _drug_illness_loaded_at, _condition_synonyms_loaded_at
    _drug_illness_loaded_at = None
    _condition_synonyms_loaded_at = None


# -- Term extraction -----------------------------------------------------------


def extract_condition_terms(query: str) -> list[str]:
    """
    Extracts candidate condition terms from a query using unigram + bigram + trigram.
    Returns candidates longest-first so phrase matches win over single-word matches.
    """
    query_clean = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    words = [
        w for w in query_clean.split() if w not in _QUERY_STOPWORDS and len(w) >= 2
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

    # Deduplicate preserving order
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)

    return result


# -- Core resolution -----------------------------------------------------------

# Priority conditions — when multiple conditions share a synonym, these win.
_PRIORITY_CONDITION_MAP = {
    "diabetes": "Diabetes Mellitus Type 2",
    "blood sugar": "Diabetes Mellitus Type 2",
    "high blood sugar": "Diabetes Mellitus Type 2",
    "type 2": "Diabetes Mellitus Type 2",
    "t2d": "Diabetes Mellitus Type 2",
    "high blood pressure": "Hypertension",
    "blood pressure": "Hypertension",
    "high bp": "Hypertension",
    "bp": "Hypertension",
    "hypertension": "Hypertension",
    "migraine": "Migraine Disorders",
    "migraines": "Migraine Disorders",
    "migraine headache": "Migraine Disorders",
    "cholesterol": "Hypercholesterolemia",
    "high cholesterol": "Hypercholesterolemia",
    "bad cholesterol": "Hypercholesterolemia",
    "ldl": "Hypercholesterolemia",
    "depression": "Depressive Disorder",
    "anxiety": "Anxiety Disorders",
    "asthma": "Asthma",
    "copd": "Pulmonary Disease, Chronic Obstructive",
    "chronic lung": "Pulmonary Disease, Chronic Obstructive",
    "emphysema": "Pulmonary Disease, Chronic Obstructive",
    "arthritis": "Arthritis, Rheumatoid",
    "rheumatoid": "Arthritis, Rheumatoid",
    "seizures": "Epilepsy",
    "epilepsy": "Epilepsy",
    "blood clot": "Thromboembolism",
    "blood clots": "Thromboembolism",
    "osteoporosis": "Osteoporosis",
    "heart failure": "Heart Failure",
    "afib": "Atrial Fibrillation",
    "hiv": "HIV Infections",
    "aids": "HIV Infections",
    "hypothyroidism": "Hypothyroidism",
    "underactive thyroid": "Hypothyroidism",
    "obesity": "Obesity",
    "overweight": "Obesity",
    "gout": "Gout",
    "psoriasis": "Psoriasis",
    "eczema": "Dermatitis, Atopic",
    "crohn": "Crohn Disease",
    "ulcerative colitis": "Colitis, Ulcerative",
    "ms": "Multiple Sclerosis",
    "multiple sclerosis": "Multiple Sclerosis",
    "lupus": "Lupus Erythematosus, Systemic",
    "cystic fibrosis": "Cystic Fibrosis",
    "cf": "Cystic Fibrosis",
    "herpes zoster": "Herpes Zoster",
    "shingles": "Herpes Zoster",
    "influenza": "Influenza",
    "flu": "Influenza",
}


def find_canonical_condition(term: str) -> str | None:
    """
    Given a patient term, find the canonical condition name.

    Matching order:
    1. Priority map — common terms map to primary condition
    2. Exact key match in condition_synonyms.json
    3. Exact synonym match (prefer primary synonyms)
    4. Partial/substring match (>= 6 chars)

    Returns None if no match found.
    """
    synonyms_data = _load_condition_synonyms()
    if not synonyms_data:
        return None

    term_lower = term.lower().strip()

    # 1. Priority map
    if term_lower in _PRIORITY_CONDITION_MAP:
        priority = _PRIORITY_CONDITION_MAP[term_lower]
        norm_priority = _normalize_condition_name(priority).lower()
        return norm_priority

    # 2. Direct key match
    if term_lower in synonyms_data:
        return term_lower

    # 3. Exact synonym match
    primary_match = None
    for canonical, synonyms in synonyms_data.items():
        syns_lower = [s.lower() for s in synonyms]
        if term_lower in syns_lower:
            idx = syns_lower.index(term_lower)
            if idx == 0:
                return canonical
            elif primary_match is None:
                primary_match = canonical

    if primary_match:
        return primary_match

    # 4. Partial match — only for terms >= 6 chars
    if len(term_lower) >= 6:
        for canonical, synonyms in synonyms_data.items():
            if term_lower in canonical.lower():
                return canonical
            for synonym in synonyms:
                if term_lower in synonym.lower():
                    return canonical

    return None


def get_drugs_for_condition(condition: str) -> list[str]:
    """
    Returns all drug words whose illnesses[] includes the given condition.

    Filters out devices, vitamins and vaccines — only returns actual drugs.

    Returns [] when illnesses[] not yet populated or no match found.
    """
    drug_data = _load_drug_illness()
    synonyms_data = _load_condition_synonyms()

    if not drug_data:
        return []

    condition_lower = condition.lower().strip()

    # Build full set of terms to match against (condition + all its synonyms)
    match_terms = {condition_lower}
    if condition_lower in synonyms_data:
        match_terms.update(s.lower() for s in synonyms_data[condition_lower])
    else:
        for canonical, synonyms in synonyms_data.items():
            if condition_lower in [s.lower() for s in synonyms]:
                match_terms.add(canonical)
                match_terms.update(s.lower() for s in synonyms)
                break

    # Find drugs whose illness terms intersect match_terms
    # Skip devices, vitamins, vaccines — they have no illness relevance
    _SKIP_TYPES = {"device", "vitamin", "vaccine"}
    matching_drugs = set()

    for drug_word, illness_terms in drug_data.items():
        # Check entry_type from full data
        full_entry = _drug_words_full_data.get(drug_word, {})
        if isinstance(full_entry, dict):
            entry_type = full_entry.get("entry_type", "drug")
            if entry_type in _SKIP_TYPES:
                continue

        if isinstance(illness_terms, list):
            drug_illness_lower = [t.lower() for t in illness_terms]
        else:
            continue

        if any(term in drug_illness_lower for term in match_terms):
            matching_drugs.add(drug_word)

    return sorted(matching_drugs)


def get_conditions_for_drug(drug: str) -> list[str]:
    """Returns illness terms for a given drug word."""
    drug_data = _load_drug_illness()
    result = drug_data.get(drug.lower(), [])
    if isinstance(result, dict):
        return result.get("illnesses", [])
    return result if isinstance(result, list) else []


def expand_condition(term: str) -> list[str]:
    """
    Returns all synonyms for a condition term, including the term itself.
    """
    synonyms_data = _load_condition_synonyms()
    term_lower = term.lower().strip()

    if term_lower in synonyms_data:
        return [term_lower] + synonyms_data[term_lower]

    for canonical, synonyms in synonyms_data.items():
        if term_lower in [s.lower() for s in synonyms]:
            return [canonical] + synonyms

    return [term_lower]


def resolve_query_to_drugs(query: str, use_llm_fallback: bool = True) -> list[str]:
    """
    Main entry point — resolves a free-text query to matching drug words.

    Steps:
        1. Extract condition terms (trigrams -> bigrams -> unigrams)
        2. For each term, look up condition_synonyms.json
        3. If match -> get drugs for that condition from drug_words.json illnesses[]
        4. If no match and use_llm_fallback=True -> LLM identifies condition

    Returns [] gracefully when illnesses[] not yet populated.
    """
    candidates = extract_condition_terms(query)

    if not candidates:
        return []

    for term in candidates:
        canonical = find_canonical_condition(term)
        if canonical:
            drugs = get_drugs_for_condition(canonical)
            if drugs:
                print(
                    f"[*] condition_resolver: '{term}' -> '{canonical}' -> {len(drugs)} drugs"
                )
                return drugs

    if use_llm_fallback:
        condition = _resolve_condition_via_llm(query, candidates)
        if condition:
            drugs = get_drugs_for_condition(condition)
            if drugs:
                print(
                    f"[*] condition_resolver: LLM -> '{condition}' -> {len(drugs)} drugs"
                )
                return drugs

    print(
        f"[*] condition_resolver: no match for '{query}' "
        f"(illnesses may be empty — run build_rxclass_lookup.py)"
    )
    return []


# -- LLM fallback --------------------------------------------------------------


def _resolve_condition_via_llm(query: str, candidates: list[str]) -> str | None:
    """
    Calls LLM to identify the medical condition in the query.
    Caches the result in condition_synonyms.json for future use.
    """
    try:
        from utility.llm import llm_chat

        candidate_str = ", ".join(candidates[:5])
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical terminology assistant. "
                    "Given a patient query, identify the ONE main medical condition. "
                    "Return ONLY a JSON object:\n"
                    '{"condition": "diabetes", "synonyms": ["blood sugar", "type 2"]}\n\n'
                    "Rules:\n"
                    "- condition: canonical medical name (1-3 words, lowercase)\n"
                    "- synonyms: 3-6 patient-friendly ways to say the same thing\n"
                    '- If no clear condition: {"condition": "", "synonyms": []}\n'
                    "- Return ONLY the JSON"
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

        # Cache result
        synonyms_data = _load_condition_synonyms()
        if condition not in synonyms_data:
            synonyms_data[condition] = [s.lower() for s in synonyms if s.strip()]
        else:
            existing = set(synonyms_data[condition])
            existing.update(s.lower() for s in synonyms if s.strip())
            synonyms_data[condition] = sorted(existing)

        for candidate in candidates[:3]:
            if candidate not in synonyms_data.get(condition, []):
                synonyms_data.setdefault(condition, [])
                if candidate not in synonyms_data[condition]:
                    synonyms_data[condition].append(candidate)

        _save_condition_synonyms(synonyms_data)
        print(f"[*] condition_resolver: LLM identified '{condition}' -> cached")
        return condition

    except Exception as e:
        print(f"[!] condition_resolver: LLM fallback failed: {e}")
        return None


# #=============================================Previously working code 07/06/2026=========================================
# # """
# # condition_resolver.py — Standalone condition-to-drug mapping service.

# # Resolves patient condition queries to matching drug names from the formulary.

# # Two-pass design (built once at index time, zero LLM cost at query time):
# #     Pass 1: drug → illness terms         (drug_names.json)
# #     Pass 2: illness → synonyms           (condition_synonyms.json)

# # Query flow:
# #     "blood pressure medication"
# #         ↓
# #     extract_condition_terms() → ["blood", "pressure", "blood pressure"]  # unigrams + bigrams
# #         ↓
# #     find_canonical_condition() → "hypertension"  # synonym lookup
# #         ↓
# #     get_drugs_for_condition() → ["lisinopril", "amlodipine", "losartan"]
# #         ↓
# #     caller looks up formulary for each drug

# # Standalone — no dependencies on RAG pipeline, client.py, or category.py.
# # Can be imported by any component that needs condition→drug resolution.

# # LLM fallback:
# #     If a condition term isn't found in condition_synonyms.json,
# #     falls back to LLM to resolve it, then caches the result for next time.
# #     Over time the cache grows to cover more terms, reducing LLM calls.
# # """

# # import os
# # import re
# # import json
# # from datetime import datetime, timedelta

# # # ── File paths ────────────────────────────────────────────────────────────────
# # _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# # DRUG_NAMES_FILE = os.path.join(_BASE_DIR, "indices", "drug_names.json")
# # CONDITION_SYNONYMS_FILE = os.path.join(_BASE_DIR, "indices", "condition_synonyms.json")

# # # ── In-memory cache with 48-hour TTL ─────────────────────────────────────────
# # _drug_names_data: dict[str, list] = {}  # drug_word → [illness terms]
# # _condition_synonyms_data: dict[str, list] = {}  # condition → [synonyms]
# # _drug_names_loaded_at: datetime | None = None
# # _condition_synonyms_loaded_at: datetime | None = None
# # _CACHE_TTL = timedelta(hours=48)

# # # ── Stopwords — ignored when extracting condition terms from query ─────────────
# # _QUERY_STOPWORDS = {
# #     "i",
# #     "my",
# #     "me",
# #     "we",
# #     "our",
# #     "the",
# #     "a",
# #     "an",
# #     "is",
# #     "are",
# #     "was",
# #     "does",
# #     "do",
# #     "did",
# #     "will",
# #     "can",
# #     "could",
# #     "would",
# #     "should",
# #     "have",
# #     "has",
# #     "had",
# #     "what",
# #     "which",
# #     "who",
# #     "how",
# #     "when",
# #     "where",
# #     "why",
# #     "want",
# #     "need",
# #     "know",
# #     "about",
# #     "show",
# #     "tell",
# #     "give",
# #     "get",
# #     "find",
# #     "for",
# #     "in",
# #     "on",
# #     "at",
# #     "to",
# #     "of",
# #     "and",
# #     "or",
# #     "but",
# #     "with",
# #     "plan",
# #     "covered",
# #     "cover",
# #     "covers",
# #     "coverage",
# #     "benefit",
# #     "benefits",
# #     "drug",
# #     "drugs",
# #     "medication",
# #     "medications",
# #     "medicine",
# #     "medicines",
# #     "meds",
# #     "pill",
# #     "pills",
# #     "prescription",
# #     "formulary",
# #     "tier",
# #     "cost",
# #     "pay",
# #     "copay",
# #     "coinsurance",
# #     "deductible",
# #     "any",
# #     "all",
# #     "some",
# #     "more",
# #     "much",
# #     "many",
# #     "few",
# #     "please",
# #     "help",
# #     "information",
# #     "info",
# # }


# # # ── Data loading ──────────────────────────────────────────────────────────────


# # def _load_drug_names() -> dict[str, list]:
# #     """
# #     Loads drug_names.json into memory with 48-hour TTL cache.
# #     Structure: {"metformin": ["diabetes", "blood sugar"], ...}
# #     """
# #     global _drug_names_data, _drug_names_loaded_at

# #     now = datetime.utcnow()
# #     if _drug_names_loaded_at is not None and (now - _drug_names_loaded_at) < _CACHE_TTL:
# #         return _drug_names_data

# #     try:
# #         if os.path.exists(DRUG_NAMES_FILE):
# #             with open(DRUG_NAMES_FILE, encoding="utf-8") as f:
# #                 data = json.load(f)
# #             if isinstance(data, list):
# #                 data = {word: [] for word in data}
# #         else:
# #             data = {}
# #             print(
# #                 f"[!] condition_resolver: drug_names.json not found at {DRUG_NAMES_FILE}"
# #             )
# #     except Exception as e:
# #         print(f"[!] condition_resolver: failed to load drug_names.json: {e}")
# #         data = _drug_names_data or {}

# #     _drug_names_data = data
# #     _drug_names_loaded_at = now
# #     return _drug_names_data


# # def _load_condition_synonyms() -> dict[str, list]:
# #     """
# #     Loads condition_synonyms.json into memory with 48-hour TTL cache.
# #     Structure: {"diabetes": ["blood sugar", "type 2", "high blood sugar"], ...}
# #     """
# #     global _condition_synonyms_data, _condition_synonyms_loaded_at

# #     now = datetime.utcnow()
# #     if (
# #         _condition_synonyms_loaded_at is not None
# #         and (now - _condition_synonyms_loaded_at) < _CACHE_TTL
# #     ):
# #         return _condition_synonyms_data

# #     try:
# #         if os.path.exists(CONDITION_SYNONYMS_FILE):
# #             with open(CONDITION_SYNONYMS_FILE, encoding="utf-8") as f:
# #                 data = json.load(f)
# #         else:
# #             data = {}
# #             print(
# #                 f"[!] condition_resolver: condition_synonyms.json not found — "
# #                 f"run rx_indexer with --synonyms to generate it"
# #             )
# #     except Exception as e:
# #         print(f"[!] condition_resolver: failed to load condition_synonyms.json: {e}")
# #         data = _condition_synonyms_data or {}

# #     _condition_synonyms_data = data
# #     _condition_synonyms_loaded_at = now
# #     return _condition_synonyms_data


# # def _save_condition_synonyms(data: dict) -> None:
# #     """Saves condition_synonyms.json — called when LLM fallback adds new entries."""
# #     global _condition_synonyms_data, _condition_synonyms_loaded_at
# #     try:
# #         os.makedirs(os.path.dirname(CONDITION_SYNONYMS_FILE), exist_ok=True)
# #         with open(CONDITION_SYNONYMS_FILE, "w", encoding="utf-8") as f:
# #             json.dump(data, f, indent=2, sort_keys=True)
# #         _condition_synonyms_data = data
# #         _condition_synonyms_loaded_at = datetime.utcnow()
# #     except Exception as e:
# #         print(f"[!] condition_resolver: failed to save condition_synonyms.json: {e}")


# # # ── Term extraction ───────────────────────────────────────────────────────────


# # def extract_condition_terms(query: str) -> list[str]:
# #     """
# #     Extracts candidate condition terms from a query using unigram + bigram + trigram.

# #     Filters out stopwords and very short words. Returns candidates longest-first
# #     so phrase matches are preferred over single-word matches.

# #     Example:
# #         "I want to know about my blood pressure medication"
# #         → ["blood pressure", "blood", "pressure"]
# #           (trigrams filtered as too noisy; bigrams + unigrams returned)
# #     """
# #     # Normalize
# #     query_clean = re.sub(r"[^\w\s]", " ", query.lower()).strip()
# #     words = [
# #         w for w in query_clean.split() if w not in _QUERY_STOPWORDS and len(w) >= 4
# #     ]

# #     candidates = []

# #     # Trigrams
# #     for i in range(len(words) - 2):
# #         candidates.append(f"{words[i]} {words[i+1]} {words[i+2]}")

# #     # Bigrams
# #     for i in range(len(words) - 1):
# #         candidates.append(f"{words[i]} {words[i+1]}")

# #     # Unigrams
# #     candidates.extend(words)

# #     # Deduplicate preserving order (longest first due to tri→bi→uni ordering)
# #     seen = set()
# #     result = []
# #     for c in candidates:
# #         if c not in seen:
# #             seen.add(c)
# #             result.append(c)

# #     return result


# # # ── Core resolution ───────────────────────────────────────────────────────────


# # def find_canonical_condition(term: str) -> str | None:
# #     """
# #     Given a patient term (e.g. "blood pressure"), returns the canonical
# #     condition name (e.g. "hypertension") by scanning condition_synonyms.json.

# #     Checks both the canonical keys AND their synonym lists.

# #     Returns None if the term is not found in any synonym list.
# #     """
# #     synonyms_data = _load_condition_synonyms()
# #     term_lower = term.lower().strip()

# #     # Direct key match (term IS a canonical condition)
# #     if term_lower in synonyms_data:
# #         return term_lower

# #     # Scan synonym lists
# #     for canonical, synonyms in synonyms_data.items():
# #         if term_lower in [s.lower() for s in synonyms]:
# #             return canonical

# #     return None


# # def get_drugs_for_condition(condition: str) -> list[str]:
# #     """
# #     Returns all drug words from drug_names.json whose illness terms
# #     include the given condition or any of its synonyms.

# #     Example:
# #         get_drugs_for_condition("hypertension")
# #         → ["lisinopril", "amlodipine", "losartan", "metoprolol"]

# #         get_drugs_for_condition("blood pressure")  # synonym of hypertension
# #         → same list
# #     """
# #     drug_data = _load_drug_names()
# #     synonyms_data = _load_condition_synonyms()

# #     condition_lower = condition.lower().strip()

# #     # Build the full set of terms to match against:
# #     # the condition itself + all its synonyms
# #     match_terms = {condition_lower}
# #     if condition_lower in synonyms_data:
# #         match_terms.update(s.lower() for s in synonyms_data[condition_lower])
# #     else:
# #         # Check if it's a synonym of something
# #         for canonical, synonyms in synonyms_data.items():
# #             if condition_lower in [s.lower() for s in synonyms]:
# #                 match_terms.add(canonical)
# #                 match_terms.update(s.lower() for s in synonyms)
# #                 break

# #     # Find all drugs whose illness terms intersect with match_terms
# #     matching_drugs = []
# #     for drug_word, illness_terms in drug_data.items():
# #         drug_illness_lower = [t.lower() for t in illness_terms]
# #         if any(term in drug_illness_lower for term in match_terms):
# #             matching_drugs.append(drug_word)

# #     return sorted(matching_drugs)


# # def get_conditions_for_drug(drug: str) -> list[str]:
# #     """
# #     Returns the illness terms for a given drug word.

# #     Example:
# #         get_conditions_for_drug("metformin")
# #         → ["diabetes", "blood sugar", "high blood sugar"]
# #     """
# #     drug_data = _load_drug_names()
# #     return drug_data.get(drug.lower(), [])


# # def expand_condition(term: str) -> list[str]:
# #     """
# #     Returns all synonyms for a given condition term, including the term itself.

# #     Example:
# #         expand_condition("hypertension")
# #         → ["hypertension", "blood pressure", "high blood pressure", "high bp", "bp"]

# #         expand_condition("blood pressure")  # synonym → expands to same set
# #         → ["hypertension", "blood pressure", "high blood pressure", "high bp", "bp"]
# #     """
# #     synonyms_data = _load_condition_synonyms()
# #     term_lower = term.lower().strip()

# #     # Direct match
# #     if term_lower in synonyms_data:
# #         return [term_lower] + synonyms_data[term_lower]

# #     # Reverse lookup — term is a synonym
# #     for canonical, synonyms in synonyms_data.items():
# #         if term_lower in [s.lower() for s in synonyms]:
# #             return [canonical] + synonyms

# #     return [term_lower]  # unknown term — return as-is


# # def resolve_query_to_drugs(query: str, use_llm_fallback: bool = True) -> list[str]:
# #     """
# #     Main entry point — resolves a free-text query to matching drug words.

# #     Steps:
# #         1. Extract condition terms (unigrams + bigrams + trigrams)
# #         2. For each candidate term, look up condition_synonyms.json
# #         3. If found → get all drugs for that condition
# #         4. If not found and use_llm_fallback=True → call LLM, cache result

# #     Returns deduplicated list of drug words, or [] if no match found.

# #     Example:
# #         resolve_query_to_drugs("I want to know about my blood pressure medication")
# #         → ["amlodipine", "lisinopril", "losartan", "metoprolol"]
# #     """
# #     candidates = extract_condition_terms(query)

# #     if not candidates:
# #         return []

# #     # Try each candidate — return on first match (longest match wins due to ordering)
# #     for term in candidates:
# #         canonical = find_canonical_condition(term)
# #         if canonical:
# #             drugs = get_drugs_for_condition(canonical)
# #             if drugs:
# #                 print(
# #                     f"[*] condition_resolver: '{term}' → '{canonical}' → {len(drugs)} drugs"
# #                 )
# #                 return drugs

# #     # LLM fallback — ask LLM what condition the query is about
# #     if use_llm_fallback:
# #         condition = _resolve_condition_via_llm(query, candidates)
# #         if condition:
# #             drugs = get_drugs_for_condition(condition)
# #             if drugs:
# #                 print(
# #                     f"[*] condition_resolver: LLM resolved to '{condition}' → {len(drugs)} drugs"
# #                 )
# #                 return drugs

# #     print(f"[*] condition_resolver: no condition match found for query: '{query}'")
# #     return []


# # # ── LLM fallback ─────────────────────────────────────────────────────────────


# # def _resolve_condition_via_llm(query: str, candidates: list[str]) -> str | None:
# #     """
# #     Calls LLM to identify the medical condition in the query.
# #     Caches the result in condition_synonyms.json for future use.

# #     Only called when condition_synonyms.json lookup fails.
# #     """
# #     try:
# #         from utility.llm import llm_chat

# #         candidate_str = ", ".join(candidates[:5])  # top 5 candidates
# #         messages = [
# #             {
# #                 "role": "system",
# #                 "content": (
# #                     "You are a medical terminology assistant. "
# #                     "Given a patient query, identify the ONE main medical condition being asked about. "
# #                     "Return ONLY a JSON object in this exact format:\n"
# #                     '{"condition": "diabetes", "synonyms": ["blood sugar", "type 2", "high blood sugar"]}\n\n'
# #                     "Rules:\n"
# #                     "- condition: the canonical medical name (1-3 words, lowercase)\n"
# #                     "- synonyms: 3-6 patient-friendly ways to say the same thing\n"
# #                     '- If no clear medical condition, return: {"condition": "", "synonyms": []}\n'
# #                     "- Return ONLY the JSON, nothing else"
# #                 ),
# #             },
# #             {
# #                 "role": "user",
# #                 "content": f"Query: {query}\nCandidate terms: {candidate_str}",
# #             },
# #         ]

# #         response = llm_chat(messages=messages, format="json", max_tokens=100)
# #         if not response:
# #             return None

# #         data = json.loads(response.strip())
# #         condition = data.get("condition", "").strip().lower()
# #         synonyms = data.get("synonyms", [])

# #         if not condition:
# #             return None

# #         # Cache in condition_synonyms.json
# #         synonyms_data = _load_condition_synonyms()
# #         if condition not in synonyms_data:
# #             synonyms_data[condition] = [s.lower() for s in synonyms if s.strip()]
# #         else:
# #             # Merge new synonyms with existing
# #             existing = set(synonyms_data[condition])
# #             existing.update(s.lower() for s in synonyms if s.strip())
# #             synonyms_data[condition] = sorted(existing)

# #         # Also add reverse mappings for each candidate that led here
# #         for candidate in candidates[:3]:
# #             if candidate not in synonyms_data.get(condition, []):
# #                 synonyms_data.setdefault(condition, [])
# #                 if candidate not in synonyms_data[condition]:
# #                     synonyms_data[condition].append(candidate)

# #         _save_condition_synonyms(synonyms_data)
# #         print(
# #             f"[*] condition_resolver: LLM identified '{condition}' → cached in condition_synonyms.json"
# #         )
# #         return condition

# #     except Exception as e:
# #         print(f"[!] condition_resolver: LLM fallback failed: {e}")
# #         return None
