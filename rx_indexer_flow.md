# BENJI — Rx Indexer Flow

Reference document — garbage-page filtering, drug name word list, and illness-term classification.

> Read this before re-running the Rx indexer. The illness-classification step has a real one-time LLM cost — see the cost warning below before running anything.

---

## What Changed This Session

| File | Change |
|---|---|
| `rx_indexer.py` | New `find_drug_list_end()` — detects and skips the alphabetical index pages (the garbage-chunk source bug) |
| `rx_indexer.py` | `update_drug_names_file()` rewritten — builds `{word: [illness_terms]}` dict instead of a flat list, with one-time LLM classification per new word |
| `category.py` | `_load_drug_name_data()` — new dict-aware loader with 48h TTL, backward-compatible with old flat-list files |
| `category.py` | `_load_drug_name_words()` — now a thin wrapper, zero behavior change for existing callers (`is_drug_name_query`, `correct_drug_spelling`, `is_drug_match`) |
| `category.py` | New `get_illness_terms_for_word()` / `find_drug_words_for_illness()` — ready for `client.py` to use later, not wired in yet |
| `main.py` | `lifespan()` startup — syncs `drug_names.json` from Blob in production; uses local file directly in dev |
| `run_indexer.py` | Step 3b added — uploads `drug_names.json` to Blob after indexing (Rx only, no-op locally) |

---

## Why This Matters

Two real bugs traced back to the same root cause: alphabetical index pages at the end of the Rx formulary PDF were being indexed as if they were real drug entries.

- **Query-time bug**: "does humera need prior authorization?" returned garbage rows mixed in with real Humira results, because index-page chunks share keywords with real drugs (e.g. "humira" appears on every index page listing nearby drug names alphabetically).
- **Category-detection bug**: `_load_drug_name_words()` read `drug_name` from every chunk, including the garbage ones — polluting the drug word set used for spelling correction and category routing.

Fixing the indexer's page boundary detection (`find_drug_list_end`) fixes both problems at once — once the source index files are clean, everything downstream that reads from them is automatically clean too.

---

## Full Indexer Flow

Run command:

```bash
python -m indexers.rx_indexer <pdf_path> <output.json>
```

### Step 1 — `classify_document()`
- Reads page 1 of the PDF
- Detects E4 vs A2 variant, effective year
- No LLM call — pure text parsing

### Step 2 — `generate_sub_index(output_path, pdf_path)`
- `find_drug_list_start()` — finds where drug entries begin (existing, unchanged)
- `find_drug_list_end()` — **NEW** — finds where the alphabetical index begins, stops processing before it. Detects via the page literally starting with the word "Index", or as a backup, a high density of long lines packed with page-number references
- `parse_intro_pages()` — builds INFO chunks from intro pages (existing, unchanged)
- Main parsing loop — builds drug chunks ONLY between the start/end boundaries (garbage pages now excluded entirely)
- Writes `output_path` — the full booklet index JSON, as before
- Calls `update_drug_names_file(chunks)` — see Step 2b below

### Step 2b — `update_drug_names_file()` (runs automatically inside Step 2)
- Extracts all drug name words from THIS run's chunks
- Loads the existing `drug_names.json`, if any
- Computes `truly_new_words = new_words - already_known_words`
- If `truly_new_words` is empty → prints "unchanged", returns immediately, **0 LLM calls**
- If `truly_new_words` has entries → **one mini-LLM call PER NEW WORD** to classify illness/condition terms, then writes the updated `drug_names.json` with all words (old + new)

### Step 3 — `post_index_upload()`
- Uploads `output_path` (the full booklet index) to Blob storage
- No-op locally when `AZURE_BLOB_CONNECTION_STRING` is not set

### Step 3b — `drug_names.json` Blob sync (NEW, Rx only)
- If `plan_category == "rx"`, also uploads the updated `drug_names.json` to Blob
- So other server instances pick up new words on their next 48h TTL refresh
- No-op locally

```
RUN: python -m indexers.rx_indexer <pdf_path> <output.json>
  │
  ├─ Step 1: classify_document()
  │
  ├─ Step 2: generate_sub_index(output_path, pdf_path)
  │   ├─ find_drug_list_start()
  │   ├─ find_drug_list_end()      ← NEW
  │   ├─ parse_intro_pages()
  │   ├─ Main parsing loop (garbage pages excluded)
  │   ├─ Writes output_path
  │   └─ update_drug_names_file(chunks)   ← Step 2b
  │
  ├─ Step 3: post_index_upload()
  │
  └─ Step 3b: drug_names.json Blob sync   ← NEW, Rx only
```

---

## ⚠️ Cost Warning Before You Run This

The illness-classification step makes **ONE mini-LLM call per NEW drug word** never seen before, across ANY booklet.

| Scenario | Expected Cost |
|---|---|
| `drug_names.json` does not exist yet (first run with this new code) | EVERY drug word in the booklet is "new" — full classification run, real token cost |
| Re-running E4 after it's already in `drug_names.json` | 0 new words → 0 LLM calls, prints "unchanged" |
| Running A2 after E4 is already indexed | Only A2's genuinely unique words get classified — most overlap with E4 and are skipped |

**Recommended first run** — verify the garbage-page fix and dict structure work correctly WITHOUT the LLM cost:

```python
update_drug_names_file(chunks, classify_illness=False)
```

This skips illness classification entirely — drug words are still added to the file with an empty illness list, and can be backfilled by a later run with `classify_illness=True` once everything else is confirmed working.

---

## Pre-Flight Checklist

- [ ] Replace `rx_indexer.py`, `category.py`, `main.py`, `run_indexer.py`
- [ ] First run: call `update_drug_names_file` with `classify_illness=False` to verify structure, zero LLM cost
- [ ] Confirm garbage pages are skipped — check console for `"alphabetical index detected starting page X"`
- [ ] Re-test "does humera need prior authorization?" — should show ONLY real Humira entries
- [ ] Run full golden test verify — confirm zero regression across all 123 queries
- [ ] Once confirmed stable, run a SEPARATE deliberate pass with `classify_illness=True` to backfill illness terms
- [ ] Re-capture Rx baseline, run full verify again
- [ ] Check in

---

## Where `drug_names.json` Lives

```
Local:       indices/drug_names.json
Production:  Blob storage, synced to the same local path at server startup
```

Structure:

```json
{
  "metformin": ["diabetes", "blood sugar"],
  "ozempic":   ["diabetes", "weight loss"],
  "humira":    ["arthritis", "crohns", "psoriasis"]
}
```

Category detection only needs the **keys** of this file (is this word a drug name at all). Illness terms (the **values**) are reserved for the future condition-based query feature ("is my diabetes medicine covered?") — designed but not yet wired into `client.py`.