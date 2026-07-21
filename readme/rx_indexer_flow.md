# BENJI — Rx Indexer Flow

Reference document — garbage-page filtering, drug name word list, three-tier illness classification, and condition-based query routing.

> Read this before re-running the Rx indexer. The illness-classification step has a real one-time LLM cost — see the cost warning below before running anything.

---

## What This Document Covers

1. Rx indexer pipeline (`rx_indexer.py`)
2. Drug intelligence layer (`build_rxclass_lookup.py`)
3. Drug word file (`drug_words.json`) — structure and loading
4. Condition resolver (`condition_resolver.py`) — how illness queries route to drugs
5. Cost warnings and pre-flight checklist

---

## Key Files

| File | Purpose |
|---|---|
| `indexers/rx_indexer.py` | Parses Rx formulary PDF → structured JSON index |
| `build_rxclass_lookup.py` | Three-tier illness classification — MED-RT → RxClass → LLM |
| `utility/condition_resolver.py` | Maps member illness queries to drug names |
| `indices/drug_words.json` | Master drug word list with illness mappings and tier tags |
| `indices/condition_synonyms.json` | Condition name synonyms for query matching |
| `utility/category.py` | Loads `drug_words.json` for category detection and drug matching |

---

## Rx Index File Structure

Each drug entry in the index JSON:

```json
{
  "drug_name": "metformin oral tablet 500 mg",
  "tier": "1",
  "tier_label": "Preferred Generic",
  "requirements": "PA",
  "requirements_text": "Prior Authorization required...",
  "status": "Covered",
  "page_number": 42
}
```

---

## drug_words.json Structure

> **Note:** The filename changed from `drug_names.json` (previous doc) to `drug_words.json`.

Each entry maps a drug word (first word of drug name, lowercase) to its metadata:

```json
{
  "metformin": {
    "entry_type": "drug",
    "illnesses": ["Diabetes Mellitus Type 2", "Hyperglycemia"],
    "illness_source": "med-rt"
  },
  "ozempic": {
    "entry_type": "drug",
    "illnesses": ["Diabetes Mellitus Type 2", "Obesity"],
    "illness_source": "rxclass"
  },
  "humira": {
    "entry_type": "drug",
    "illnesses": ["Arthritis, Rheumatoid", "Crohn Disease", "Psoriasis"],
    "illness_source": "llm"
  },
  "insulin": {
    "entry_type": "drug",
    "illnesses": [],
    "illness_source": "unclassified"
  }
}
```

### `illness_source` Values

| Value | Source | Accuracy |
|---|---|---|
| `med-rt` | NLM MED-RT XML + RXNREL.RRF (authoritative) | ~100% |
| `rxclass` | NLM RxClass API, may_treat only | ~100% |
| `llm` | LLM fallback (tagged for review) | ~90-95% |
| `unclassified` | No illness mapping found | N/A — direct drug query only |

### Current Stats (July 2026)

```
Total words:        2,645
Drug entries:       2,480
Tier 1 (MED-RT):   2,194 drugs
Tier 2 (RxClass):     13 drugs
Tier 3 (LLM):        187 drugs
Unclassified:        103 drugs (direct query only, no illness mapping)
Non-drug words:      165 (devices, vaccines, filtered out of illness routing)
```

---

## Full Indexer Flow

### Run Command

```bash
python -m indexers.rx_indexer <pdf_path> <output.json>
```

### Step 1 — `classify_document()`
- Reads page 1 of the PDF
- Detects E4 vs A2 variant, effective year
- No LLM call — pure text parsing

### Step 2 — `generate_sub_index(output_path, pdf_path)`

```
find_drug_list_start()     ← finds where drug entries begin
find_drug_list_end()       ← finds where alphabetical index begins, stops there
parse_intro_pages()        ← builds INFO chunks from intro pages
Main parsing loop          ← builds drug chunks ONLY between start/end boundaries
Writes output_path         ← full booklet index JSON
update_drug_names_file()   ← Step 2b below
```

#### `find_drug_list_end()` — Garbage Page Fix

The alphabetical index pages at the end of Rx formulary PDFs were being indexed as real drug entries. This caused two bugs:

1. **Query bug** — "does humira need prior authorization?" returned garbage rows mixed with real Humira results, because index pages list drug names alphabetically alongside page numbers
2. **Category detection bug** — `_load_drug_name_words()` read drug names from garbage chunks, polluting the drug word set

