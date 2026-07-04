"""
rx_classifier.py — Standalone Drug Illness & Synonym Classifier

Runs INDEPENDENTLY of the PDF indexer. Reads existing drug_names.json
and classifies drug words into illness terms (Pass 2) and then generates
synonyms for each illness term (Pass 3).

Does NOT re-parse any PDF or re-index any documents.

Usage:
    # Pass 2 only — drug → illness terms
    python -m indexers.rx_classifier --illness

    # Pass 2 + Pass 3 — drug → illness + illness → synonyms
    python -m indexers.rx_classifier --illness --synonyms

    # Pass 3 only — illness → synonyms (assumes Pass 2 already done)
    python -m indexers.rx_classifier --synonyms

    # Force full redo — reclassify everything even if already done
    python -m indexers.rx_classifier --illness --synonyms --force

    # Dry run — show what would be classified without calling LLM
    python -m indexers.rx_classifier --illness --dry-run

How it works:
    Pass 2: drug → illness
        Reads drug_names.json — finds all entries with empty illness lists
        Sends batches of 25 drug words to LLM
        LLM returns: "metformin → diabetes, blood sugar"
        Saves back to drug_names.json incrementally (safe to interrupt)

    Pass 3: illness → synonyms
        Collects all unique illness terms from drug_names.json
        Finds terms with empty synonym lists in condition_synonyms.json
        Sends batches of 25 illness terms to LLM
        LLM returns: "diabetes → blood sugar, type 2, T2D, sugar levels"
        Saves to condition_synonyms.json incrementally

    Incremental by default:
        Already classified entries are SKIPPED — safe to re-run anytime
        Use --force to override and reclassify everything

    Progressive save:
        Saves after every batch — if interrupted, resume from where left off
        No work is lost on interruption
"""

import os
import sys
import json
import argparse
from datetime import datetime

# ── File paths ────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRUG_NAMES_FILE = os.path.join(_BASE_DIR, "indices", "drug_names.json")
CONDITION_SYNONYMS_FILE = os.path.join(_BASE_DIR, "indices", "condition_synonyms.json")


# ── Helpers ───────────────────────────────────────────────────────────────────


