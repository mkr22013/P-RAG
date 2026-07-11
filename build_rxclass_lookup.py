"""
build_rxclass_lookup.py -- Build drug illness mapping directly into drug_words.json.

Two data sources:
    1. Core_MEDRT_DTS.xml  -- may_treat relations (primary)
    2. RXNREL.RRF          -- brand->ingredient links (fallback for brands not in MED-RT)

Flow:
    Generic drugs:  drug_word -> MED-RT may_treat -> illnesses
    Brand drugs:    drug_word -> RXNREL has_ingredient -> ingredient -> MED-RT may_treat -> illnesses

Usage:
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf --force
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf --test metformin anoro humira
"""

import os
import re
import sys
import json
import time
import argparse

# -- File paths ----------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRUG_WORDS_FILE = os.path.join(_BASE_DIR, "indices", "drug_words.json")


# -- Load/Save drug_words.json ------------------------------------------------


def load_drug_words() -> dict:
    if not os.path.exists(DRUG_WORDS_FILE):
        print()
        print("=" * 60)
        print("[!] drug_words.json not found")
        print("    Run rx_indexer first:")
        print("    python -m indexers.rx_indexer docs/2026/rx/052149_2026.pdf")
        print("=" * 60)
        sys.exit(1)
    with open(DRUG_WORDS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    print(f"[*] Loaded drug_words.json: {len(data)} unique drug words")
    return data


def save_drug_words(data: dict) -> None:
    os.makedirs(os.path.dirname(DRUG_WORDS_FILE), exist_ok=True)
    with open(DRUG_WORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# -- Parse MED-RT XML ---------------------------------------------------------


def parse_medrt_xml(xml_path: str) -> dict:
    """
    Parse Core_MEDRT_DTS.xml and extract all may_treat relations.

    Returns:
        {drug_name_lower: [disease_name, ...]}

    XML structure:
        <association>
          <name>may_treat</name>
          <from_name>metformin [6809]</from_name>
          <to_name>Diabetes Mellitus, Type 2 [M0006155]</to_name>
        </association>
    """
    if not os.path.exists(xml_path):
        print(f"[!] MED-RT XML not found: {xml_path}")
        sys.exit(1)

    print(f"[*] Parsing MED-RT XML ...", end="", flush=True)
    t0 = time.time()

    name_re = re.compile(r"<name>(.*?)</name>")
    from_re = re.compile(r"<from_name>(.*?)\s*\[.*?\]</from_name>")
    to_re = re.compile(r"<to_name>(.*?)\s*\[.*?\]</to_name>")

    drug_to_illnesses: dict = {}
    may_treat_count = 0

    with open(xml_path, encoding="utf-8") as f:
        content = f.read()

    blocks = content.split("<association>")

    for block in blocks[1:]:
        name_match = name_re.search(block)
        if not name_match or name_match.group(1).strip() != "may_treat":
            continue

        from_match = from_re.search(block)
        to_match = to_re.search(block)
        if not from_match or not to_match:
            continue

        drug_name = from_match.group(1).strip().lower()
        disease_name = re.sub(r"\s*\[.*?\]\s*$", "", to_match.group(1).strip()).strip()

        if not drug_name or not disease_name:
            continue

        if drug_name not in drug_to_illnesses:
            drug_to_illnesses[drug_name] = []
        if disease_name not in drug_to_illnesses[drug_name]:
            drug_to_illnesses[drug_name].append(disease_name)

        may_treat_count += 1

    elapsed = time.time() - t0
    print(
        f" done ({may_treat_count:,} may_treat relations, "
        f"{len(drug_to_illnesses):,} unique drugs, {elapsed:.1f}s)"
    )

    return drug_to_illnesses


# -- Parse RXNREL.RRF for brand->ingredient links -----------------------------


def parse_rxnrel_ingredients(rrf_dir: str) -> dict:
    """
    Parse RXNREL.RRF and extract brand->ingredient links.

    Returns:
        {rxcui: [ingredient_name_lower, ...]}

    We also build a rxcui->name lookup from RXNCONSO so we can
    resolve ingredient rxcuis back to names for MED-RT lookup.
    """
    rxnconso_path = os.path.join(rrf_dir, "RXNCONSO.RRF")
    rxnrel_path = os.path.join(rrf_dir, "RXNREL.RRF")

    if not os.path.exists(rxnconso_path) or not os.path.exists(rxnrel_path):
        print(f"[!] RXNCONSO.RRF or RXNREL.RRF not found in: {rrf_dir}")
        return {}

    # Step 1: build rxcui->name from RXNCONSO (RXNORM source, ingredient types only)
    print(f"[*] Parsing RXNCONSO.RRF for rxcui->name ...", end="", flush=True)
    t0 = time.time()
    rxcui_to_name = {}
    name_to_rxcui = {}

    with open(rxnconso_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split("|")
            if len(parts) < 15:
                continue
            rxcui = parts[0].strip()
            sab = parts[11].strip()
            tty = parts[12].strip()
            str_val = parts[14].strip()

            if sab == "RXNORM" and tty in {"IN", "BN", "MIN", "PIN"}:
                name_lower = str_val.lower()
                if name_lower not in name_to_rxcui:
                    name_to_rxcui[name_lower] = rxcui
                if rxcui not in rxcui_to_name:
                    rxcui_to_name[rxcui] = str_val.lower()

    print(f" done ({len(rxcui_to_name):,} entries, {time.time()-t0:.1f}s)")

    # Step 2: build brand_rxcui -> [ingredient_names] from RXNREL
    print(f"[*] Parsing RXNREL.RRF for brand->ingredient ...", end="", flush=True)
    t0 = time.time()
    brand_to_ingredients: dict = {}  # rxcui -> [ingredient_name]

    with open(rxnrel_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split("|")
            if len(parts) < 11:
                continue
            rxcui1 = parts[0].strip()
            rxcui2 = parts[4].strip()
            rela = parts[7].strip()
            sab = parts[10].strip()

            if sab == "RXNORM" and rela in (
                "has_ingredient",  # brand -> ingredient (clinical drug -> ingredient)
                "has_tradename",  # ingredient -> brand name (e.g. adalimumab -> humira)
                # stored as rxcui1=brand, rxcui2=ingredient in reverse
                "ingredient_of",  # dose form -> ingredient
                "constitutes",  # branded form -> clinical form
                "isa",  # subtype -> parent
            ):
                ing_name = rxcui_to_name.get(rxcui2)
                if ing_name:
                    if rxcui1 not in brand_to_ingredients:
                        brand_to_ingredients[rxcui1] = []
                    if ing_name not in brand_to_ingredients[rxcui1]:
                        brand_to_ingredients[rxcui1].append(ing_name)
                # Also store reverse mapping: rxcui2 -> rxcui1 name
                # So adalimumab (327361) maps back to humira variants
                ing_name2 = rxcui_to_name.get(rxcui1)
                if ing_name2:
                    if rxcui2 not in brand_to_ingredients:
                        brand_to_ingredients[rxcui2] = []
                    if ing_name2 not in brand_to_ingredients[rxcui2]:
                        brand_to_ingredients[rxcui2].append(ing_name2)

    print(
        f" done ({len(brand_to_ingredients):,} brands with ingredients, "
        f"{time.time()-t0:.1f}s)"
    )

    # Step 3: build drug_name -> [ingredient_names]
    # Key insight: store by FIRST WORD of drug name so "humira" matches
    # "humira 40 mg per 0.8 ml injection" -> rxcui=352334 -> adalimumab
    drug_name_to_ingredients: dict = {}
    for drug_name, rxcui in name_to_rxcui.items():
        if rxcui in brand_to_ingredients:
            # Store by full name
            drug_name_to_ingredients[drug_name] = brand_to_ingredients[rxcui]
            # Also store by first word for fuzzy matching
            first_word = drug_name.split()[0] if drug_name else ""
            if first_word and first_word not in drug_name_to_ingredients:
                drug_name_to_ingredients[first_word] = brand_to_ingredients[rxcui]
            elif first_word and first_word in drug_name_to_ingredients:
                # Union ingredients from multiple variants
                existing = set(drug_name_to_ingredients[first_word])
                existing.update(brand_to_ingredients[rxcui])
                drug_name_to_ingredients[first_word] = list(existing)

    print(f"[*] Brand->ingredient map: {len(drug_name_to_ingredients):,} entries")
    return drug_name_to_ingredients


# -- Core lookup --------------------------------------------------------------


def lookup_illnesses(
    drug_word: str,
    full_names: list,
    medrt: dict,
    brand_to_ingredients: dict,
) -> list:
    """
    Find illnesses for a drug using MED-RT + RXNREL brand->ingredient fallback.

    Strategy:
    1. Exact match on drug_word in MED-RT
    2. Match on full_names in MED-RT
    3. Prefix match in MED-RT
    4. Brand->ingredient hop via RXNREL, then lookup ingredients in MED-RT
    """
    seen = set()
    illnesses = []

    def add(names):
        for n in names:
            if n not in seen:
                seen.add(n)
                illnesses.append(n)

    # Strategy 1: exact drug word match in MED-RT
    if drug_word in medrt:
        add(medrt[drug_word])

    # Strategy 2: match on full_names in MED-RT
    for full_name in full_names:
        full_lower = full_name.lower().strip()
        if full_lower in medrt:
            add(medrt[full_lower])
        # First two words
        words = full_lower.split()
        if len(words) >= 2:
            two = f"{words[0]} {words[1]}"
            if two in medrt:
                add(medrt[two])

    # Strategy 3: prefix match in MED-RT
    if not illnesses:
        prefix = drug_word + " "
        for medrt_name, diseases in medrt.items():
            if medrt_name.startswith(prefix):
                add(diseases)

    # Strategy 4: brand->ingredient hop via RXNREL
    if not illnesses and brand_to_ingredients:
        # Check drug_word
        ingredients = brand_to_ingredients.get(drug_word, [])
        # Also check full_names first words
        for full_name in full_names:
            first = full_name.lower().split()[0] if full_name else ""
            if first and first in brand_to_ingredients:
                for ing in brand_to_ingredients[first]:
                    if ing not in ingredients:
                        ingredients.append(ing)

        for ingredient in ingredients:
            if ingredient in medrt:
                add(medrt[ingredient])
            else:
                # Try prefix match for ingredient too
                prefix = ingredient + " "
                for medrt_name, diseases in medrt.items():
                    if medrt_name.startswith(prefix):
                        add(diseases)

    return sorted(illnesses)


# -- LLM fallback -------------------------------------------------------------


def _llm_classify_batch(drug_words: list, drug_words_data: dict = None) -> dict:
    """
    LLM fallback split by entry_type:
    - vaccines: ask what disease it prevents
    - drugs: ask what condition it treats (simple prompt, local LLM friendly)
    """
    sys.path.insert(0, _BASE_DIR)
    from utility.llm import llm_chat

    results = {w: [] for w in drug_words}
    drug_words_data = drug_words_data or {}

    # Split by entry_type
    vaccines = [
        w
        for w in drug_words
        if drug_words_data.get(w, {}).get("entry_type") == "vaccine"
    ]
    drugs = [
        w
        for w in drug_words
        if drug_words_data.get(w, {}).get("entry_type") != "vaccine"
    ]

    # -- Vaccine prompt -------------------------------------------------------
    if vaccines:
        vaccine_list = "\n".join(vaccines)
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical assistant. "
                        "For each vaccine name, return ONE line:\n"
                        "vaccine_name -> Disease Name\n\n"
                        "Return the disease the vaccine PREVENTS.\n"
                        "Use standard disease names. Max 2 diseases.\n"
                        "If unknown, return: vaccine_name -> \n\n"
                        "Examples:\n"
                        "gardasil -> Human Papillomavirus Infections\n"
                        "shingrix -> Herpes Zoster\n"
                        "fluzone -> Influenza\n"
                        "havrix -> Hepatitis A\n"
                        "engerix -> Hepatitis B\n"
                        "arexvy -> RSV Infection\n"
                        "prevnar -> Pneumococcal Infections\n"
                        "varivax -> Chickenpox\n"
                        "m-m-r -> Measles, Mumps, Rubella Infections\n"
                        "rotarix -> Rotavirus Infections\n"
                        "comirnaty -> COVID-19\n"
                        "spikevax -> COVID-19"
                    ),
                },
                {"role": "user", "content": vaccine_list},
            ]
            response = llm_chat(messages=messages, max_tokens=len(vaccines) * 20)
            if response:
                for line in response.strip().split("\n"):
                    if "->" not in line:
                        continue
                    parts = line.split("->", 1)
                    if len(parts) != 2:
                        continue
                    word = parts[0].strip().lower()
                    terms_raw = parts[1].strip()
                    if not terms_raw:
                        continue
                    terms = [
                        t.strip()
                        for t in terms_raw.split(",")
                        if t.strip() and t.strip() not in ("->", ">", "-")
                    ]
                    for original in vaccines:
                        if original.lower() == word:
                            results[original] = terms[:2]
                            break
        except Exception as e:
            print(f"[!] Vaccine LLM batch failed: {e}")

    # -- Drug prompt ----------------------------------------------------------
    if drugs:
        drug_list = "\n".join(drugs)
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical assistant. "
                        "For each drug name, return ONE line:\n"
                        "drug_name -> Condition Name 1, Condition Name 2\n\n"
                        "Return the medical condition(s) the drug TREATS.\n"
                        "Use standard clinical names. Max 3 conditions.\n"
                        "IMPORTANT: separate conditions with semicolons (;) not commas.\n"
                        "Condition names may contain commas e.g. Diabetes Mellitus Type 2\n"
                        "If not a real drug or unknown, return: drug_name -> \n\n"
                        "Examples:\n"
                        "ozempic -> Diabetes Mellitus Type 2; Obesity\n"
                        "paxlovid -> COVID-19\n"
                        "trikafta -> Cystic Fibrosis\n"
                        "brilinta -> Myocardial Ischemia; Thromboembolism\n"
                        "sprintec -> Contraception\n"
                        "mirena -> Menorrhagia; Contraception\n"
                        "shingrix -> Herpes Zoster\n"
                        "wegovy -> Obesity\n"
                        "saxenda -> Obesity\n"
                        "chantix -> Nicotine Dependence\n"
                        "tecfidera -> Multiple Sclerosis\n"
                        "symdeko -> Cystic Fibrosis\n"
                        "takhzyro -> Hereditary Angioedema\n"
                        "aimovig -> Migraine Disorders\n"
                        "gardasil -> \n"
                        "dexcom -> \n"
                        "lancets -> "
                    ),
                },
                {"role": "user", "content": drug_list},
            ]
            response = llm_chat(messages=messages, max_tokens=len(drugs) * 25)
            if response:
                for line in response.strip().split("\n"):
                    if "->" not in line:
                        continue
                    parts = line.split("->", 1)
                    if len(parts) != 2:
                        continue
                    word = parts[0].strip().lower()
                    terms_raw = parts[1].strip()
                    if not terms_raw:
                        continue
                    # Split on semicolons to preserve condition names with commas
                    # e.g. "Diabetes Mellitus Type 2; Obesity" -> 2 terms
                    terms = [
                        t.strip()
                        for t in terms_raw.split(";")
                        if t.strip() and t.strip() not in ("->", ">", "-")
                    ]
                    # Fallback: if no semicolons, try comma split
                    if len(terms) == 1 and "," in terms_raw:
                        terms = [
                            t.strip()
                            for t in terms_raw.split(",")
                            if t.strip() and t.strip() not in ("->", ">", "-")
                        ]
                    for original in drugs:
                        if original.lower() == word:
                            results[original] = terms[:3]
                            break
        except Exception as e:
            print(f"[!] Drug LLM batch failed: {e}")

    return results


