# BENJI — Knowledge Base Design

## Purpose

The knowledge base is a curated + LLM-grown synonym mapping that improves
chunk retrieval accuracy by bridging the gap between:

- **What members say** — "teeth cleaning", "tooth removal", "glasses"
- **What the index contains** — "prophylaxis", "extraction", "vision hardware"

Without synonyms, a member asking "how much is a teeth cleaning?" would not
match the "Prophylaxis" chunk because the word "cleaning" is not in the chunk
keywords. The knowledge base solves this by mapping plain-language terms to
their technical equivalents and vice versa.

---

## Problem It Solves

### Before Knowledge Base

```
Member query: "how much is a teeth cleaning?"
query_keywords: ["cleaning", "teeth"]

Chunk keywords: ["prophylaxis", "dental", "class-i", "deductible"]

Score: 0 keyword matches → wrong chunk returned ❌
```

### After Knowledge Base

```
Member query: "how much is a teeth cleaning?"
query_keywords: ["cleaning", "teeth"]

KB lookup at query time:
    "cleaning" → dental KB → canonical: "prophylaxis"
    expand query_keywords → ["cleaning", "teeth", "prophylaxis"]

Chunk keywords: ["prophylaxis", "dental", "class-i", "deductible"]

Score: "prophylaxis" matches → correct chunk returned ✅
```

---

## Two-Layer Protection

The knowledge base works at TWO points in the pipeline:

```
Layer 1 — Query time expansion (tools.py):
    query_keywords → KB lookup → add canonical terms
    → immediate benefit, no re-index needed
    → covers new KB entries as soon as knowledge_base.json is updated

Layer 2 — Index time enrichment (get_smart_keywords):
    chunk_keywords → KB lookup → inject synonyms into chunk
    → makes chunk findable even WITHOUT query expansion
    → takes effect after next re-index
    → provides redundancy
```

Both layers do a simple dict lookup — no LLM, no tokens, microseconds.

---

## File Structure

```
utility/
└── knowledge_base.json          ← single file, category-aware structure
```

Single file preferred over multiple files — one load, one cache, simpler
maintenance. Category separation is handled by nesting.

### Structure

```json
{
  "dental": {
    "prophylaxis": ["cleaning", "teeth cleaning", "polish", "tooth polish"],
    "extraction": ["tooth removal", "pulling tooth", "tooth pulled", "remove tooth"],
    "root canal": ["endodontic", "nerve treatment", "root treatment", "canal treatment"],
    "crown": ["cap", "tooth cap", "porcelain crown", "dental cap"],
    "panoramic": ["full mouth xray", "panoramic xray", "full mouth series", "panorex"],
    "bitewing": ["bitewing xray", "cavity xray", "interproximal xray"],
    "periapical": ["periapical xray", "individual tooth xray", "single tooth xray"],
    "orthodontic": ["braces", "aligners", "invisalign", "retainer", "teeth straightening"],
    "periodontic": ["gum treatment", "gum disease", "scaling root planing"],
    "implant": ["dental implant", "tooth implant", "implant surgery"],
    "denture": ["false teeth", "removable teeth", "partial denture", "full denture"],
    "sealant": ["tooth sealant", "fissure sealant", "dental sealant"],
    "fluoride": ["fluoride treatment", "fluoride varnish", "fluoride application"],
    "tmj": ["jaw pain", "jaw disorder", "jaw clicking", "temporomandibular"],
    "bridge": ["dental bridge", "tooth bridge", "pontic", "fixed bridge"],
    "apicoectomy": ["root tip surgery", "root end surgery", "root tip removal"],
    "anesthesia": ["sedation", "sleep dentistry", "dental sedation", "nitrous oxide"],
    "class i": ["diagnostic", "preventive", "cleaning xray exam"],
    "class ii": ["basic services", "fillings extractions root canals"],
    "class iii": ["major services", "crowns bridges dentures implants"]
  },
  "medical": {
    "rehabilitation": ["physical therapy", "rehab therapy", "PT", "occupational therapy"],
    "inpatient": ["hospital stay", "admitted to hospital", "hospitalization"],
    "outpatient": ["same day surgery", "ambulatory care", "day surgery"],
    "prophylaxis": ["preventive care", "wellness visit", "annual physical"],
    "coinsurance": ["cost sharing", "your share", "percentage you pay"],
    "deductible": ["amount you pay first", "before insurance pays"],
    "prior authorization": ["prior auth", "pre-approval", "pre-authorization", "PA"],
    "formulary": ["drug list", "covered drugs", "medication list"],
    "specialist": ["specialist doctor", "specialist visit", "specialist copay"],
    "emergency room": ["ER", "emergency department", "ED", "emergency care"],
    "urgent care": ["urgent care clinic", "walk in clinic", "immediate care"],
    "home health": ["home care", "visiting nurse", "home nursing"],
    "hospice": ["end of life care", "palliative care", "terminal care"],
    "dialysis": ["kidney treatment", "renal treatment", "hemodialysis"],
    "bariatric": ["weight loss surgery", "gastric bypass", "gastric sleeve"],
    "transplant": ["organ transplant", "kidney transplant", "liver transplant"],
    "chiropractic": ["chiropractor", "spinal adjustment", "back adjustment"],
    "acupuncture": ["acupuncturist", "needle therapy", "acupuncture treatment"],
    "telehealth": ["virtual visit", "video visit", "online doctor", "virtual care"],
    "durable medical equipment": ["DME", "wheelchair", "crutches", "medical equipment"]
  },
  "vision": {
    "vision hardware": ["glasses", "eyeglasses", "frames", "lenses", "contacts"],
    "vision exam": ["eye exam", "eye examination", "eye checkup", "vision test"],
    "contact lenses": ["contacts", "contact lens", "soft lenses", "hard lenses"],
    "frames": ["eyeglass frames", "glasses frames", "spectacle frames"],
    "progressive": ["progressive lenses", "no line bifocal", "varifocal"],
    "bifocal": ["bifocal lenses", "lined bifocal", "reading glasses"],
    "out of area": ["out of state", "outside washington", "travelling", "travel vision"],
    "optometrist": ["eye doctor", "vision doctor", "OD"],
    "ophthalmologist": ["eye surgeon", "eye specialist", "MD eye doctor"],
    "low vision": ["vision impairment", "vision loss", "legally blind"]
  },
  "shared": {
    "copay": ["co-pay", "fixed amount", "flat fee", "visit fee"],
    "deductible": ["amount before insurance", "your deductible", "annual deductible"],
    "out of pocket": ["OOP", "out-of-pocket maximum", "OOP max", "your maximum"],
    "covered": ["included", "eligible", "benefits available"],
    "not covered": ["excluded", "not eligible", "no benefit", "not included"],
    "in network": ["in-network", "participating provider", "network provider"],
    "out of network": ["out-of-network", "non-participating", "non-network"]
  }
}
```

