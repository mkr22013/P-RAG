import sys
sys.path.insert(0, r'C:\Personal\AI\P-RAG')

from utility.condition_resolver import (
    find_canonical_condition,
    get_drugs_for_condition,
    resolve_query_to_drugs,
    _load_drug_illness,
    _load_condition_synonyms,
)

print("=== Testing condition_resolver ===")

# Check canonical condition
canonical = find_canonical_condition("diabetes")
print(f"find_canonical_condition('diabetes') = {repr(canonical)}")

# Check synonyms keys
synonyms = _load_condition_synonyms()
matching_keys = [k for k in synonyms if 'diabetes' in k.lower()]
print(f"Synonyms keys with 'diabetes': {matching_keys}")

# Check drug illness data
drug_data = _load_drug_illness()
metformin = drug_data.get('metformin', [])
print(f"metformin illnesses in cache: {metformin}")

# Check get_drugs_for_condition
if canonical:
    drugs = get_drugs_for_condition(canonical)
    print(f"get_drugs_for_condition('{canonical}') = {len(drugs)} drugs")
    print(f"metformin in results: {'metformin' in drugs}")
    print(f"First 10: {drugs[:10]}")

# Full resolve
print()
print("resolve_query_to_drugs('drugs for diabetes'):")
result = resolve_query_to_drugs("drugs for diabetes", use_llm_fallback=False)
print(f"metformin in result: {'metformin' in result}")
print(f"Total: {len(result)} drugs")