# -- Main build ---------------------------------------------------------------


def build(
    xml_path: str, rrf_dir: str, force: bool = False, batch_size: int = 25
) -> None:
    """Fill illnesses[] directly into drug_words.json."""
    print("=" * 60)
    print("Building Drug -> Illness Mapping")
    print("=" * 60)
    print(f"  MED-RT XML: {xml_path}")
    print(f"  RRF dir:    {rrf_dir}")
    print(f"  Output:     {DRUG_WORDS_FILE}")
    print("=" * 60)

    t_start = time.time()

    drug_words_data = load_drug_words()

    # Determine which drugs need classification
    if force:
        to_classify = [
            w
            for w, e in drug_words_data.items()
            if isinstance(e, dict) and e.get("entry_type") != "vitamin"
        ]
        print(f"[*] FORCE: reclassifying all {len(to_classify)} non-vitamin drugs")
    else:
        to_classify = [
            w
            for w, e in drug_words_data.items()
            if isinstance(e, dict)
            and e.get("entry_type") != "vitamin"
            and not e.get("illnesses")
        ]
        already_done = sum(
            1
            for e in drug_words_data.values()
            if isinstance(e, dict) and e.get("illnesses")
        )
        vitamins = sum(
            1
            for e in drug_words_data.values()
            if isinstance(e, dict) and e.get("entry_type") == "vitamin"
        )
        print(
            f"[*] {already_done} already classified, "
            f"{vitamins} vitamins skipped, "
            f"{len(to_classify)} to classify"
        )

    if not to_classify:
        print("[*] Nothing to classify")
        return

    # Parse data sources
    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}

    # Lookup illnesses for each drug
    print(f"\n[*] Looking up {len(to_classify)} drugs ...")
    medrt_hits = 0
    llm_needed = []

    for word in to_classify:
        full_names = drug_words_data[word].get("full_names", [])
        illnesses = lookup_illnesses(word, full_names, medrt, brand_to_ingredients)

        if illnesses:
            drug_words_data[word]["illnesses"] = illnesses
            medrt_hits += 1
        else:
            llm_needed.append(word)

    print(
        f"[*] MED-RT + RXNREL: {medrt_hits} classified, "
        f"{len(llm_needed)} need LLM fallback"
    )

    save_drug_words(drug_words_data)

    # LLM fallback
    # Remove devices from LLM fallback -- they have no illness classification
    device_skipped = [
        w
        for w in llm_needed
        if drug_words_data.get(w, {}).get("entry_type") == "device"
    ]
    llm_needed = [
        w
        for w in llm_needed
        if drug_words_data.get(w, {}).get("entry_type") != "device"
    ]

    if device_skipped:
        print(
            f"[*] Skipped {len(device_skipped)} devices (no illness classification needed)"
        )

    if llm_needed:
        print(f"\n[*] LLM fallback for {len(llm_needed)} drugs ...")
        total_batches = (len(llm_needed) - 1) // batch_size + 1

        for i in range(0, len(llm_needed), batch_size):
            batch = llm_needed[i : i + batch_size]
            batch_num = i // batch_size + 1
            print(
                f"[*] LLM batch {batch_num}/{total_batches} "
                f"({min(i + batch_size, len(llm_needed))}/{len(llm_needed)}) ..."
            )

            batch_results = _llm_classify_batch(batch, drug_words_data)
            for word, illnesses in batch_results.items():
                if word in drug_words_data:
                    drug_words_data[word]["illnesses"] = illnesses

            save_drug_words(drug_words_data)

    # Final stats
    elapsed = time.time() - t_start
    classified = sum(
        1
        for e in drug_words_data.values()
        if isinstance(e, dict) and e.get("illnesses")
    )

    print(f"\n[*] Done in {elapsed:.1f}s")
    print(f"    {medrt_hits} classified from MED-RT + RXNREL")
    print(f"    {len(llm_needed)} via LLM fallback")
    print(f"    {classified} total with illness mappings")

    if llm_needed:
        print(f"\n[*] LLM fallback drugs (not in MED-RT):")
        for w in sorted(llm_needed):
            illnesses = drug_words_data.get(w, {}).get("illnesses", [])
            status = f"-> {illnesses}" if illnesses else "-> [no classification]"
            print(f"    - {w} {status}")


