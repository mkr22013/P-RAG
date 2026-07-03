# BENJI — Illness Classification Design

Reference document — drug-to-illness mapping feature design, prompt specification, and query-time flow.

> Read this before implementing illness classification. The one-time LLM classification pass has a real token cost — see the cost warning below. The query-time wiring has zero token cost once classification is complete.

---

## Why This Feature Exists

Members often don't know the exact drug name — they know the condition it treats:

```
"is my diabetes medicine covered?"
"what cholesterol drugs do you cover?"
"are there any cancer medications on my plan?"
```

Without illness mapping, BENJI can only answer these queries via LLM fallback (expensive, slow) or not at all. With illness mapping, these become zero-token structured lookups against the formulary index — same speed and accuracy as any direct drug-name query.

---

## Architecture — Two Completely Separate Phases

### Phase 1: Classification (Index Time — One Time Only)

Runs during `rx_indexer.py` when `classify_illness=True`. Makes one mini-LLM call per NEW drug word never seen before, writes results to `drug_names.json`.

**This phase has a real, bounded token cost — see cost warning below.**

### Phase 2: Query-Time Lookup (Zero Tokens)

Reads pre-classified illness terms from `drug_names.json` at query time. No LLM call needed — pure dictionary lookup, same speed as any other rule-based path in BENJI.

---

## Phase 1 — The Classification Prompt

### Design Constraints

The prompt must enforce:

1. **Layman terms only** — members say "diabetes" not "type 2 diabetes mellitus"
2. **Short, searchable phrases** — 1-3 words max per term, matching what a member would actually type
3. **Medically specific** — "fungal infection" not just "infection" (too generic)
4. **Bounded count** — 1-3 terms max per drug, not an exhaustive medical reference
5. **Structured output only** — comma-separated list, no explanation, no preamble

### Prompt Specification

```python
SYSTEM_PROMPT = """You are a medical terminology assistant.
Given a drug name, return 1-3 common layman conditions or illnesses
it is used to treat. Rules:
- Use everyday patient language, NOT medical jargon
  (e.g. "diabetes" not "type 2 diabetes mellitus")
- Maximum 3 words per condition term
- Return ONLY a comma-separated list, nothing else
- If the drug treats multiple unrelated conditions, pick the most common
- If unsure, return empty string

Examples:
metformin → diabetes, blood sugar
atorvastatin → high cholesterol, cholesterol
fluconazole → fungal infection, yeast infection
amoxicillin → bacterial infection
lisinopril → high blood pressure, heart failure
BEYFORTUS → rsv prevention, preventive"""

USER_PROMPT = f"Drug: {drug_word}"
```

### Illness Term Stoplist (Post-LLM Filtering)

Even with a precise prompt, LLM can return generic connector words. Filter these out after receiving the response:

```python
_ILLNESS_TERM_STOPLIST = {
    "type", "disease", "disorder", "condition", "syndrome",
    "chronic", "acute", "related", "associated", "induced",
    "treatment", "therapy", "management", "prevention",
}

def clean_illness_terms(terms: list) -> list:
    result = []
    for term in terms:
        words = term.lower().strip().split()
        filtered_words = [w for w in words if w not in _ILLNESS_TERM_STOPLIST]
        if filtered_words and len(' '.join(filtered_words)) > 2:
            result.append(' '.join(filtered_words))
    return result[:3]  # hard cap at 3 terms per drug
```

---

## ⚠️ Cost Warning

```
~5,000-6,000 unique drug words × ~40 tokens/call ≈ 200,000-250,000 tokens TOTAL
This is a ONE-TIME cost — re-indexing the same booklet costs 0 additional tokens
since already-classified words are never re-classified.
```

**To run the classification pass:**

1. Change in `rx_indexer.py`:

```python
# From:
update_drug_names_file(chunks, classify_illness=False)

# To:
update_drug_names_file(chunks, classify_illness=True)
```

2. Re-run the Rx indexer for BOTH E4 and A2:

```bash
python -m indexers.rx_indexer docs/2026/rx/052149_2026.pdf indices/2026_rx_..._e4.json
python -m indexers.rx_indexer docs/2026/rx/Rx.pdf indices/2026_rx_..._a2.json
```

