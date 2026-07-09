"""
build_rxclass_lookup.py — Build drug illness mapping directly into drug_words.json.

Reads two flat files from the RxNorm Prescribable Content download:
    RXNCONSO.RRF  — drug/disease names and their rxcui IDs
    RXNREL.RRF    — relationships between rxcuis (has_ingredient, may_treat)

Writes illness mappings directly into:
    indices/drug_words.json  — illnesses[] filled for each drug

LLM fallback for drugs not found in RxNorm (~10% brand-only/new drugs).

Flow inside RRF files:
    RXNCONSO: "anoro" → rxcui=2121191
    RXNREL:   rxcui=2121191 --has_ingredient--> rxcui=1043498 (umeclidinium)
    RXNREL:   rxcui=1043498 --may_treat-------> rxcui=C0024117
    RXNCONSO: rxcui=C0024117 → "Pulmonary Disease, Chronic Obstructive"

Only runs against OUR drug list (drug_words.json) — not all 100K+ RxNorm drugs.

Usage:
    python build_rxclass_lookup.py --rrf C:/path/to/rrf/folder
    python build_rxclass_lookup.py --rrf C:/path/to/rrf/folder --force
    python build_rxclass_lookup.py --rrf C:/path/to/rrf/folder --test metformin lisinopril
"""

import os
import sys
import json
import argparse
import time
from collections import defaultdict

# ── File paths ────────────────────────────────────────────────────────────────
_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DRUG_WORDS_FILE  = os.path.join(_BASE_DIR, "indices", "drug_words.json")

# RRF files are pipe-delimited
RRF_SEP = "|"

# Sources
SAB_RXNORM = "RXNORM"
SAB_MEDRT  = "MEDRT"

# Relations
RELA_HAS_INGREDIENT = "has_ingredient"
RELA_MAY_TREAT      = "may_treat"

# Term types for drug name lookup
DRUG_TTY = {"IN", "BN", "MIN", "PIN", "SCD", "SBD"}


# ── Load drug_words.json ──────────────────────────────────────────────────────

