# import re
# import sys
# import os
# sys.stdout.reconfigure(encoding='utf-8')
# sys.path.insert(0, '.')

# from utility.category import detect_category_rule_based

# queries = [
#     "show me all dialysis related benefits",
#     "are there any benefit for therapeutic injections",
#     "show me all my virtual care benefits",
#     "what are the cost for breast reconstructions",
#     "show me all home health care benefits",
#     "is preventive care covered at no cost?",
#     "allergy testing and treatment cost",
#     "show me cost for immunotherapy",
#     "show me newborn care benefits",
#     "does clinical trials covered for me?",
#     "is mental health and substance abuse covered?",
#     "what are my mental health benefits?",
# ]

# print("Category rule-based detection (None = falls through to LLM):")
# print()
# for q in queries:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     cat = detect_category_rule_based(words, q)
#     status = cat if cat else "NONE -> LLM"
#     print(f"  {status:10} | {q[:55]}")


# import re
# import sys
# sys.stdout.reconfigure(encoding='utf-8')
# sys.path.insert(0, '.')

# from utility.category import detect_category_rule_based

# # All medical queries from golden test
# queries = [
#     "allergy testing and treatment cost",
#     "want to know about blood products",
#     "show me cost for immunotherapy",
#     "I want to know about emergency room service",
#     "my urgent care cost",
#     "are there any benefit for therapeutic injections",
#     "Can you show me transplants cost",
#     "show me all my virtual care benefits",
#     "I want to know about nicotine habit breaking programs cost",
#     "what is my cost for x-ray, lab and imaging",
#     "show me all dialysis related benefits",
#     "what are the $ amount for electronic visits",
#     "show me foot care in an office or clinic visit cost",
#     "show me all home health care benefits",
#     "show me cost for vasectomy",
#     "what are the benefits for skilled nursing facility care",
#     "Do i need to pay any amount for psychological testing",
#     "Want to know about rehabilitation therapy",
#     "what are the cost for breast reconstructions",
#     "show me gender affirming care professional service",
#     "does my plan provide medical food during my hospital stay",
#     "show me newborn care benefits",
#     "show me new born care inpatient care cost",
#     "what is covered under clinical trials and what does it cost",
#     "tell me about emergency room coverage and cost",
#     "what does my plan cover for medical transportation and how much does it cost",
#     "what is prior authorization and how does it affect my benefits",
#     "is Bariatric surgery covered under my plan?",
#     "what is my pcp copay?",
#     "what is my out-of-pocket max?",
#     "how much is my deductible?",
#     "show me my family deductible",
#     "does clinical trials covered for me?",
#     "is mental health and substance abuse covered?",
#     "what is my specialist visit copay?",
#     "is preventive care covered at no cost?",
#     "how much does an ambulance cost?",
#     "what are my mental health benefits?",
# ]

# print("Medical category rule-based detection:")
# print()
# llm_needed = []
# for q in queries:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     cat = detect_category_rule_based(words, q)
#     status = cat if cat else "NONE -> LLM"
#     if not cat:
#         llm_needed.append(q)
#     print(f"  {status:10} | {q[:60]}")

# print(f"\nTotal needing LLM for category: {len(llm_needed)}")
# print("\nMissing words to add:")
# for q in llm_needed:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     # Filter out stopwords
#     stopwords = {'show', 'me', 'cost', 'for', 'what', 'is', 'my', 'the', 'a', 
#                  'an', 'and', 'or', 'i', 'want', 'to', 'know', 'about', 'does',
#                  'can', 'are', 'how', 'much', 'does', 'do', 'will', 'all', 'any',
#                  'have', 'be', 'been', 'get', 'in', 'of', 'at', 'by', 'it',
#                  'plan', 'covered', 'under', 'benefits', 'benefit'}
#     signal_words = [w for w in words if w not in stopwords and len(w) >= 4]
#     print(f"  {q[:50]}")
#     print(f"    signal words: {signal_words}")

# import re
# import sys
# import os
# os.environ["PYTHONIOENCODING"] = "utf-8"
# sys.path.insert(0, '.')

# # Queries with 2 LLM calls from capture output
# # First call was category (now fixed to 0)
# # Remaining calls are topic + sometimes synthesis
# # Let's map each query to its token count and identify topic patterns

# two_call_queries = [
#     ("allergy testing and treatment cost", 777),
#     ("show me cost for immunotherapy", 793),
#     ("are there any benefit for therapeutic injections", 847),
#     ("show me all my virtual care benefits", 833),
#     ("show me all dialysis related benefits", 863),
#     ("what are the $ amount for electronic visits", 851),
#     ("show me foot care in an office or clinic visit cost", 811),
#     ("show me all home health care benefits", 840),
#     ("show me gender affirming care professional service", 834),
#     ("show me newborn care benefits", 841),
#     ("show me new born care inpatient care cost", 851),
#     ("what is covered under clinical trials and what does it cost", 833),
#     ("what does my plan cover for medical transportation and how much does it cost", 840),
#     ("is Bariatric surgery covered under my plan?", 828),
#     ("what is my out-of-pocket max?", 1014),
#     ("does clinical trials covered for me?", 840),
#     ("is preventive care covered at no cost?", 827),
# ]