Fix: detect page literally starting with "Index", or high density of long lines packed with page-number references → stop processing before that page.

#### Step 2b — `update_drug_names_file()`
- Extracts all drug name words from this run's chunks
- Loads existing `drug_words.json`
- Computes `truly_new_words = new_words - already_known_words`
- If empty → prints "unchanged", **0 LLM calls**
- If new words exist → classifies via three-tier system (see below)
- Writes updated `drug_words.json`

### Step 3 — `post_index_upload()`
- Uploads `output_path` to Blob storage
- No-op locally when `AZURE_BLOB_CONNECTION_STRING` is not set

### Step 3b — `drug_words.json` Blob Sync (Rx only)
- Uploads updated `drug_words.json` to Blob
- Other server instances pick up new words on next 48h TTL refresh
- No-op locally

```
RUN: python -m indexers.rx_indexer <pdf_path> <output.json>
  │
  ├─ Step 1: classify_document()
  │
  ├─ Step 2: generate_sub_index(output_path, pdf_path)
  │   ├─ find_drug_list_start()
  │   ├─ find_drug_list_end()         ← garbage page detection
  │   ├─ parse_intro_pages()
  │   ├─ Main parsing loop
  │   ├─ Writes output_path
  │   └─ update_drug_names_file()     ← Step 2b
  │
  ├─ Step 3: post_index_upload()
  │
  └─ Step 3b: drug_words.json Blob sync (Rx only)
```

---

## Three-Tier Drug Intelligence Layer

> **This is a separate one-time build step, NOT part of the Rx indexer run.**
> Run `build_rxclass_lookup.py` to classify illness terms for all drug words.

### Why Three Tiers

Single-source classification misses too many drugs. The three tiers maximize coverage:

| Tier | Source | Why |
|---|---|---|
| 1 | MED-RT XML (NLM) + RXNREL.RRF | Most authoritative, covers ~88% of drugs |
| 2 | RxClass API (NLM, may_treat only) | Catches drugs MED-RT misses |
| 3 | LLM fallback | Catches remaining drugs with ~90-95% accuracy |

### Build Command

```bash
python build_rxclass_lookup.py [flags]
```

| Flag | Effect |
|---|---|
| *(no flags)* | Full run — all three tiers |
| `--no-rxclass` | Skip Tier 2 (RxClass API) |
| `--no-llm` | Skip Tier 3 (LLM fallback) |
| `--force` | Re-classify already-classified drugs |
| `--clear` | Wipe all illness data, start fresh |

### Build Flow

```
Step 1: Load drug_words.json
Step 2: MED-RT XML + RXNREL.RRF lookup
        → for each drug, find illnesses via RXNORM relationship graph
        → tag illness_source = "med-rt"
Step 3: RxClass API lookup (may_treat relationship only)
        → for drugs still unclassified after Step 2
        → tag illness_source = "rxclass"
Step 4: LLM fallback
        → for drugs still unclassified after Steps 2-3
        → one mini-LLM call per drug
        → tag illness_source = "llm"
Step 5: Write updated drug_words.json
```

### ⚠️ Cost Warning

| Scenario | LLM Cost |
|---|---|
| First run, no existing classifications | Real cost — one call per unclassified drug |
| Re-run after existing drug_words.json | Only truly new/unclassified drugs → minimal |
| `--no-llm` flag | Zero LLM cost |
| Tier 1+2 cover the drug | Zero LLM cost (LLM only for Tier 3 fallback) |

---

## Condition Resolver — How Illness Queries Route to Drugs

> **Status:** Fully operational and wired into `client.py` via `_handle_rx_query()`.
> Previous doc stated "not yet wired in" — that is now outdated.

### What It Does

Maps member illness queries to matching drug names from `drug_words.json`:

```
"drugs for diabetes"          → ["metformin", "ozempic", "glipizide", ...] (67 drugs)
"what is covered for asthma?" → ["albuterol", "fluticasone", "montelukast", ...]
"medication for blood clots"  → ["warfarin", "eliquis", "xarelto", ...] (13 drugs)
"what is covered for sleep apnea?" → ["modafinil", "anoro", "trelegy", "provigil"]
```

---

### Step 1 — `extract_condition_terms(query)`

Extracts candidate condition terms using trigram → bigram → unigram approach.
Returns candidates longest-first so phrase matches beat single-word matches.