def load_drug_names() -> dict:
    """Load drug_names.json — {drug_word: [illness_terms]}"""
    if not os.path.exists(DRUG_NAMES_FILE):
        print(f"[!] drug_names.json not found at {DRUG_NAMES_FILE}")
        print(f"[!] Run the rx_indexer first to generate it")
        sys.exit(1)
    with open(DRUG_NAMES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Backward compat
    if isinstance(data, list):
        data = {word: [] for word in data}
    print(f"[*] Loaded drug_names.json: {len(data)} drug words")
    return data


def save_drug_names(data: dict) -> None:
    """Save drug_names.json incrementally."""
    with open(DRUG_NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def load_condition_synonyms() -> dict:
    """Load condition_synonyms.json — {condition: [synonyms]}"""
    if not os.path.exists(CONDITION_SYNONYMS_FILE):
        return {}
    with open(CONDITION_SYNONYMS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_condition_synonyms(data: dict) -> None:
    """Save condition_synonyms.json incrementally."""
    os.makedirs(os.path.dirname(CONDITION_SYNONYMS_FILE), exist_ok=True)
    with open(CONDITION_SYNONYMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ── Pass 2: Drug → Illness ────────────────────────────────────────────────────

_ILLNESS_TERM_STOPLIST = {
    "type",
    "disease",
    "disorder",
    "condition",
    "syndrome",
    "chronic",
    "acute",
    "common",
    "general",
    "related",
}


def _classify_illness_batch(batch: list) -> dict:
    """
    Sends a batch of drug words to LLM and returns illness term mappings.
    Format: {drug_word: [illness_term1, illness_term2, ...]}
    """
    from utility.llm import llm_chat

    drug_list = "\n".join(batch)
    results = {word: [] for word in batch}

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical terminology assistant. "
                    "For each drug name listed, return ONE line in this exact format:\n"
                    "drug_name → condition1, condition2\n\n"
                    "Rules:\n"
                    "- Use everyday patient language, NOT medical jargon\n"
                    "- Maximum 3 words per condition term\n"
                    "- Maximum 3 conditions per drug\n"
                    "- Return ONLY these lines, one per drug, same order as input\n"
                    "- If unsure or not a drug name, return: drug_name → \n\n"
                    "Examples:\n"
                    "metformin → diabetes, blood sugar\n"
                    "atorvastatin → high cholesterol\n"
                    "fluconazole → fungal infection, yeast infection\n"
                    "amoxicillin → bacterial infection\n"
                    "lisinopril → high blood pressure, heart failure\n"
                    "sertraline → depression, anxiety\n"
                    "omeprazole → acid reflux, heartburn, stomach ulcer"
                ),
            },
            {"role": "user", "content": drug_list},
        ]

        max_tokens = len(batch) * 25
        response = llm_chat(messages=messages, max_tokens=max_tokens)
        if not response:
            return results

        for line in response.strip().split("\n"):
            if "→" not in line and "->" not in line:
                continue
            separator = "→" if "→" in line else "->"
            parts = line.split(separator, 1)
            if len(parts) != 2:
                continue

            drug_word = parts[0].strip().lower()
            terms_raw = parts[1].strip()

            if drug_word not in [w.lower() for w in batch]:
                continue

            if not terms_raw:
                continue

            terms = [t.strip().lower() for t in terms_raw.split(",") if t.strip()]
            clean_terms = [
                t for t in terms if len(t) > 2 and t not in _ILLNESS_TERM_STOPLIST
            ]

            for original_word in batch:
                if original_word.lower() == drug_word:
                    results[original_word] = clean_terms[:3]
                    break

    except Exception as e:
        print(f"[!] Illness batch failed: {e}")

    return results


def run_illness_classification(
    drug_data: dict,
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = 25,
) -> dict:
    """
    Pass 2: Classify drug words into illness terms.

    Incremental — skips drugs that already have illness terms unless force=True.
    Saves progressively after each batch.

    Returns updated drug_data dict.
    """
    if force:
        to_classify = list(drug_data.keys())
        print(f"[*] Pass 2 (FORCE): reclassifying all {len(to_classify)} drug words")
    else:
        to_classify = [w for w, terms in drug_data.items() if not terms]
        already_done = len(drug_data) - len(to_classify)
        print(
            f"[*] Pass 2: {already_done} already classified, "
            f"{len(to_classify)} to classify"
        )

    if not to_classify:
        print("[*] Pass 2: nothing to do — all drugs already classified")
        return drug_data

    if dry_run:
        print(f"[DRY RUN] Would classify {len(to_classify)} drug words")
        print(f"[DRY RUN] Sample: {to_classify[:10]}")
        return drug_data

    total_batches = (len(to_classify) - 1) // batch_size + 1
    start_time = datetime.now()

    for i in range(0, len(to_classify), batch_size):
        batch = to_classify[i : i + batch_size]
        batch_num = i // batch_size + 1

        print(
            f"[*] Pass 2 batch {batch_num}/{total_batches} "
            f"({min(i + batch_size, len(to_classify))}/{len(to_classify)} drugs)..."
        )

        batch_results = _classify_illness_batch(batch)

        # Update data
        for drug_word, illness_terms in batch_results.items():
            drug_data[drug_word] = illness_terms

        # Progressive save after every batch
        save_drug_names(drug_data)

        # Progress estimate
        elapsed = (datetime.now() - start_time).seconds
        if batch_num > 1 and elapsed > 0:
            rate = batch_num / elapsed  # batches per second
            remaining = total_batches - batch_num
            eta_seconds = int(remaining / rate) if rate > 0 else 0
            eta_min = eta_seconds // 60
            eta_sec = eta_seconds % 60
            print(f"    ETA: ~{eta_min}m {eta_sec}s remaining")

    classified = sum(1 for terms in drug_data.values() if terms)
    print(f"[*] Pass 2 complete: {classified}/{len(drug_data)} drugs classified")
    return drug_data


# ── Pass 3: Illness → Synonyms ────────────────────────────────────────────────


def _classify_synonyms_batch(batch: list) -> dict:
    """
    Sends a batch of illness terms to LLM and returns synonym mappings.
    Format: {illness_term: [synonym1, synonym2, ...]}
    """
    from utility.llm import llm_chat

    illness_list = "\n".join(batch)
    results = {term: [] for term in batch}

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical terminology assistant specializing in patient communication. "
                    "For each medical condition listed, return ONE line with ALL the ways "
                    "a patient might describe it — informal language, abbreviations, and related terms.\n\n"
                    "Format: condition → synonym1, synonym2, synonym3\n\n"
                    "Rules:\n"
                    "- Use everyday patient language — how patients describe it to their doctor\n"
                    "- Include single words AND short phrases\n"
                    "- Include common abbreviations (bp, T2D, GERD etc.)\n"
                    "- Maximum 6 synonyms per condition\n"
                    "- Return ONLY these lines, one per condition\n"
                    "- If unsure, return: condition → \n\n"
                    "Examples:\n"
                    "diabetes → blood sugar, type 2, high blood sugar, sugar levels, T2D, sugar\n"
                    "hypertension → blood pressure, high blood pressure, high bp, bp, elevated blood pressure\n"
                    "high cholesterol → cholesterol, bad cholesterol, ldl, lipids, high lipids\n"
                    "acid reflux → heartburn, GERD, stomach acid, indigestion, reflux\n"
                    "bacterial infection → infection, bacteria, bacterial"
                ),
            },
            {"role": "user", "content": illness_list},
        ]

        max_tokens = len(batch) * 35
        response = llm_chat(messages=messages, max_tokens=max_tokens)
        if not response:
            return results

        for line in response.strip().split("\n"):
            if "→" not in line and "->" not in line:
                continue
            separator = "→" if "→" in line else "->"
            parts = line.split(separator, 1)
            if len(parts) != 2:
                continue

            term = parts[0].strip().lower()
            synonyms_raw = parts[1].strip()

            if term not in [t.lower() for t in batch]:
                continue

            if not synonyms_raw:
                continue

            synonyms = [s.strip().lower() for s in synonyms_raw.split(",") if s.strip()]
            clean_synonyms = [s for s in synonyms if len(s) >= 2 and s != term]

            for original_term in batch:
                if original_term.lower() == term:
                    results[original_term] = clean_synonyms[:6]
                    break

    except Exception as e:
        print(f"[!] Synonym batch failed: {e}")

    return results