# three_call_queries = [
#     ("what are the cost for breast reconstructions", 1250),
# ]

# print("2-LLM-call queries — need topic mapping:")
# print()
# stopwords = {
#     'show', 'me', 'cost', 'for', 'what', 'is', 'my', 'the', 'a',
#     'an', 'and', 'or', 'i', 'want', 'to', 'know', 'about', 'does',
#     'can', 'are', 'how', 'much', 'do', 'will', 'all', 'any',
#     'have', 'be', 'been', 'get', 'in', 'of', 'at', 'by', 'it',
#     'plan', 'covered', 'under', 'benefits', 'benefit', 'that',
#     'your', 'there', 'no', 'not', 'if', 'amount', 'amounts',
#     'cover', 'covers', 'coverage',
# }
# for q, tokens in two_call_queries:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     signal_words = [w for w in words if w not in stopwords and len(w) >= 4]
#     print(f"  [{tokens} tokens] {q[:55]}")
#     print(f"    signal: {signal_words}")
#     print()

# print("3-LLM-call queries:")
# for q, tokens in three_call_queries:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     signal_words = [w for w in words if w not in stopwords and len(w) >= 4]
#     print(f"  [{tokens} tokens] {q}")
#     print(f"    signal: {signal_words}")

# import re
# import sys
# import os
# os.environ["PYTHONIOENCODING"] = "utf-8"
# sys.path.insert(0, '.')

# from utility.topic_resolver import resolve_insurance_topic

# # All 150 queries
# all_queries = {
#     "medical": [
#         "allergy testing and treatment cost",
#         "want to know about blood products",
#         "show me cost for immunotherapy",
#         "I want to know about emergency room service",
#         "my urgent care cost",
#         "are there any benefit for therapeutic injections",
#         "Can you show me transplants cost",
#         "show me all my virtual care benefits",
#         "I want to know about nicotine habit breaking programs cost",
#         "what is my cost for x-ray, lab and imaging",
#         "show me all dialysis related benefits",
#         "what are the $ amount for electronic visits",
#         "show me foot care in an office or clinic visit cost",
#         "show me all home health care benefits",
#         "show me cost for vasectomy",
#         "what are the benefits for skilled nursing facility care",
#         "Do i need to pay any amount for psychological testing",
#         "Want to know about rehabilitation therapy",
#         "what are the cost for breast reconstructions",
#         "show me gender affirming care professional service",
#         "does my plan provide medical food during my hospital stay",
#         "show me newborn care benefits",
#         "show me new born care inpatient care cost",
#         "what is covered under clinical trials and what does it cost",
#         "tell me about emergency room coverage and cost",
#         "what does my plan cover for medical transportation and how much does it cost",
#         "what is prior authorization and how does it affect my benefits",
#         "is Bariatric surgery covered under my plan?",
#         "what is my pcp copay?",
#         "what is my out-of-pocket max?",
#         "how much is my deductible?",
#         "show me my family deductible",
#         "does clinical trials covered for me?",
#         "is mental health and substance abuse covered?",
#         "what is my specialist visit copay?",
#         "is preventive care covered at no cost?",
#         "how much does an ambulance cost?",
#         "what are my mental health benefits?",
#     ],
#     "dental": [
#         "What is my general office visit copay for dental?",
#         "How much does a teeth cleaning cost?",
#         "What is the cost for a dental x-ray?",
#         "How much is a dental exam?",
#         "How much are sealants?",
#         "What is the cost for a filling?",
#         "How much does a crown cost?",
#         "What does a root canal cost?",
#         "How much is periodontal scaling and root planing?",
#         "How much does a dental implant cost?",
#         "Is TMJ treatment covered under my dental plan?",
#         "What are my orthodontic benefits?",
#         "What is my coinsurance for a basic dental service?",
#         "What is my calendar year dental deductible?",
#     ],
#     "vision": [
#         "What is the cost for a vision exam?",
#         "What is my out-of-network cost for vision hardware?",
#         "Are contact lenses covered under my vision plan?",
#         "What is NOT covered under vision hardware?",
#         "What happens if I need vision care outside Washington?",
#     ],
# }

# # Track topic usage
# topic_hits = {}
# no_topic_queries = []

# import io
# from contextlib import redirect_stdout

# for category, queries in all_queries.items():
#     for q in queries:
#         words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#         # Suppress print output
#         f = io.StringIO()
#         with redirect_stdout(f):
#             result = resolve_insurance_topic(words, q, p_type=category)
        