3. Run full golden test verify — should be **100% identical** to pre-classification baseline since no query-time code changed yet.

4. Only THEN wire illness terms into `client.py` (Phase 2).

---

## Phase 2 — Query-Time Flow

### Medicine Signal Word Guard

MUST be present before condition-based routing fires. Prevents "I have diabetes" (no medicine context) from accidentally routing to Rx.

```python
MEDICINE_SIGNAL_WORDS = {
    "drug", "drugs", "medicine", "medicines",
    "medication", "medications", "pill", "pills",
    "tablet", "tablets", "capsule", "capsules",
    "prescription", "prescriptions",
}
```

### Full Query Flow

```
Member: "is my diabetes medicine covered?"
    ↓
1. medicine-signal-word check
   ("medicine" present) ✓
    ↓
2. Extract condition term from query
   query_words filtered through _PURE_NOISE_WORDS
   → remaining concept words: ["diabetes"]
    ↓
3. find_drug_words_for_illness("diabetes")
   → scans drug_names.json values for "diabetes"
   → returns: ["metformin", "glipizide", "sitagliptin", "ozempic", ...]
    ↓
4. Pass drug words as search terms to tools.py
   (exact same scoring path as any drug-name query)
    ↓
5. Filter results by what's on THIS member's formulary
   (same is_drug_match() logic already in place)
    ↓
6. build_rx_response() with real tier/cost data
   → 0 additional LLM calls at query time
```

### Helper Functions (Already Built in category.py)

```python
# Returns illness terms for a specific drug word
get_illness_terms_for_word("metformin") → ["diabetes", "blood sugar"]

# Returns all drug words that treat a given condition
find_drug_words_for_illness("diabetes") → ["metformin", "glipizide", ...]
```

These are already implemented and tested — just need wiring into `client.py`.

---

## drug_names.json Structure

```json
{
  "metformin": ["diabetes", "blood sugar"],
  "atorvastatin": ["high cholesterol", "cholesterol"],
  "fluconazole": ["fungal infection", "yeast infection"],
  "humira": ["arthritis", "crohns", "psoriasis"],
  "beyfortus": ["rsv prevention", "preventive"],
  "ozempic": ["diabetes", "weight loss"]
}
```

**Keys:** drug name words (plan-agnostic, cross-booklet, deduplicated at write time)
**Values:** layman illness/condition terms (empty list `[]` until `classify_illness=True` is run)

---

## Implementation Checklist

- [ ] Change `classify_illness=False` → `classify_illness=True` in `rx_indexer.py`
- [ ] Re-run Rx indexer for E4 and A2 (accept the one-time token cost)
- [ ] Confirm `drug_names.json` values are populated with real illness terms
- [ ] Run full verify — confirm 124/124 passing (zero regression expected)
- [ ] Add `MEDICINE_SIGNAL_WORDS` check to `client.py` Rx path
- [ ] Wire `find_drug_words_for_illness()` into condition-based query routing
- [ ] Add test queries to `golden_test.py`:
  - `"is my diabetes medicine covered?"`
  - `"what cholesterol drugs do you cover?"`
  - `"are there any cancer medications on my plan?"`
- [ ] Capture new Rx baseline
- [ ] Full verify — confirm no regression on existing 123 queries
- [ ] Check in

---

## Why This Won't Cause Regression

**Phase 1 (classification pass):**

- Only changes VALUES in `drug_names.json` (was empty lists, now has terms)
- KEYS unchanged — `_load_drug_name_words()` still returns the same set
- No query-time code changes whatsoever
- Existing golden tests unaffected (none trigger illness-term lookup yet)

**Phase 2 (wiring into client.py):**

- Only fires when BOTH medicine-signal-word AND recognized illness term present
- Falls back to existing behavior (LLM or direct drug lookup) for all other queries
- New test queries added to golden suite before capture — any regression caught immediately

**Category isolation:**

- `drug_names.json` is only read by the Rx path
- Medical, dental, vision completely unaffected regardless of what changes in this file