```python
query = "what is covered for high blood pressure?"
stopwords removed → ["covered", "high", "blood", "pressure"]

trigrams: ["covered high blood", "high blood pressure"]
bigrams:  ["covered high", "high blood", "blood pressure"]
unigrams: ["covered", "high", "blood", "pressure"]

# Result (deduped, order preserved):
["covered high blood", "high blood pressure", "covered high",
 "high blood", "blood pressure", "covered", "high", "blood", "pressure"]
```

Longer phrases are tried first — `"high blood pressure"` matches before
`"blood"` does.

---

### Step 2 — `find_canonical_condition(term)`

Maps a plain-language term to a canonical condition name using a 4-stage
matching process:

#### Stage 1 — Priority Map (`_PRIORITY_CONDITION_MAP`)

Hardcoded map for the most common conditions where ambiguity exists:

```python
_PRIORITY_CONDITION_MAP = {
    "diabetes":           "Diabetes Mellitus Type 2",
    "high blood pressure":"Hypertension",
    "blood pressure":     "Hypertension",
    "migraine":           "Migraine Disorders",
    "cholesterol":        "Hypercholesterolemia",
    "high cholesterol":   "Hypercholesterolemia",
    "depression":         "Depressive Disorder",
    "anxiety":            "Anxiety Disorders",
    "asthma":             "Asthma",
    "blood clot":         "Thromboembolism",
    "blood clots":        "Thromboembolism",
    "seizures":           "Epilepsy",
    "epilepsy":           "Epilepsy",
    "ms":                 "Multiple Sclerosis",
    "flu":                "Influenza",
    "shingles":           "Herpes Zoster",
    # ... 30+ total entries
}
```

Why needed: without this, `"diabetes"` might match multiple conditions.
Priority map ensures the most clinically relevant condition wins.

#### Stage 2 — Direct Key Match in `condition_synonyms.json`

```python
# Exact match against canonical condition names (keys)
if term_lower in synonyms_data:
    return term_lower
```

#### Stage 3 — Synonym Match (Most Specific Wins)

Searches all synonym lists in `condition_synonyms.json`.
Collects ALL matches, then scores by specificity:

```python
def match_score(item):
    canonical, idx = item
    canonical_words = set(canonical.lower().replace(",", "").split())
    words_in_common = len(term_words & canonical_words)
    return (words_in_common, -idx, len(canonical))
    # Higher words_in_common = more specific match
    # Lower synonym index = more primary synonym
    # Longer canonical = more specific condition
```

**The specificity bug fix (July 2026):**
`"sleep apnea"` matched BOTH `"Apnea"` (has "sleep apnea" as synonym)
AND `"Sleep Apnea, Obstructive"` (direct key). Old code returned the first
match found (dict order) = `"Apnea"` → wrong drugs (migraine/respiratory).
New code scores by words-in-common:
- `"apnea"` canonical: 1 word in common with "sleep apnea"
- `"sleep apnea, obstructive"` canonical: 2 words in common → WINS

#### Stage 4 — Partial Substring Match (≥6 char terms)

```python
# "hypertens" matches "Hypertension" — partial match fallback
if len(term_lower) >= 6:
    if term_lower in canonical.lower():
        partial_matches.append((canonical, words_in_common, len(canonical)))
# Picks most specific partial match
```

---

### Step 3 — `get_drugs_for_condition(condition)`

Finds all drugs treating the condition by:

1. Building a `match_terms` set = condition name + ALL its synonyms
2. Scanning `drug_words.json` illnesses[] for any intersection with match_terms
3. Filtering out devices, vitamins, vaccines (entry_type check)

```python
# Example for "Hypertension":
match_terms = {"hypertension", "high blood pressure", "elevated blood pressure", "hbp", ...}

# drug_words.json scan:
"lisinopril": illnesses=["Hypertension", "Heart Failure"] → MATCH ✅
"amlodipine": illnesses=["Hypertension", "Angina"]       → MATCH ✅
"metformin":  illnesses=["Diabetes Mellitus Type 2"]      → no match
```

---

### Step 4 — `resolve_query_to_drugs(query)` — Full Resolution

```
query → extract_condition_terms()
     → for each term (longest first):
         find_canonical_condition(term)
              → Priority Map       (Stage 1)
              → Direct Key Match   (Stage 2)
              → Synonym Match      (Stage 3, most specific wins)
              → Partial Match      (Stage 4)
         if canonical found:
             get_drugs_for_condition(canonical)
             if drugs found → return immediately
     → if still nothing and use_llm_fallback=True:
         LLM identifies condition from query
         get_drugs_for_condition(llm_condition)
         return drugs
     → return []
```