def load_drug_words() -> dict:
    """Load drug_words.json — exits gracefully if missing."""
    if not os.path.exists(DRUG_WORDS_FILE):
        print()
        print("=" * 60)
        print("[!] drug_words.json not found")
        print("    Run rx_indexer first to generate it.")
        print("=" * 60)
        sys.exit(1)

    with open(DRUG_WORDS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    print(f"[*] Loaded drug_words.json: {len(data)} unique drug words")
    return data


def save_drug_words(data: dict) -> None:
    """Save drug_words.json with illnesses filled."""
    os.makedirs(os.path.dirname(DRUG_WORDS_FILE), exist_ok=True)
    with open(DRUG_WORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ── RRF Parsers ───────────────────────────────────────────────────────────────

def parse_rxnconso(rrf_path: str, our_drugs: set) -> tuple[dict, dict]:
    """
    Parse RXNCONSO.RRF — only loads entries relevant to our drug list.

    Returns:
        name_to_rxcui: {lowercase_name: rxcui}  — drug name → rxcui
        rxcui_to_name: {rxcui: str}              — disease rxcui → name
    """
    name_to_rxcui = {}
    rxcui_to_name = {}

    print(f"[*] Parsing RXNCONSO.RRF ...", end="", flush=True)
    t0 = time.time()
    rows = 0

    with open(rrf_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(RRF_SEP)
            if len(parts) < 15:
                continue

            rxcui   = parts[0].strip()
            sab     = parts[11].strip()
            tty     = parts[12].strip()
            str_val = parts[14].strip()

            if not rxcui or not str_val:
                continue

            # Drug names from RxNorm
            if sab == SAB_RXNORM and tty in DRUG_TTY:
                name_lower = str_val.lower()
                if name_lower not in name_to_rxcui:
                    name_to_rxcui[name_lower] = rxcui

            # Disease names from MED-RT
            if sab == SAB_MEDRT:
                if rxcui not in rxcui_to_name:
                    rxcui_to_name[rxcui] = str_val

            rows += 1

    elapsed = time.time() - t0
    print(f" done ({rows:,} rows, {len(name_to_rxcui):,} drug names, "
          f"{len(rxcui_to_name):,} disease classes, {elapsed:.1f}s)")
    return name_to_rxcui, rxcui_to_name


def parse_rxnrel(rrf_path: str) -> tuple[dict, dict]:
    """
    Parse RXNREL.RRF into relationship dicts.

    Returns:
        ingredient_of:   {rxcui: [ingredient_rxcuis]}  — brand → ingredients
        treats_diseases: {rxcui: [disease_rxcuis]}      — ingredient → diseases
    """
    ingredient_of   = defaultdict(list)
    treats_diseases = defaultdict(list)

    print(f"[*] Parsing RXNREL.RRF ...", end="", flush=True)
    t0 = time.time()
    rows = 0

    with open(rrf_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(RRF_SEP)
            if len(parts) < 11:
                continue

            rxcui1 = parts[0].strip()
            rxcui2 = parts[4].strip()
            rela   = parts[7].strip()
            sab    = parts[10].strip()

            if not rxcui1 or not rxcui2 or not rela:
                continue

            if sab == SAB_RXNORM and rela == RELA_HAS_INGREDIENT:
                ingredient_of[rxcui1].append(rxcui2)
            elif sab == SAB_MEDRT and rela == RELA_MAY_TREAT:
                treats_diseases[rxcui1].append(rxcui2)

            rows += 1

    elapsed = time.time() - t0
    print(f" done ({rows:,} rows, {len(ingredient_of):,} brand→ingredient, "
          f"{len(treats_diseases):,} drug→disease, {elapsed:.1f}s)")
    return dict(ingredient_of), dict(treats_diseases)


# ── Lookup ────────────────────────────────────────────────────────────────────

def lookup_illnesses(
    drug_word: str,
    name_to_rxcui: dict,
    rxcui_to_name: dict,
    ingredient_of: dict,
    treats_diseases: dict,
) -> list[str]:
    """
    Lookup illness names for a drug word via RxNorm relations.

    drug_word → rxcui → ingredient rxcuis → disease rxcuis → disease names
    """
    rxcui = name_to_rxcui.get(drug_word.lower())
    if not rxcui:
        return []

    # Collect rxcuis to check (drug itself + all its ingredients)
    rxcuis_to_check = {rxcui}
    for ing in ingredient_of.get(rxcui, []):
        rxcuis_to_check.add(ing)

    # Collect disease rxcuis
    disease_rxcuis = set()
    for check in rxcuis_to_check:
        for d in treats_diseases.get(check, []):
            disease_rxcuis.add(d)

    # Resolve to names
    seen = set()
    names = []
    for d_rxcui in disease_rxcuis:
        name = rxcui_to_name.get(d_rxcui)
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    return sorted(names)


# ── LLM Fallback ──────────────────────────────────────────────────────────────

def _llm_classify_batch(drug_words: list) -> dict:
    """
    LLM fallback for drugs not found in RxNorm.
    Returns {drug_word: [illness_names]} using clinical terminology
    to stay consistent with MED-RT disease names.
    """
    sys.path.insert(0, _BASE_DIR)
    from utility.llm import llm_chat

    drug_list = "\n".join(drug_words)
    results = {w: [] for w in drug_words}

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical terminology assistant. "
                    "For each drug name listed, return ONE line:\n"
                    "drug_name → Condition Name 1, Condition Name 2\n\n"
                    "Rules:\n"
                    "- Use proper clinical condition names (as found in medical databases)\n"
                    "- Maximum 3 conditions per drug\n"
                    "- If not a real drug, return: drug_name → \n"
                    "- Return ONLY these lines, one per drug\n\n"
                    "Examples:\n"
                    "humira → Rheumatoid Arthritis, Crohn's Disease, Psoriasis\n"
                    "keytruda → Melanoma, Lung Cancer\n"
                    "dupixent → Atopic Dermatitis, Asthma\n"
                    "ozempic → Diabetes Mellitus, Type 2, Obesity"
                ),
            },
            {"role": "user", "content": drug_list},
        ]

        response = llm_chat(messages=messages, max_tokens=len(drug_words) * 30)
        if not response:
            return results

        for line in response.strip().split("\n"):
            if "→" not in line and "->" not in line:
                continue
            sep = "→" if "→" in line else "->"
            parts = line.split(sep, 1)
            if len(parts) != 2:
                continue

            drug_word = parts[0].strip().lower()
            terms_raw = parts[1].strip()
            if not terms_raw:
                continue

            terms = [t.strip() for t in terms_raw.split(",") if t.strip()]
            for original in drug_words:
                if original.lower() == drug_word:
                    results[original] = terms[:3]
                    break

    except Exception as e:
        print(f"[!] LLM batch failed: {e}")

    return results


# ── Main Build ────────────────────────────────────────────────────────────────

def build(rrf_dir: str, force: bool = False, batch_size: int = 25) -> None:
    """
    Main build — fills illnesses[] directly into drug_words.json.
    """
    rxnconso_path = os.path.join(rrf_dir, "RXNCONSO.RRF")
    rxnrel_path   = os.path.join(rrf_dir, "RXNREL.RRF")

    for path in [rxnconso_path, rxnrel_path]:
        if not os.path.exists(path):
            print(f"[!] File not found: {path}")
            sys.exit(1)

    print("=" * 60)
    print("Building Drug → Illness Mapping from RxNorm RRF files")
    print("=" * 60)
    print(f"  RXNCONSO: {rxnconso_path}")
    print(f"  RXNREL:   {rxnrel_path}")
    print(f"  Output:   {DRUG_WORDS_FILE}")
    print("=" * 60)

    t_start = time.time()

    # Load our drug list
    drug_words_data = load_drug_words()

    # Determine which drugs need classification
    if force:
        to_classify = [
            w for w, e in drug_words_data.items()
            if isinstance(e, dict) and e.get("entry_type") != "vitamin"
        ]
        print(f"[*] FORCE: reclassifying all {len(to_classify)} non-vitamin drugs")
    else:
        to_classify = [
            w for w, e in drug_words_data.items()
            if isinstance(e, dict)
            and e.get("entry_type") != "vitamin"
            and not e.get("illnesses")
        ]
        already_done = sum(
            1 for e in drug_words_data.values()
            if isinstance(e, dict) and e.get("illnesses")
        )
        vitamins = sum(
            1 for e in drug_words_data.values()
            if isinstance(e, dict) and e.get("entry_type") == "vitamin"
        )
        print(f"[*] {already_done} already classified, "
              f"{vitamins} vitamins skipped, "
              f"{len(to_classify)} to classify")

    if not to_classify:
        print("[*] Nothing to classify — all drugs already have illness mappings")
        return

    our_drug_set = set(w.lower() for w in to_classify)

    # Parse RRF files
    name_to_rxcui, rxcui_to_name = parse_rxnconso(rxnconso_path, our_drug_set)
    ingredient_of, treats_diseases = parse_rxnrel(rxnrel_path)

    # Lookup illnesses for each drug
    print(f"\n[*] Looking up {len(to_classify)} drugs in RxNorm MED-RT ...")
    rxnorm_hits = 0
    llm_needed  = []

    for word in to_classify:
        illnesses = lookup_illnesses(
            word, name_to_rxcui, rxcui_to_name,
            ingredient_of, treats_diseases
        )
        if illnesses:
            drug_words_data[word]["illnesses"] = illnesses
            rxnorm_hits += 1
        else:
            llm_needed.append(word)

    print(f"[*] RxNorm MED-RT: {rxnorm_hits} classified, "
          f"{len(llm_needed)} need LLM fallback")

    # Save after RxNorm hits
    save_drug_words(drug_words_data)

    # LLM fallback
    if llm_needed:
        print(f"\n[*] LLM fallback for {len(llm_needed)} drugs ...")
        total_batches = (len(llm_needed) - 1) // batch_size + 1

        for i in range(0, len(llm_needed), batch_size):
            batch = llm_needed[i:i + batch_size]
            batch_num = i // batch_size + 1
            print(f"[*] LLM batch {batch_num}/{total_batches} "
                  f"({min(i + batch_size, len(llm_needed))}/{len(llm_needed)}) ...")

            batch_results = _llm_classify_batch(batch)
            for word, illnesses in batch_results.items():
                if word in drug_words_data:
                    drug_words_data[word]["illnesses"] = illnesses

            # Progressive save after each batch
            save_drug_words(drug_words_data)

    # Final stats
    elapsed = time.time() - t_start
    classified = sum(
        1 for e in drug_words_data.values()
        if isinstance(e, dict) and e.get("illnesses")
    )

    print(f"\n[*] Done in {elapsed:.1f}s")
    print(f"    {rxnorm_hits} classified from RxNorm MED-RT")
    print(f"    {len(llm_needed)} via LLM fallback")
    print(f"    {classified} total with illness mappings")
    print(f"    Output: {DRUG_WORDS_FILE}")

    # Print LLM fallback list
    if llm_needed:
        print(f"\n[*] LLM fallback drugs (not in RxNorm — brand-only or new drugs):")
        for w in sorted(llm_needed):
            illnesses = drug_words_data.get(w, {}).get("illnesses", [])
            status = f"→ {illnesses}" if illnesses else "→ [no classification]"
            print(f"    - {w} {status}")


# ── Test Mode ─────────────────────────────────────────────────────────────────

def test(drug_words: list, rrf_dir: str) -> None:
    """Quick test — look up specific drugs and print results."""
    rxnconso_path = os.path.join(rrf_dir, "RXNCONSO.RRF")
    rxnrel_path   = os.path.join(rrf_dir, "RXNREL.RRF")

    name_to_rxcui, rxcui_to_name = parse_rxnconso(rxnconso_path, set(drug_words))
    ingredient_of, treats_diseases = parse_rxnrel(rxnrel_path)

    print(f"\n{'=' * 60}")
    print("Test Lookups")
    print('=' * 60)

    for word in drug_words:
        illnesses = lookup_illnesses(
            word, name_to_rxcui, rxcui_to_name,
            ingredient_of, treats_diseases
        )
        rxcui = name_to_rxcui.get(word.lower(), "NOT FOUND")
        print(f"\n  {word} (rxcui={rxcui})")
        if illnesses:
            for ill in illnesses:
                print(f"    → {ill}")
        else:
            print(f"    → (not found in MED-RT)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fill drug_words.json illnesses[] from RxNorm RRF files"
    )
    parser.add_argument(
        "--rrf", required=True,
        help="Path to folder containing RXNCONSO.RRF and RXNREL.RRF"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reclassify all drugs even if already classified"
    )
    parser.add_argument(
        "--batch-size", type=int, default=25,
        help="LLM batch size for fallback (default: 25)"
    )
    parser.add_argument(
        "--test", nargs="+", metavar="DRUG",
        help="Test mode — look up specific drugs and print results"
    )

    args = parser.parse_args()

    if args.test:
        test(args.test, args.rrf)
    else:
        build(args.rrf, force=args.force, batch_size=args.batch_size)


if __name__ == "__main__":
    main()