# -- Test mode ----------------------------------------------------------------


def test(drug_words: list, xml_path: str, rrf_dir: str) -> None:
    """Quick test -- look up specific drugs and print results."""
    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}

    print(f"\n{'=' * 60}")
    print("Test Lookups")
    print("=" * 60)

    for word in drug_words:
        illnesses = lookup_illnesses(word, [], medrt, brand_to_ingredients)
        in_medrt = word.lower() in medrt
        via_rxnrel = not in_medrt and bool(illnesses)
        source = (
            "MED-RT" if in_medrt else ("RXNREL->MED-RT" if via_rxnrel else "NOT FOUND")
        )
        print(f"\n  {word} ({source})")
        if illnesses:
            for ill in illnesses:
                print(f"    -> {ill}")
        else:
            print(f"    -> (not found)")


# -- Validate mode ------------------------------------------------------------


def validate(drug_words: list, xml_path: str, rrf_dir: str) -> None:
    """
    Validate specific drugs against MED-RT XML and RXNREL.
    Shows exactly why a drug is not found and what alternatives exist.
    Useful for debugging LLM fallback drugs.
    """
    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}

    print(f"\n{'=' * 60}")
    print("Validation - Checking drugs against MED-RT + RXNREL")
    print("=" * 60)

    for word in drug_words:
        print(f"\n  [{word}]")

        # Check direct MED-RT match
        if word.lower() in medrt:
            print(f"    FOUND in MED-RT directly")
            for ill in medrt[word.lower()]:
                print(f"      -> {ill}")
            continue

        # Check brand_to_ingredients
        ingredients = brand_to_ingredients.get(word.lower(), [])
        if ingredients:
            print(f"    FOUND in RXNREL -> ingredients: {ingredients}")
            for ing in ingredients:
                if ing in medrt:
                    print(f"    ingredient '{ing}' found in MED-RT:")
                    for ill in medrt[ing]:
                        print(f"      -> {ill}")
                else:
                    print(f"    ingredient '{ing}' NOT in MED-RT")
            continue

        # Try prefix match
        prefix = word.lower() + " "
        prefix_matches = [k for k in medrt if k.startswith(prefix)]
        if prefix_matches:
            print(f"    FOUND via prefix match: {prefix_matches[:3]}")
            for match in prefix_matches[:2]:
                for ill in medrt[match]:
                    print(f"      -> {ill}")
            continue

        # Check if it exists in RXNCONSO at all
        print(f"    NOT FOUND in MED-RT, RXNREL, or prefix match")
        print(f"    -> Will use LLM fallback")


