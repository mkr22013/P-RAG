import re
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

from utility.category import detect_category_rule_based

queries = [
    "show me all dialysis related benefits",
    "are there any benefit for therapeutic injections",
    "show me all my virtual care benefits",
    "what are the cost for breast reconstructions",
    "show me all home health care benefits",
    "is preventive care covered at no cost?",
    "allergy testing and treatment cost",
    "show me cost for immunotherapy",
    "show me newborn care benefits",
    "does clinical trials covered for me?",
    "is mental health and substance abuse covered?",
    "what are my mental health benefits?",
]

print("Category rule-based detection (None = falls through to LLM):")
print()
for q in queries:
    words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
    cat = detect_category_rule_based(words, q)
    status = cat if cat else "NONE -> LLM"
    print(f"  {status:10} | {q[:55]}")