First successful match wins — no merging across multiple conditions.
This prevents "high blood pressure" returning both hypertension drugs
AND blood clot drugs by accidentally matching "blood" to both.

---

## condition_synonyms.json Structure

Maps canonical condition names (keys) to plain-language synonyms members
might use. Used bidirectionally — query terms look up synonyms, and drug
illness terms look up canonical names.

```json
{
  "Hypertension": [
    "high blood pressure", "elevated blood pressure", "hbp", "blood pressure"
  ],
  "Diabetes Mellitus Type 2": [
    "diabetes", "type 2 diabetes", "t2d", "high blood sugar", "blood sugar"
  ],
  "Sleep Apnea, Obstructive": [
    "sleep apnoea", "obstructive sleep disorder", "osa", "snoring",
    "breathing problems at night", "sleep stoppages"
  ],
  "Thromboembolism": [
    "blood clots", "blood clot", "deep vein thrombosis", "dvt",
    "pulmonary embolism", "pe", "clotting disorder"
  ],
  "Migraine Disorders": [
    "migraine", "migraines", "migraine headache", "chronic migraine",
    "hemiplegic migraine", "cluster headache"
  ]
  // ... 100+ conditions total
}
```

### How Synonyms Are Built

`condition_synonyms.json` was built once using NLM condition data and
extended manually. It is NOT auto-generated on each run. To add new synonyms:

1. Edit `indices/condition_synonyms.json` directly
2. No re-indexing needed — loaded at runtime with 48h TTL cache
3. Cache can be invalidated via `condition_resolver.invalidate_cache()`

---

## End-to-End Example: "medication for blood clots"

```
Query: "medication for blood clots"

Step 1 — extract_condition_terms():
  trigrams: ["medication for blood", "for blood clots"]
  bigrams:  ["medication for", "for blood", "blood clots"]
  unigrams: ["medication", "for", "blood", "clots"]

Step 2 — find_canonical_condition() for each term:
  "medication for blood" → None (not in synonyms)
  "for blood clots"      → None
  "medication for"       → None
  "for blood"            → None
  "blood clots"          → _PRIORITY_CONDITION_MAP hit → "Thromboembolism"

Step 3 — get_drugs_for_condition("Thromboembolism"):
  match_terms = {"thromboembolism", "blood clots", "dvt", "pulmonary embolism", ...}
  drug scan → 13 matching drugs: warfarin, eliquis, xarelto, pradaxa, ...

Step 4 — _handle_rx_query():
  condition_drugs = ["warfarin", "eliquis", "xarelto", ...] (13 drugs)
  len = 13 > 10? NO → full drug table (not name list)
  → rx formulary lookup for all 13 drugs
  → build_rx_response() → covered table + cost table
  → answer: "Here are the covered medications for Thromboembolism..."

Token cost: 0 (condition resolved by rules, no LLM needed)
```

---

## drug_words.json Loading (category.py)

```python
_load_drug_name_data()      # loads full dict with illness terms, 48h TTL
_load_drug_name_words()     # thin wrapper → returns set of word keys only
                            # used by: is_drug_name_query, correct_drug_spelling,
                            #          is_drug_match

get_illness_terms_for_word(word)    # returns illness list for a drug word
find_drug_words_for_illness(illness) # returns all drugs treating an illness
```

---

## Pre-Flight Checklist

- [ ] Replace `rx_indexer.py`, `category.py`, `main.py`, `run_indexer.py`
- [ ] First run: verify garbage pages skipped — check console for `"alphabetical index detected starting page X"`
- [ ] Run full golden test verify — confirm zero regression (151 queries)
- [ ] Re-test "does humira need prior authorization?" — should show ONLY real Humira entries
- [ ] Run `build_rxclass_lookup.py --no-llm` first to verify structure, zero LLM cost
- [ ] Once confirmed stable, run `build_rxclass_lookup.py` for full three-tier classification
- [ ] Re-capture Rx baseline, run full verify again
- [ ] Check in

---

## Where Files Live

```
Local:
  indices/drug_words.json          ← master drug word + illness list
  indices/condition_synonyms.json  ← condition name synonyms
  indices/2026_rx_*.json           ← Rx formulary index (per plan)

Production:
  Blob storage → synced to local path at server startup (48h TTL)
```