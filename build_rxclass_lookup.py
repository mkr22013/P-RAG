"""
build_rxclass_lookup.py -- Build drug illness mapping directly into drug_words.json.

Three-tier classification:
    Tier 1: MED-RT XML + RXNREL.RRF  (authoritative, local)
    Tier 2: RxClass API               (authoritative, NLM, may_treat only)
    Tier 3: LLM fallback              (last resort, tagged for review)

Each drug entry gets illness_source field:
    "medrt"    -- classified from MED-RT XML or RXNREL
    "rxclass"  -- classified from RxClass API
    "llm"      -- classified by LLM (lowest confidence)

Usage:
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf --no-llm
    python build_rxclass_lookup.py --xml path/to/Core_MEDRT_DTS.xml --rrf path/to/rrf --force
    python build_rxclass_lookup.py --clear --xml path/to/Core_MEDRT_DTS.xml
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse

# -- File paths ----------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRUG_WORDS_FILE = os.path.join(_BASE_DIR, "indices", "drug_words.json")


# -- Load/Save drug_words.json ------------------------------------------------


def load_drug_words() -> dict:
    if not os.path.exists(DRUG_WORDS_FILE):
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


# -- Clear all illness classifications ----------------------------------------


def clear_illnesses() -> None:
    """Clear all illness classifications from drug_words.json."""
    data = load_drug_words()
    cleared = 0
    for word, entry in data.items():
        if isinstance(entry, dict):
            if entry.get("illnesses"):
                entry["illnesses"] = []
                cleared += 1
            if "illness_source" in entry:
                del entry["illness_source"]
    save_drug_words(data)
    print(f"[*] Cleared illnesses from {cleared} drugs")


# -- Parse MED-RT XML ---------------------------------------------------------


def parse_medrt_xml(xml_path: str) -> dict:
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

    for block in content.split("<association>")[1:]:
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

        drug_to_illnesses.setdefault(drug_name, [])
        if disease_name not in drug_to_illnesses[drug_name]:
            drug_to_illnesses[drug_name].append(disease_name)
        may_treat_count += 1

    elapsed = time.time() - t0
    print(
        f" done ({may_treat_count:,} may_treat relations, "
        f"{len(drug_to_illnesses):,} unique drugs, {elapsed:.1f}s)"
    )
    return drug_to_illnesses


# -- Parse RXNREL.RRF ---------------------------------------------------------


def parse_rxnrel_ingredients(rrf_dir: str) -> dict:
    rxnconso_path = os.path.join(rrf_dir, "RXNCONSO.RRF")
    rxnrel_path = os.path.join(rrf_dir, "RXNREL.RRF")

    if not os.path.exists(rxnconso_path) or not os.path.exists(rxnrel_path):
        print(f"[!] RXNCONSO.RRF or RXNREL.RRF not found in: {rrf_dir}")
        return {}

    print(f"[*] Parsing RXNCONSO.RRF ...", end="", flush=True)
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

    print(f"[*] Parsing RXNREL.RRF ...", end="", flush=True)
    t0 = time.time()
    brand_to_ingredients: dict = {}

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
                "has_ingredient",
                "has_tradename",
                "ingredient_of",
                "constitutes",
                "isa",
            ):
                ing_name = rxcui_to_name.get(rxcui2)
                if ing_name:
                    brand_to_ingredients.setdefault(rxcui1, [])
                    if ing_name not in brand_to_ingredients[rxcui1]:
                        brand_to_ingredients[rxcui1].append(ing_name)
                ing_name2 = rxcui_to_name.get(rxcui1)
                if ing_name2:
                    brand_to_ingredients.setdefault(rxcui2, [])
                    if ing_name2 not in brand_to_ingredients[rxcui2]:
                        brand_to_ingredients[rxcui2].append(ing_name2)

    print(f" done ({len(brand_to_ingredients):,} brands, {time.time()-t0:.1f}s)")

    drug_name_to_ingredients: dict = {}
    for drug_name, rxcui in name_to_rxcui.items():
        if rxcui in brand_to_ingredients:
            drug_name_to_ingredients[drug_name] = brand_to_ingredients[rxcui]
            first_word = drug_name.split()[0] if drug_name else ""
            if first_word:
                if first_word not in drug_name_to_ingredients:
                    drug_name_to_ingredients[first_word] = brand_to_ingredients[rxcui]
                else:
                    existing = set(drug_name_to_ingredients[first_word])
                    existing.update(brand_to_ingredients[rxcui])
                    drug_name_to_ingredients[first_word] = list(existing)

    print(f"[*] Brand->ingredient map: {len(drug_name_to_ingredients):,} entries")
    return drug_name_to_ingredients


# -- Tier 1: MED-RT + RXNREL lookup ------------------------------------------


def lookup_illnesses(
    drug_word: str,
    full_names: list,
    medrt: dict,
    brand_to_ingredients: dict,
) -> list:
    seen = set()
    illnesses = []

    def add(names):
        for n in names:
            if n not in seen:
                seen.add(n)
                illnesses.append(n)

    # Direct match
    if drug_word in medrt:
        add(medrt[drug_word])

    # Full name match
    for full_name in full_names:
        full_lower = full_name.lower().strip()
        if full_lower in medrt:
            add(medrt[full_lower])
        words = full_lower.split()
        if len(words) >= 2:
            two = f"{words[0]} {words[1]}"
            if two in medrt:
                add(medrt[two])

    # Prefix match
    if not illnesses:
        prefix = drug_word + " "
        for medrt_name, diseases in medrt.items():
            if medrt_name.startswith(prefix):
                add(diseases)

    # Brand->ingredient hop
    if not illnesses and brand_to_ingredients:
        ingredients = list(brand_to_ingredients.get(drug_word, []))
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
                prefix = ingredient + " "
                for medrt_name, diseases in medrt.items():
                    if medrt_name.startswith(prefix):
                        add(diseases)

    return sorted(illnesses)


# -- Tier 2: RxClass API ------------------------------------------------------


def _get_rxcui(drug_name: str) -> str | None:
    """Get rxcui for a drug name via RxNorm API."""
    try:
        encoded = urllib.parse.quote(drug_name)
        url = f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={encoded}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        rxcuis = data.get("idGroup", {}).get("rxnormId", [])
        return rxcuis[0] if rxcuis else None
    except Exception:
        return None


def _get_rxclass_may_treat(rxcui: str) -> list:
    """Get may_treat conditions for a rxcui via RxClass API."""
    try:
        url = (
            f"https://rxnav.nlm.nih.gov/REST/rxclass/class/byRxcui.json"
            f"?rxcui={rxcui}&relaSource=MEDRT"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        concepts = data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", [])
        return [
            c["rxclassMinConceptItem"]["className"]
            for c in concepts
            if c.get("rela") == "may_treat"
        ]
    except Exception:
        return []


def lookup_rxclass_api(
    drug_word: str,
    full_names: list,
    delay: float = 0.2,
) -> list:
    """
    Tier 2 — RxClass API lookup with may_treat filter.
    Tries drug_word first, then full_names, then ingredient names.
    Returns [] if nothing found.
    """
    candidates = [drug_word] + [fn.lower().split()[0] for fn in full_names if fn]
    candidates = list(dict.fromkeys(candidates))  # deduplicate preserving order

    for candidate in candidates:
        rxcui = _get_rxcui(candidate)
        if rxcui:
            time.sleep(delay)  # rate limit — NLM asks for polite delays
            conditions = _get_rxclass_may_treat(rxcui)
            if conditions:
                return sorted(conditions)
        time.sleep(delay)

    return []


# -- Tier 3: LLM fallback -----------------------------------------------------


def _llm_classify_batch(drug_words_list: list, drug_words_data: dict) -> dict:
    """LLM fallback — split by entry_type for better prompts."""
    sys.path.insert(0, _BASE_DIR)
    from utility.llm import llm_chat

    results = {w: [] for w in drug_words_list}

    vaccines = [
        w
        for w in drug_words_list
        if drug_words_data.get(w, {}).get("entry_type") == "vaccine"
    ]
    drugs = [
        w
        for w in drug_words_list
        if drug_words_data.get(w, {}).get("entry_type") != "vaccine"
    ]

    if vaccines:
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "For each vaccine, return: vaccine_name -> Disease Name\n"
                        "Return the disease it PREVENTS. Max 2. If unknown: vaccine_name -> \n"
                        "Examples:\n"
                        "fluzone -> Influenza\nshingrix -> Herpes Zoster\n"
                        "gardasil -> Human Papillomavirus Infections\n"
                        "prevnar -> Pneumococcal Infections\ncomirnaty -> COVID-19"
                    ),
                },
                {"role": "user", "content": "\n".join(vaccines)},
            ]
            response = llm_chat(messages=messages, max_tokens=len(vaccines) * 20)
            if response:
                for line in response.strip().split("\n"):
                    if "->" not in line:
                        continue
                    parts = line.split("->", 1)
                    word = parts[0].strip().lower()
                    terms = [
                        t.strip()
                        for t in parts[1].split(",")
                        if t.strip() and t.strip() not in ("->", ">", "-")
                    ]
                    for original in vaccines:
                        if original.lower() == word:
                            results[original] = terms[:2]
        except Exception as e:
            print(f"[!] Vaccine LLM batch failed: {e}")

    if drugs:
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "For each drug, return: drug_name -> Condition 1; Condition 2\n"
                        "Return ONLY FDA-approved PRIMARY conditions the drug TREATS.\n"
                        "Use standard clinical names. Max 2. Separate with semicolons.\n"
                        "If not a real drug or unknown: drug_name -> \n\n"
                        "Examples:\n"
                        "ozempic -> Diabetes Mellitus Type 2; Obesity\n"
                        "aimovig -> Migraine Disorders\n"
                        "trikafta -> Cystic Fibrosis\n"
                        "chantix -> Nicotine Dependence\n"
                        "paxlovid -> COVID-19\n"
                        "tecfidera -> Multiple Sclerosis\n"
                        "sprintec -> Contraception\n"
                        "wegovy -> Obesity\n"
                        "dexcom -> \nommnipod -> \nlancets -> "
                    ),
                },
                {"role": "user", "content": "\n".join(drugs)},
            ]
            response = llm_chat(messages=messages, max_tokens=len(drugs) * 25)
            if response:
                for line in response.strip().split("\n"):
                    if "->" not in line:
                        continue
                    parts = line.split("->", 1)
                    word = parts[0].strip().lower()
                    terms_raw = parts[1].strip()
                    if not terms_raw:
                        continue
                    terms = [
                        t.strip()
                        for t in terms_raw.split(";")
                        if t.strip() and t.strip() not in ("->", ">", "-")
                    ]
                    if len(terms) == 1 and "," in terms_raw:
                        terms = [
                            t.strip()
                            for t in terms_raw.split(",")
                            if t.strip() and t.strip() not in ("->", ">", "-")
                        ]
                    for original in drugs:
                        if original.lower() == word:
                            results[original] = terms[:2]
        except Exception as e:
            print(f"[!] Drug LLM batch failed: {e}")

    return results


# -- Main build ---------------------------------------------------------------


def build(
    xml_path: str,
    rrf_dir: str,
    force: bool = False,
    batch_size: int = 25,
    no_llm: bool = False,
    no_rxclass: bool = False,
) -> None:
    """Fill illnesses[] into drug_words.json using three-tier approach."""
    print("=" * 60)
    print("Building Drug -> Illness Mapping (Three-Tier)")
    print("=" * 60)
    print(f"  MED-RT XML:  {xml_path}")
    print(f"  RRF dir:     {rrf_dir}")
    print(f"  Output:      {DRUG_WORDS_FILE}")
    print(f"  RxClass API: {'disabled' if no_rxclass else 'enabled'}")
    print(f"  LLM:         {'disabled (--no-llm)' if no_llm else 'enabled'}")
    print("=" * 60)

    t_start = time.time()
    drug_words_data = load_drug_words()

    # Determine what needs classification
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
        already = sum(
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
            f"[*] {already} already classified, {vitamins} vitamins skipped, "
            f"{len(to_classify)} to classify"
        )

    if not to_classify:
        print("[*] Nothing to classify")
        return

    # -- Tier 1: MED-RT + RXNREL ----------------------------------------------
    print(f"\n{'─'*60}")
    print(f"TIER 1: MED-RT + RXNREL")
    print(f"{'─'*60}")

    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}

    medrt_hits = 0
    tier2_needed = []

    for word in to_classify:
        entry = drug_words_data[word]
        if entry.get("entry_type") == "device":
            continue  # devices never get illness classification
        full_names = entry.get("full_names", [])
        illnesses = lookup_illnesses(word, full_names, medrt, brand_to_ingredients)
        if illnesses:
            drug_words_data[word]["illnesses"] = illnesses
            drug_words_data[word]["illness_source"] = "medrt"
            medrt_hits += 1
        else:
            tier2_needed.append(word)

    # Skip devices for tiers 2 and 3
    device_skipped = [
        w
        for w in tier2_needed
        if drug_words_data.get(w, {}).get("entry_type") == "device"
    ]
    tier2_needed = [
        w
        for w in tier2_needed
        if drug_words_data.get(w, {}).get("entry_type") != "device"
    ]

    print(
        f"[*] Tier 1 result: {medrt_hits} classified, "
        f"{len(tier2_needed)} need further lookup, "
        f"{len(device_skipped)} devices skipped"
    )
    save_drug_words(drug_words_data)

    # -- Tier 2: RxClass API --------------------------------------------------
    tier3_needed = []

    if no_rxclass:
        print(f"\n[*] --no-rxclass: skipping RxClass API")
        tier3_needed = tier2_needed
    else:
        print(f"\n{'─'*60}")
        print(f"TIER 2: RxClass API ({len(tier2_needed)} drugs)")
        print(f"{'─'*60}")
        print(f"[*] Calling NLM RxClass API (polite delay 0.2s between calls)...")

        rxclass_hits = 0
        for i, word in enumerate(tier2_needed):
            entry = drug_words_data[word]
            full_names = entry.get("full_names", [])

            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(tier2_needed)}] {word}")

            conditions = lookup_rxclass_api(word, full_names)
            if conditions:
                drug_words_data[word]["illnesses"] = conditions
                drug_words_data[word]["illness_source"] = "rxclass"
                rxclass_hits += 1
            else:
                tier3_needed.append(word)

        print(
            f"[*] Tier 2 result: {rxclass_hits} classified via RxClass API, "
            f"{len(tier3_needed)} still unclassified"
        )
        save_drug_words(drug_words_data)

    # -- Tier 3: LLM fallback -------------------------------------------------
    if no_llm:
        print(f"\n[*] --no-llm: skipping LLM for {len(tier3_needed)} drugs")
        print(f"[*] Unclassified drugs (direct queries only):")
        for w in sorted(tier3_needed):
            print(f"    - {w}")
    elif tier3_needed:
        print(f"\n{'─'*60}")
        print(f"TIER 3: LLM fallback ({len(tier3_needed)} drugs)")
        print(f"{'─'*60}")
        total_batches = (len(tier3_needed) - 1) // batch_size + 1

        llm_hits = 0
        for i in range(0, len(tier3_needed), batch_size):
            batch = tier3_needed[i : i + batch_size]
            batch_num = i // batch_size + 1
            print(
                f"[*] LLM batch {batch_num}/{total_batches} "
                f"({min(i+batch_size, len(tier3_needed))}/{len(tier3_needed)}) ..."
            )

            results = _llm_classify_batch(batch, drug_words_data)
            for word, illnesses in results.items():
                if word in drug_words_data:
                    # Filter out empty results
                    clean = [ill for ill in illnesses if ill and len(ill) > 3]
                    drug_words_data[word]["illnesses"] = clean
                    drug_words_data[word]["illness_source"] = "llm" if clean else ""
                    if clean:
                        llm_hits += 1

            save_drug_words(drug_words_data)

        print(f"[*] Tier 3 result: {llm_hits} classified via LLM")

    # -- Final stats ----------------------------------------------------------
    elapsed = time.time() - t_start
    by_source = {"medrt": 0, "rxclass": 0, "llm": 0, "none": 0}
    for e in drug_words_data.values():
        if isinstance(e, dict) and e.get("illnesses"):
            src = e.get("illness_source", "none")
            by_source[src] = by_source.get(src, 0) + 1
        elif isinstance(e, dict) and e.get("entry_type") not in (
            "device",
            "vitamin",
            "vaccine",
        ):
            by_source["none"] += 1

    print(f"\n{'='*60}")
    print(f"DONE in {elapsed:.1f}s")
    print(f"  MED-RT + RXNREL:  {by_source['medrt']:4} drugs")
    print(f"  RxClass API:      {by_source['rxclass']:4} drugs")
    print(f"  LLM fallback:     {by_source['llm']:4} drugs")
    print(f"  No classification:{by_source['none']:4} drugs (direct queries only)")
    print(f"{'='*60}")


# -- Test/Validate/CLI --------------------------------------------------------


def test(drug_words_list: list, xml_path: str, rrf_dir: str) -> None:
    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}
    print(f"\n{'='*60}\nTest Lookups\n{'='*60}")
    for word in drug_words_list:
        illnesses = lookup_illnesses(word, [], medrt, brand_to_ingredients)
        in_medrt = word.lower() in medrt
        source = (
            "MED-RT" if in_medrt else ("RXNREL->MED-RT" if illnesses else "NOT FOUND")
        )
        print(f"\n  {word} ({source})")
        for ill in illnesses:
            print(f"    -> {ill}")
        if not illnesses:
            print(f"    -> (not found)")


def validate(drug_words_list: list, xml_path: str, rrf_dir: str) -> None:
    medrt = parse_medrt_xml(xml_path)
    brand_to_ingredients = parse_rxnrel_ingredients(rrf_dir) if rrf_dir else {}
    print(f"\n{'='*60}\nValidation\n{'='*60}")
    for word in drug_words_list:
        print(f"\n  [{word}]")
        if word.lower() in medrt:
            print(f"    FOUND in MED-RT directly")
            for ill in medrt[word.lower()]:
                print(f"      -> {ill}")
            continue
        ingredients = brand_to_ingredients.get(word.lower(), [])
        if ingredients:
            print(f"    FOUND in RXNREL -> ingredients: {ingredients}")
            for ing in ingredients:
                if ing in medrt:
                    for ill in medrt[ing]:
                        print(f"      -> {ill}")
            continue
        prefix_matches = [k for k in medrt if k.startswith(word.lower() + " ")]
        if prefix_matches:
            print(f"    FOUND via prefix: {prefix_matches[:3]}")
            continue
        print(f"    NOT FOUND -> will try RxClass API then LLM")


def test_llm(drug_words_list: list, xml_path: str, rrf_dir: str) -> None:
    drug_words_data = load_drug_words()
    subset = {w: drug_words_data[w] for w in drug_words_list if w in drug_words_data}
    missing = [w for w in drug_words_list if w not in drug_words_data]
    if missing:
        print(f"[!] Not in drug_words.json: {missing}")
    if not subset:
        print("[!] No matching drugs found")
        return
    print(f"\n[*] Testing LLM on {len(subset)} drugs ...")
    results = _llm_classify_batch(list(subset.keys()), drug_words_data)
    print(f"\n{'='*60}\nLLM Test Results\n{'='*60}")
    for word in drug_words_list:
        entry_type = drug_words_data.get(word, {}).get("entry_type", "?")
        illnesses = results.get(word, [])
        print(
            f"  {word} ({entry_type}): {illnesses if illnesses else '[no classification]'}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Fill drug_words.json illnesses[] — Three-Tier: MED-RT → RxClass API → LLM"
    )
    parser.add_argument(
        "--xml", required=False, help="Path to Core_MEDRT_YYYYMMDD_DTS.xml"
    )
    parser.add_argument(
        "--rrf", default=None, help="Path to RRF folder (RXNCONSO.RRF + RXNREL.RRF)"
    )
    parser.add_argument("--force", action="store_true", help="Reclassify all drugs")
    parser.add_argument(
        "--no-llm", action="store_true", help="Skip LLM fallback (Tier 3)"
    )
    parser.add_argument(
        "--no-rxclass", action="store_true", help="Skip RxClass API (Tier 2)"
    )
    parser.add_argument(
        "--clear", action="store_true", help="Clear all illness classifications"
    )
    parser.add_argument(
        "--batch-size", type=int, default=25, help="LLM batch size (default: 25)"
    )
    parser.add_argument(
        "--test",
        nargs="+",
        metavar="DRUG",
        help="Test Tier 1 lookup for specific drugs",
    )
    parser.add_argument(
        "--validate", nargs="+", metavar="DRUG", help="Validate specific drugs"
    )
    parser.add_argument(
        "--test-llm", nargs="+", metavar="DRUG", help="Test LLM on specific drugs"
    )

    args = parser.parse_args()

    if args.clear:
        clear_illnesses()
        return

    if not args.xml:
        parser.error("--xml is required (except for --clear)")

    if args.test:
        test(args.test, args.xml, args.rrf or "")
    elif args.validate:
        validate(args.validate, args.xml, args.rrf or "")
    elif getattr(args, "test_llm", None):
        test_llm(args.test_llm, args.xml, args.rrf or "")
    else:
        if not args.rrf:
            parser.error("--rrf is required for full build")
        build(
            args.xml,
            args.rrf,
            force=args.force,
            batch_size=args.batch_size,
            no_llm=getattr(args, "no_llm", False),
            no_rxclass=getattr(args, "no_rxclass", False),
        )


if __name__ == "__main__":
    main()