---

## How It Works

### At Query Time — `tools.py`

```python
def expand_query_keywords(keywords: list, benefit_category: str) -> list:
    """
    Expands query keywords using knowledge base synonyms.
    Pure dict lookup — 0 tokens, microseconds.
    Called by tools.py before chunk scoring.

    Example:
        keywords = ["cleaning", "teeth"]
        benefit_category = "dental"
        → KB lookup: "cleaning" found in dental KB
        → canonical: "prophylaxis"
        → returns: ["cleaning", "teeth", "prophylaxis"]
    """
    kb = _load_knowledge_base()
    category_kb = kb.get(benefit_category, {})
    shared_kb = kb.get("shared", {})

    expanded = list(keywords)
    for keyword in keywords:
        # Check category-specific KB
        for canonical, synonyms in category_kb.items():
            if keyword in synonyms and canonical not in expanded:
                expanded.append(canonical)
            elif keyword == canonical:
                for syn in synonyms:
                    if syn not in expanded:
                        expanded.append(syn)
        # Check shared KB
        for canonical, synonyms in shared_kb.items():
            if keyword in synonyms and canonical not in expanded:
                expanded.append(canonical)

    return expanded
```

### At Index Time — `get_smart_keywords()`

```python
def get_smart_keywords(content, benefit_category: str = None) -> list:
    """
    Generates chunk keywords with KB synonym injection.
    benefit_category determines which KB section to use.
    """
    # ... domain patterns (Phase 1) ...
    # ... word fallback from event+service only (Phase 2) ...

    # Phase 3 — KB synonym injection
    if benefit_category:
        kb = _load_knowledge_base()
        category_kb = kb.get(benefit_category, {})
        enriched = list(found)
        for keyword in found:
            if keyword in category_kb:
                for syn in category_kb[keyword]:
                    if syn not in enriched and len(enriched) < 15:
                        enriched.append(syn)
        found = enriched

    return found[:15]  # increased from 10 to allow synonym injection
```

---

## How Knowledge Base Grows

### Phase 1 — Manual Curation (Day 1)
- We create `knowledge_base.json` with known synonyms
- Based on our understanding of dental/medical/vision terminology
- This is the starting point — covers the most common terms

### Phase 2 — LLM Growth (Ongoing)

`build_knowledge_base.py` runs AFTER each re-index:

```
1. Scan ALL index JSON files
2. Collect ALL chunk_keywords
3. For each keyword NOT in knowledge_base[benefit_category]:
       → LLM call: "What are plain-language synonyms for
                    '{keyword}' in {benefit_category} context?"
       → Add to knowledge_base[benefit_category]
4. Save updated knowledge_base.json
5. Next re-index → enriched keywords in chunks
6. Query time → expanded keywords from updated KB
```