def run_synonym_classification(
    drug_data: dict,
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = 25,
) -> dict:
    """
    Pass 3: Generate synonyms for each unique illness term.

    Collects all unique illness terms from drug_data, then classifies
    those not yet in condition_synonyms.json.

    Returns updated condition_synonyms dict.
    """
    # Collect all unique illness terms
    all_illness_terms = set()
    for illness_list in drug_data.values():
        for term in illness_list:
            if term and len(term) >= 3:
                all_illness_terms.add(term.lower())

    if not all_illness_terms:
        print("[!] Pass 3: no illness terms found in drug_names.json")
        print("[!] Run --illness first to classify drugs")
        return {}

    print(f"[*] Pass 3: {len(all_illness_terms)} unique illness terms found")

    # Load existing synonyms
    synonym_data = load_condition_synonyms()

    if force:
        to_classify = list(all_illness_terms)
        print(f"[*] Pass 3 (FORCE): reclassifying all {len(to_classify)} illness terms")
    else:
        to_classify = [
            t for t in all_illness_terms if t not in synonym_data or not synonym_data[t]
        ]
        already_done = len(all_illness_terms) - len(to_classify)
        print(
            f"[*] Pass 3: {already_done} already classified, "
            f"{len(to_classify)} to classify"
        )

    if not to_classify:
        print("[*] Pass 3: nothing to do — all conditions already have synonyms")
        return synonym_data

    if dry_run:
        print(f"[DRY RUN] Would classify {len(to_classify)} illness terms")
        print(f"[DRY RUN] Sample: {to_classify[:10]}")
        return synonym_data

    total_batches = (len(to_classify) - 1) // batch_size + 1
    start_time = datetime.now()

    for i in range(0, len(to_classify), batch_size):
        batch = to_classify[i : i + batch_size]
        batch_num = i // batch_size + 1

        print(
            f"[*] Pass 3 batch {batch_num}/{total_batches} "
            f"({min(i + batch_size, len(to_classify))}/{len(to_classify)} terms)..."
        )

        batch_results = _classify_synonyms_batch(batch)

        for term, synonyms in batch_results.items():
            synonym_data[term] = synonyms

        # Progressive save after every batch
        save_condition_synonyms(synonym_data)

        # Progress estimate
        elapsed = (datetime.now() - start_time).seconds
        if batch_num > 1 and elapsed > 0:
            rate = batch_num / elapsed
            remaining = total_batches - batch_num
            eta_seconds = int(remaining / rate) if rate > 0 else 0
            eta_min = eta_seconds // 60
            eta_sec = eta_seconds % 60
            print(f"    ETA: ~{eta_min}m {eta_sec}s remaining")

    classified = sum(1 for synonyms in synonym_data.values() if synonyms)
    print(
        f"[*] Pass 3 complete: {classified}/{len(synonym_data)} conditions with synonyms"
    )
    return synonym_data


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Drug Intelligence Classifier — illness mapping and synonym generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m indexers.rx_classifier --illness
  python -m indexers.rx_classifier --illness --synonyms
  python -m indexers.rx_classifier --synonyms
  python -m indexers.rx_classifier --illness --synonyms --force
  python -m indexers.rx_classifier --illness --dry-run
        """,
    )

    parser.add_argument(
        "--illness",
        action="store_true",
        help="Run Pass 2: classify drug words into illness terms",
    )
    parser.add_argument(
        "--synonyms",
        action="store_true",
        help="Run Pass 3: generate synonyms for each illness term",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reclassification of all entries (overrides existing data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be classified without calling LLM",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Number of items per LLM batch (default: 25)",
    )

    args = parser.parse_args()

    if not args.illness and not args.synonyms:
        parser.print_help()
        print("\n[!] Specify at least --illness or --synonyms")
        sys.exit(1)

    print("=" * 60)
    print("Drug Intelligence Classifier")
    print("=" * 60)
    print(f"  illness:    {args.illness}")
    print(f"  synonyms:   {args.synonyms}")
    print(f"  force:      {args.force}")
    print(f"  dry-run:    {args.dry_run}")
    print(f"  batch-size: {args.batch_size}")
    print("=" * 60)

    # Load drug data
    drug_data = load_drug_names()

    # Pass 2: Drug → Illness
    if args.illness:
        print("\n── Pass 2: Drug → Illness ──────────────────────────────────")
        drug_data = run_illness_classification(
            drug_data,
            force=args.force,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )

    # Pass 3: Illness → Synonyms
    if args.synonyms:
        print("\n── Pass 3: Illness → Synonyms ──────────────────────────────")
        run_synonym_classification(
            drug_data,
            force=args.force,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )

    print("\n" + "=" * 60)
    print("Classification complete")
    print(f"  drug_names.json:          {DRUG_NAMES_FILE}")
    print(f"  condition_synonyms.json:  {CONDITION_SYNONYMS_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
