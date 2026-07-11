import json
import re

def normalize(name):
    name = re.sub(r',\s*(Type\s+\d)', r' \1', name)
    return name.strip()

with open('indices/drug_words.json') as f:
    data = json.load(f)

# Check metformin
m = data.get('metformin', {})
raw_illness = m.get('illnesses', [])
normalized = [normalize(i) for i in raw_illness]
print('metformin raw illnesses:', raw_illness)
print('metformin normalized:', normalized)

# Check what condition we query for
query_condition = 'Diabetes Mellitus Type 2'
print()
print('Query condition lower:', query_condition.lower())

# Check if it matches
for norm_ill in normalized:
    match = norm_ill.lower() == query_condition.lower()
    print(f'Does "{norm_ill.lower()}" match "{query_condition.lower()}"? {match}')

# Also check what get_drugs_for_condition actually does
print()
print('--- Simulating get_drugs_for_condition ---')
condition_lower = query_condition.lower()
match_terms = {condition_lower}
print('match_terms:', match_terms)

# Check metformin illness against match_terms
drug_illness_lower = [t.lower() for t in normalized]
print('metformin illness lower (normalized):', drug_illness_lower)
print('any match?', any(term in drug_illness_lower for term in match_terms))