# -- Test LLM mode ------------------------------------------------------------


def test_llm(drug_words: list, xml_path: str, rrf_dir: str) -> None:
    """
    Test LLM classification on specific drugs without touching drug_words.json.
    Useful for verifying prompt quality before full --force run.
    """
    drug_words_data = load_drug_words()

    # Filter to only the requested words
    subset = {w: drug_words_data[w] for w in drug_words if w in drug_words_data}
    missing = [w for w in drug_words if w not in drug_words_data]

    if missing:
        print(f"[!] Not in drug_words.json: {missing}")

    if not subset:
        print("[!] No matching drugs found")
        return

    print(f"\n[*] Testing LLM on {len(subset)} drugs ...")
    results = _llm_classify_batch(list(subset.keys()), drug_words_data)

    print(f"\n{'=' * 60}")
    print("LLM Test Results")
    print("=" * 60)
    for word in drug_words:
        entry_type = drug_words_data.get(word, {}).get("entry_type", "?")
        illnesses = results.get(word, [])
        print(f"  {word} ({entry_type})")
        if illnesses:
            for ill in illnesses:
                print(f"    -> {ill}")
        else:
            print(f"    -> [no classification]")


# -- CLI ----------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Fill drug_words.json illnesses[] from MED-RT XML + RXNREL.RRF"
    )
    parser.add_argument(
        "--xml", required=True, help="Path to Core_MEDRT_YYYYMMDD_DTS.xml"
    )
    parser.add_argument(
        "--rrf",
        default=None,
        help="Path to folder containing RXNCONSO.RRF and RXNREL.RRF (for brand->ingredient)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reclassify all drugs even if already classified",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="LLM batch size for fallback (default: 25)",
    )
    parser.add_argument(
        "--test",
        nargs="+",
        metavar="DRUG",
        help="Test mode -- look up specific drugs and print results",
    )
    parser.add_argument(
        "--validate",
        nargs="+",
        metavar="DRUG",
        help="Validate mode -- show exactly why specific drugs are/aren't in MED-RT",
    )
    parser.add_argument(
        "--test-llm",
        nargs="+",
        metavar="DRUG",
        help="Test LLM classification on specific drugs without saving",
    )

    args = parser.parse_args()

    if args.test:
        test(args.test, args.xml, args.rrf or "")
    elif args.validate:
        validate(args.validate, args.xml, args.rrf or "")
    elif getattr(args, "test_llm", None):
        test_llm(args.test_llm, args.xml, args.rrf or "")
    else:
        if not args.rrf:
            print("[!] --rrf is required for full build (brand->ingredient lookup)")
            sys.exit(1)
        build(args.xml, args.rrf, force=args.force, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
