"""
Test LLM classification for drugs not found in MED-RT or RxClass API.
Run from C:\Personal\AI\P-RAG directory.
"""
import sys
import json

sys.path.insert(0, '.')

from utility.llm import llm_chat

# Popular drugs that MED-RT missed
test_drugs = [
    "ozempic", "wegovy", "rybelsus", "victoza", "liraglutide",
    "steglatro", "chantix", "varenicline", "brilinta", "ticagrelor",
    "plavix", "clopidogrel", "aimovig", "ajovy", "trikafta",
    "tecfidera", "bafiertam", "saxenda", "paxlovid", "auvelity"
]

drug_list = "\n".join(test_drugs)

messages = [
    {
        "role": "system",
        "content": (
            "You are a medical assistant. "
            "For each drug name, return ONE line:\n"
            "drug_name -> Condition Name 1; Condition Name 2\n\n"
            "Return ONLY the PRIMARY medical condition(s) the drug is "
            "FDA-APPROVED to TREAT. Use standard clinical names.\n"
            "Max 2 conditions. Separate with semicolons.\n"
            "If not a real drug or truly unknown, return: drug_name -> \n\n"
            "Examples:\n"
            "ozempic -> Diabetes Mellitus Type 2; Obesity\n"
            "aimovig -> Migraine Disorders\n"
            "trikafta -> Cystic Fibrosis\n"
            "chantix -> Nicotine Dependence\n"
            "paxlovid -> COVID-19\n"
            "humira -> \n"
            "dexcom -> "
        ),
    },
    {"role": "user", "content": drug_list},
]

print("Testing LLM classification for popular drugs missed by MED-RT...\n")
response = llm_chat(messages=messages, max_tokens=len(test_drugs) * 20)
print("RAW RESPONSE:")
print(response)
print("\nPARSED RESULTS:")
for line in response.strip().split("\n"):
    if "->" in line:
        parts = line.split("->", 1)
        drug = parts[0].strip()
        conditions = [t.strip() for t in parts[1].split(";") if t.strip()]
        print(f"  {drug}: {conditions}")