#         topics = result.get('topics', [])
#         if topics:
#             for t in topics:
#                 topic_hits[t] = topic_hits.get(t, 0) + 1
#         else:
#             no_topic_queries.append((category, q))

# print("Topics being used (sorted by frequency):")
# print()
# for topic, count in sorted(topic_hits.items(), key=lambda x: -x[1]):
#     print(f"  {count:3}x  {topic}")

# print(f"\nQueries with NO topic resolved ({len(no_topic_queries)}) — go to LLM for topic:")
# for cat, q in no_topic_queries:
#     print(f"  [{cat}] {q[:65]}")

# """
# Calls LLM topic resolver for each unresolved medical query
# and prints what topics LLM returns — so we can hardcode them.
# """
# import re
# import sys
# import os
# import json
# os.environ["PYTHONIOENCODING"] = "utf-8"
# sys.path.insert(0, '.')

# from utility.llm import llm_chat

# unresolved_queries = [
#     "allergy testing and treatment cost",
#     "show me cost for immunotherapy",
#     "are there any benefit for therapeutic injections",
#     "Can you show me transplants cost",
#     "show me all my virtual care benefits",
#     "I want to know about nicotine habit breaking programs cost",
#     "show me all dialysis related benefits",
#     "what are the $ amount for electronic visits",
#     "show me foot care in an office or clinic visit cost",
#     "show me all home health care benefits",
#     "show me cost for vasectomy",
#     "what are the cost for breast reconstructions",
#     "show me gender affirming care professional service",
#     "show me newborn care benefits",
#     "show me new born care inpatient care cost",
#     "what is covered under clinical trials and what does it cost",
#     "what does my plan cover for medical transportation and how much does it cost",
#     "is Bariatric surgery covered under my plan?",
#     "what is my out-of-pocket max?",
#     "does clinical trials covered for me?",
#     "is preventive care covered at no cost?",
# ]

# # Use the same prompt as topic_resolver LLM call
# SYSTEM_PROMPT = """You are a health insurance query classifier.
# Given a member query, return the most relevant insurance topic and keywords.
# Return ONLY valid JSON: {"topics": ["topic1"], "keywords": ["keyword1", "keyword2"]}
# Topics should be short (1-3 words), lowercase, matching insurance benefit categories.
# Examples: "emergency room", "dialysis", "preventive care", "clinical trials",
# "bariatric surgery", "home health care", "newborn care", "breast reconstruction",
# "gender affirming care", "transportation", "vasectomy", "allergy testing",
# "immunotherapy", "therapeutic injections", "virtual care", "electronic visits",
# "foot care", "nicotine programs", "out-of-pocket maximum", "transplants"
# Return ONLY the JSON."""

# print("LLM topic mappings for unresolved medical queries:")
# print()
# results = {}
# for q in unresolved_queries:
#     messages = [
#         {"role": "system", "content": SYSTEM_PROMPT},
#         {"role": "user", "content": q}
#     ]
#     try:
#         response = llm_chat(messages=messages, format="json", max_tokens=100)
#         data = json.loads(response.strip())
#         topics = data.get("topics", [])
#         keywords = data.get("keywords", [])
#         results[q] = {"topics": topics, "keywords": keywords}
#         print(f'  "{q[:55]}"')
#         print(f'    topics={topics}  keywords={keywords}')
#         print()
#     except Exception as e:
#         print(f'  ERROR for "{q[:50]}": {e}')
#         print()

# print("\n# Ready to hardcode in topic_resolver.py:")
# print()
# for q, data in results.items():
#     print(f'# "{q[:55]}"')
#     print(f'#   topics={data["topics"]}  keywords={data["keywords"]}')


# import re
# import sys
# import os
# import io
# from contextlib import redirect_stdout
# os.environ["PYTHONIOENCODING"] = "utf-8"
# sys.path.insert(0, '.')

# from utility.topic_resolver import resolve_insurance_topic

# queries = [
#     "what are the $ amount for electronic visits",
#     "show me cost for vasectomy",
# ]

# for q in queries:
#     words = [re.sub(r'[^\w\s]', '', w) for w in q.lower().split()]
#     f = io.StringIO()
#     with redirect_stdout(f):
#         result = resolve_insurance_topic(words, q, 'medical')
#     print(f'Query: "{q}"')
#     print(f'  topics={result["topics"]}')
#     print(f'  keywords={result["keywords"]}')
#     print()

import sys
sys.path.insert(0, '.')
from utility.condition_resolver import find_canonical_condition

tests = [
    'sleep apnea',
    'apnea',
    'diabetes',
    'high blood pressure',
    'migraine',
    'anxiety',
    'depression',
    'asthma',
    'blood clots',
    'high cholesterol',
]
for t in tests:
    print(f'  {t!r} -> {find_canonical_condition(t)!r}')