### LLM Growth Trigger

```
Scheduled job order:
  1. Run all indexers (medical, dental, vision, sbc)
  2. Run build_knowledge_base.py  ← discovers unknowns, calls LLM
  3. Next scheduled indexer run   ← injects new synonyms into chunks
```

### LLM Cost

```
First run:  One call per unknown word (one-time cost)
Re-runs:    Only truly new words → minimal cost
Over time:  KB grows → fewer unknowns → cost approaches zero
```

---

## One Re-Index Cycle Lag

```
New plan indexed today:
  Step 1: Indexer runs → chunks created
          get_smart_keywords injects CURRENT KB synonyms
          New unknown words in chunks but NOT enriched yet ⚠️

  Step 2: build_knowledge_base.py runs
          Discovers unknown words → LLM enriches → KB updated ✅

  Step 3: Query time — KB expansion works immediately ✅
          "cleaning" → KB → "prophylaxis" → correct chunk found

  Step 4: Next re-index → chunks rebuilt with enriched keywords ✅
          Both layers now working for all words
```

The one-cycle lag is acceptable because:
- Query time expansion (Layer 1) works immediately after KB update
- Index enrichment (Layer 2) is a bonus — provides redundancy
- Same proven pattern as drug_words.json / condition_synonyms.json

---

## Double Work — Accepted Trade-off

Both query time and index time do KB lookups. This is intentional:

```
Query time:  "cleaning" → KB → add "prophylaxis" to query ✅
Index time:  "prophylaxis" → KB → add "cleaning" to chunk ✅

Both = pure dict lookup = microseconds = 0 tokens = 0 cost

Redundancy benefit:
  If query expansion works → chunk found ✅
  If chunk enriched → chunk found ✅
  If both work → extra confidence in scoring ✅
  If one fails → other catches it ✅
```

---

## Implementation Plan

### Step 1 — Create knowledge_base.json
- Manual curation of dental/medical/vision synonyms
- Stored in `utility/knowledge_base.json`

### Step 2 — KB Loader Utility
- `_load_knowledge_base()` in `utility/utils.py`
- Cached in memory (same pattern as condition_synonyms.json)
- 24h TTL

### Step 3 — Query Time Expansion in tools.py
- `expand_query_keywords(keywords, benefit_category)` 
- Called before chunk scoring
- Pure dict lookup, 0 tokens

### Step 4 — Index Time Enrichment in get_smart_keywords()
- Pass `benefit_category` parameter
- Category-specific domain patterns
- KB synonym injection into chunk_keywords
- Word fallback from event+service ONLY (no limitations prose)
- Extended stopword filter

### Step 5 — build_knowledge_base.py
- Scans all index files for unknown keywords
- LLM call per unknown word
- Updates knowledge_base.json
- Run after each re-index

### Step 6 — Re-index all plans
- Picks up enriched KB synonyms
- All chunks get enriched keywords

### Step 7 — Update golden tests
- Add queries that test synonym expansion
- e.g. "how much is a teeth cleaning?" → prophylaxis chunk
- Run full verify

---

## Files Changed

| File | Change |
|---|---|
| `utility/knowledge_base.json` | NEW — synonym knowledge base |
| `utility/utils.py` | Add `_load_knowledge_base()`, update `get_smart_keywords(benefit_category)` |
| `insurance_mcp/tools.py` | Add `expand_query_keywords()` call before scoring |
| `indexers/medical_indexer.py` | Pass `benefit_category="medical"` to `get_smart_keywords()` |
| `indexers/dental_indexer.py` | Pass `benefit_category="dental"` to `get_smart_keywords()` |
| `indexers/vision_indexer.py` | Pass `benefit_category="vision"` to `get_smart_keywords()` |
| `indexers/sbc_indexer.py` | Pass `benefit_category="sbc"` to `get_smart_keywords()` |
| `build_knowledge_base.py` | NEW — LLM growth script |

---

## Success Metrics

After implementation, these queries should work without LLM topic calls:

```
"how much is a teeth cleaning?"     → prophylaxis chunk ✅
"what does tooth removal cost?"     → extraction chunk ✅
"cost for full mouth xray"          → panoramic chunk ✅
"how much is a cap for my tooth?"   → crown chunk ✅
"what is the jaw pain benefit?"     → TMJ chunk ✅
"how much is physical therapy?"     → rehabilitation chunk ✅
"what does a chiropractor cost?"    → rehabilitation chunk ✅
"how much is an eye exam?"          → vision exam chunk ✅
"cost for glasses"                  → vision hardware chunk ✅
```