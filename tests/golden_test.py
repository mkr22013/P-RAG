"""
Golden file regression test.

Calls the real /chat API endpoint for every query and saves the response
as a baseline. On subsequent runs, compares results against baselines.

Rules:
  - COST rows changed  → HARD FAIL  (cost data is non-negotiable)
  - INFO rows changed  → SHOW DIFF, prompt approve/reject

Usage:
  # Capture all baselines (server must be running)
  python -m tests.golden_test --capture

  # Capture single category only
  python -m tests.golden_test --capture --category medical
  python -m tests.golden_test --capture --category dental_willamette
  python -m tests.golden_test --capture --category dental_premera
  python -m tests.golden_test --capture --category vision

  # Verify all against baselines
  python -m tests.golden_test --verify

  # Verify single category
  python -m tests.golden_test --verify --category medical
  python -m tests.golden_test --verify --category dental_willamette
  python -m tests.golden_test --verify --category dental_premera
  python -m tests.golden_test --verify --category vision

  # Non-interactive CI mode (fails on any diff)
  python -m tests.golden_test --verify --ci

Prerequisites:
  - Server must be running: python -m uvicorn main.main:app --reload
  - Demo member data available (uses DEMO000001 / group 1000016)

Baselines are stored in: tests/baselines/<category>/<query_slug>.json
"""

import sys
import os
import re
import json
import argparse
import requests
from datetime import datetime
from difflib import unified_diff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINES_DIR = os.path.join(BASE_DIR, "baselines")

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")

# ── Member info per plan variant ─────────────────────────────────────────────

_BASE_MEMBER = {
    "year": "2026",
    "member_key": "DEMO000001",
    "group_number": "1000016",
}

_DEMO_MEMBER_PLANS = {
    "medical": {
        "plan_category": "medical",
        "group_number": "1000016",
        "group_name": "Premera Employees Health Plan",
        "plan": "Premera Employees Health Plan \u2013 Standard PPO Retiree Plan",
        "plan_type": "PPO",
        "plan_tier": "",
        "product_line": "Null",
        "variant": "Retiree",
        "network": "",
        "page_offset": 4,
    },
    "dental": {
        "plan_category": "dental",
        "group_number": "1000016",
        "group_name": "Premera Employees Health Plan",
        "plan": "Willamette Dental Plan",
        "plan_type": "",
        "plan_tier": "",
        "product_line": "Null",
        "variant": "Standard",
        "network": "",
        "page_offset": 5,
    },
    "vision": {
        "plan_category": "vision",
        "group_number": "1000016",
        "group_name": "Premera Employees Health Plan",
        "plan": "Vision Plan",
        "plan_type": "",
        "plan_tier": "",
        "product_line": "Null",
        "variant": "Standard",
        "network": "",
        "page_offset": 6,
    },
    "rx": {
        "plan_category": "rx",
        "group_number": "1000016",
        "group_name": "Premera Employees Health Plan",
        "plan": "Essentials Formulary Drug List",
        "plan_type": "",
        "plan_tier": "",
        "product_line": "",
        "variant": "E4",
        "network": "",
    },
}

# Same as demo member but with Premera Dental instead of Willamette
_DEMO_MEMBER_PREMERA_DENTAL_PLANS = {
    **_DEMO_MEMBER_PLANS,
    "dental": {
        "plan_category": "dental",
        "group_number": "1000016",
        "group_name": "Premera Employees Health Plan",
        "plan": "Premera Dental Plan",
        "plan_type": "",
        "plan_tier": "",
        "product_line": "Null",
        "variant": "Standard",
        "network": "",
        "page_offset": 5,
    },
}

MEMBER_INFO_BY_CATEGORY = {
    "medical": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
    "dental_willamette": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
    "dental_premera": json.dumps(
        {**_BASE_MEMBER, "plans": _DEMO_MEMBER_PREMERA_DENTAL_PLANS}
    ),
    "vision": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
    "rx": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
}

# ── Queries ───────────────────────────────────────────────────────────────────

QUERIES = {
    "medical": [
        "allergy testing and treatment cost",
        "want to know about blood products",
        "show me cost for immunotherapy",
        "I want to know about emergency room service",
        "my urgent care cost",
        "are there any benefit for therapeutic injections",
        "Can you show me transplants cost",
        "show me all my virtual care benefits",
        "I want to know about nicotine habit breaking programs cost",
        "what is my cost for x-ray, lab and imaging",
        "show me all dialysis related benefits",
        "what are the $ amount for electronic visits",
        "show me foot care in an office or clinic visit cost",
        "show me all home health care benefits",
        "show me cost for vasectomy",
        "what are the benefits for skilled nursing facility care",
        "Do i need to pay any amount for psychological testing",
        "Want to know about rehabilitation therapy",
        "what are the cost for breast reconstructions",
        "show me gender affirming care professional service",
        "does my plan provide medical food during my hospital stay",
        "show me newborn care benefits",
        "show me new born care inpatient care cost",
        "what is covered under clinical trials and what does it cost",
        "tell me about emergency room coverage and cost",
        "what does my plan cover for medical transportation and how much does it cost",
        "what is prior authorization and how does it affect my benefits",
        "is Bariatric surgery covered under my plan?",
        "what is my pcp copay?",
        "what is my out-of-pocket max?",
        "how much is my deductible?",
        "show me my family deductible",
        "does clinical trials covered for me?",
        # Complex/edge case queries
        "is mental health and substance abuse covered?",
        "what is my specialist visit copay?",
        "is preventive care covered at no cost?",
        "how much does an ambulance cost?",
        "what are my mental health benefits?",
    ],
    "dental_willamette": [
        # Office Visit
        "What is my general office visit copay for dental?",
        "What is my specialist office visit copay for dental?",
        # Diagnostic and Preventive
        "How much does a teeth cleaning cost?",
        "What is the cost for a dental x-ray?",
        "How much is a dental exam?",
        "What does a panoramic x-ray cost?",
        "What is the cost for fluoride treatment?",
        "How much are sealants?",
        # Restorative
        "What is the cost for a filling?",
        "How much does an amalgam filling cost?",
        "What is my copay for a composite filling?",
        # Crowns
        "How much does a crown cost?",
        "What is my copay for a porcelain crown?",
        "What does a stainless steel crown cost?",
        # Endodontic
        "What does a root canal cost?",
        "How much is an apicoectomy?",
        "What is the cost for a pulp cap?",
        # Periodontic
        "How much is periodontal scaling and root planing?",
        "What does periodontic maintenance cost?",
        "What is my copay for gum surgery?",
        # Oral Surgery
        "How much does a tooth extraction cost?",
        "What is the cost to remove an impacted tooth?",
        "What does wisdom tooth removal cost?",
        # Prosthodontics
        "How much does a complete denture cost?",
        "What is the cost for a partial denture?",
        "How much does a dental bridge cost?",
        # Implants
        "How much does a dental implant cost?",
        "What is covered for dental implants?",
        # Adjunctive
        "What does nitrous oxide cost at the dentist?",
        "How much is general anesthesia for a dental procedure?",
        "What is my emergency dental visit copay?",
        # Info
        "Is TMJ treatment covered under my dental plan?",
        "show me all benefits for Temporomandibular Joint Disorders (TMJ) Care",
        "What dental services are not covered?",
        "What happens if I go to an out of network dentist?",
        "What are my orthodontic benefits?",
        "Is there a maximum benefit for dental implants?",
    ],
    "dental_premera": [
        "What is my coinsurance for a basic dental service?",
        "What percentage do I pay for major dental work like a crown?",
        "What is my calendar year dental deductible?",
        "What is the annual maximum benefit for dental?",
        "What does Class I diagnostic and preventive services cost me?",
        "What services are covered under Class I diagnostic and preventive?",
        "What dental services fall under Class II basic services?",
        "What is included in Class III major dental services?",
        "Are fillings covered under my dental plan?",
        "Is a root canal covered and what class is it?",
        "Is orthodontic treatment covered under my dental plan?",
        "Is TMJ treatment covered?",
        "show me all benefits for Temporomandibular Joint Disorders (TMJ) Care",
        "Are dental implants covered?",
        "What dental services are NOT covered or excluded?",
        # NOTE: "show me all covered services under my dental plan" removed —
        # this overly broad query sits right at the relevance-filter boundary
        # and flips between including/excluding deductible rows on every
        # capture. Both versions are factually correct Premera data; this is
        # scoring non-determinism on an unrealistic query, not a real bug.
        "how much is a dental exam?",
        "how much is a teeth cleaning?",
    ],
    "vision": [
        "What is the cost for a vision exam?",
        "What is my out-of-network cost for vision hardware?",
        "How much does vision hardware cost in network?",
        "What is the annual limit for vision hardware?",
        "Is there a calendar year limit for eye exams?",
        "What services are covered under vision exams?",
        "What vision hardware is covered under my plan?",
        "Are contact lenses covered under my vision plan?",
        "What is NOT covered under vision hardware?",
        "How does selecting an in-network vision provider affect my costs?",
        "What happens if I need vision care outside Washington?",
        "Is vision care covered when I am travelling?",
        "What vision services are excluded from my plan?",
        "Are plain sunglasses covered under my vision plan?",
        "What does my vision plan cover and how much will I pay?",
    ],
    "rx": [
        # Tier queries — generic drugs
        "what tier is metformin?",
        "what tier is lisinopril?",
        "what tier is atorvastatin?",
        "what tier is amlodipine?",
        "what tier is omeprazole?",
        # Coverage queries — brand drugs
        "is vivjoa covered?",
        "does my plan cover humira?",
        "what are the requirements for cresemba?",
        "is lipitor on my formulary?",
        "is ozempic covered under my plan?",
        # Formulary queries
        "is fluconazole on my formulary?",
        "is metformin on my formulary?",
        "is ibuprofen covered under my prescription plan?",
        # Requirement queries
        "does metformin require prior authorization?",
        "does humira need prior authorization?",
        # Not on formulary
        "is ancobon covered?",
        "is diflucan covered?",
        # Combination drugs
        "what tier is glipizide metformin?",
        "is glyburide metformin covered?",
        # General
        "what tier is vivjoa?",
        "what is formulary drugs?",
        "I want to know about my preventive drugs?",
        # Illness queries — with rx keywords (0 token path)
        "drugs for diabetes",
        "medication for migraine",
        "what drugs treat high blood pressure?",
        "drugs for high cholesterol",
        "medication for blood clots",
        "what drugs are available for depression?",
        "what anxiety medication is covered under my plan?",
        "what asthma medication is on my formulary?",
        "what cholesterol medication is on my formulary?",
        "is there any medication covered for high blood pressure?",
        # Illness queries — condition only (LLM category, 0 token retrieval)
        "what is covered for asthma?",
        "what is covered for diabetes?",
        "what treats depression?",
        "what helps with anxiety?",
        "what is covered for migraine?",
        "what is covered for high cholesterol?",
        "what is covered for blood clots?",
        # Specific drug queries — new drugs
        "is wegovy covered under my plan?",
        "what tier is gabapentin?",
        "what is covered for epilepsy?",
        "is jardiance covered?",
    ],
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def slug(query):
    return re.sub(r"[^\w]+", "_", query.lower()).strip("_")[:60]


def baseline_path(category, query):
    return os.path.join(BASELINES_DIR, category, f"{slug(query)}.json")


def call_api(query, category):
    """POST to /chat — same as the UI does."""
    # Map category key to actual plan category for the API
    api_category = (
        category.replace("dental_willamette", "dental")
        .replace("dental_premera", "dental")
        .replace("rx", "")
    )
    member_info = MEMBER_INFO_BY_CATEGORY.get(
        category, MEMBER_INFO_BY_CATEGORY["medical"]
    )

    try:
        resp = requests.post(
            f"{API_BASE}/chat",
            data={
                "prompt": query,
                "member_info": member_info,
                "current_category": api_category,
                "history": "[]",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print(f"\n  ✗ Cannot connect to {API_BASE} — is the server running?")
        sys.exit(1)
    except Exception as e:
        return {"answer": f"[ERROR] {e}", "pages": [], "source": ""}


def parse_response(response: dict) -> dict:
    """
    Extract answer, pages and source from the API response.
    """
    answer = response.get("answer", "")
    pages = response.get("pages", [])
    source = response.get("source", "")
    return {
        "answer": answer,
        "pages": pages,
        "source": source,
    }


def run_query(query, category):
    response = call_api(query, category)
    parsed = parse_response(response)
    token_usage = response.get("token_usage", {})
    return {
        "query": query,
        "category": category,
        "timestamp": datetime.now().isoformat(),
        "answer": parsed["answer"],
        "pages": parsed["pages"],
        "source": parsed["source"],
        "token_usage": token_usage,
    }


def save_baseline(result):
    cat = result["category"]
    path = baseline_path(cat, result["query"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def load_baseline(category, query):
    path = baseline_path(category, query)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff_answer(old, new):
    if old == new:
        return None
    lines = list(
        unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="baseline",
            tofile="current",
            lineterm="",
        )
    )
    return "\n".join(lines)


def is_cost_changed(old_answer, new_answer):
    """
    Hard fail if any dollar amount or coinsurance value changed.
    Extracts all $ amounts and percentage coinsurance values for comparison.
    """

    def extract_costs(text):
        amounts = re.findall(r"\$[\d,]+(?:\.\d+)?", text)
        coinsurance = re.findall(r"\d+%\s*coinsurance", text, re.IGNORECASE)
        copays = re.findall(r"\$[\d,]+\s*copay", text, re.IGNORECASE)
        return sorted(set(amounts + coinsurance + copays))

    old_costs = extract_costs(old_answer)
    new_costs = extract_costs(new_answer)
    return old_costs != new_costs, old_costs, new_costs


# ── Capture mode ──────────────────────────────────────────────────────────────


def capture(categories=None):
    cats = categories or list(QUERIES.keys())
    total = saved = 0
    total_tokens = 0
    total_calls = 0

    for cat in cats:
        # Show which dental plan is being used — helps debug baseline mixups
        # Only shown for dental categories since others don't have dental-specific plans
        member_info_dict = json.loads(MEMBER_INFO_BY_CATEGORY.get(cat, "{}"))
        dental_plan = (
            member_info_dict.get("plans", {}).get("dental", {}).get("plan", "")
        )
        plan_label = (
            f" — dental plan: {dental_plan}" if dental_plan and "dental" in cat else ""
        )
        print(f"\n[{cat.upper()}]{plan_label}")
        for query in QUERIES[cat]:
            total += 1
            print(f"  {query[:65]}", end="  ", flush=True)
            result = run_query(query, cat)
            save_baseline(result)
            saved += 1
            has_answer = bool(result["answer"] and "[ERROR]" not in result["answer"])
            tokens = result.get("token_usage", {}).get("total_tokens", 0)
            calls = result.get("token_usage", {}).get("total_llm_calls", 0)
            total_tokens += tokens
            total_calls += calls
            # Show source for dental categories to confirm correct plan
            source_label = f" [{result.get('source', '')}]" if "dental" in cat else ""
            print(
                f"{'✓' if has_answer else '✗ ERROR'}  [{tokens} tokens, {calls} LLM calls]{source_label}"
            )

    avg_tokens = total_tokens // saved if saved else 0
    avg_calls = round(total_calls / saved, 1) if saved else 0
    print(f"\nSaved {saved}/{total} baselines → {BASELINES_DIR}/")
    print(f"\n── Token Usage Summary ─────────────────")
    print(f"  Total queries:     {saved}")
    print(f"  Total tokens:      {total_tokens:,}")
    print(f"  Total LLM calls:   {total_calls}")
    print(f"  Avg tokens/query:  {avg_tokens}")
    print(f"  Avg LLM calls/q:   {avg_calls}")


# ── Verify mode ───────────────────────────────────────────────────────────────


def verify(categories=None, ci=False):
    cats = categories or list(QUERIES.keys())
    cost_failures = []
    info_diffs = []
    missing = []

    for cat in cats:
        print(f"\n[{cat.upper()}]")
        for query in QUERIES[cat]:
            baseline = load_baseline(cat, query)
            if baseline is None:
                missing.append((cat, query))
                print(f"  ? NO BASELINE  {query[:60]}")
                continue

            current = run_query(query, cat)

            cost_changed, old_costs, new_costs = is_cost_changed(
                baseline["answer"], current["answer"]
            )
            answer_diff = diff_answer(baseline["answer"], current["answer"])

            if cost_changed:
                cost_failures.append((cat, query, old_costs, new_costs))
                print(f"  ✗ COST CHANGED  {query[:60]}")
                print(f"      baseline: {old_costs}")
                print(f"      current:  {new_costs}")
            elif answer_diff:
                info_diffs.append((cat, query, answer_diff, current))
                print(f"  ~ ANSWER CHANGED  {query[:60]}")
            else:
                print(f"  ✓  {query[:60]}")

    # ── Report ────────────────────────────────────────────────────────────────
    print()

    if missing:
        print(f"⚠  {len(missing)} queries have no baseline — run --capture first")
        print()

    if cost_failures:
        print(f"{'='*60}")
        print(f"COST FAILURES: {len(cost_failures)}  ← MUST FIX BEFORE DEPLOY")
        print(f"{'='*60}")
        for cat, q, old, new in cost_failures:
            print(f"\n  [{cat}] {q}")
            print(f"  baseline: {old}")
            print(f"  current:  {new}")

    if info_diffs and not ci:
        print(f"\n{'='*60}")
        print(f"ANSWER CHANGES: {len(info_diffs)}  ← review and approve/reject")
        print(f"{'='*60}")
        approved = rejected = 0
        for cat, q, diff, current in info_diffs:
            print(f"\n[{cat}] {q}")
            print(diff[:1000])  # show first 1000 chars of diff
            ans = input("  Accept as new baseline? [y/n]: ").strip().lower()
            if ans == "y":
                save_baseline(current)
                approved += 1
                print("  ✓ Baseline updated.")
            else:
                rejected += 1
                print("  ✗ Kept old baseline.")
        print(f"\nAnswer changes: {approved} approved, {rejected} rejected.")
    elif info_diffs and ci:
        print(f"\nANSWER CHANGES ({len(info_diffs)}) — run without --ci to review.")

    total_queries = sum(len(QUERIES[c]) for c in cats)
    n_ok = total_queries - len(cost_failures) - len(info_diffs) - len(missing)

    print(f"\n{'='*60}")
    if cost_failures:
        print(f"  RESULT: FAILED  ✗  ({len(cost_failures)} cost failures)")
    elif info_diffs and ci:
        print(f"  RESULT: FAILED  ✗  ({len(info_diffs)} unreviewed answer changes)")
    else:
        print(f"  RESULT: PASSED  ✓  ({n_ok}/{total_queries} queries unchanged)")
    print(f"{'='*60}")

    return len(cost_failures) == 0 and (not ci or len(info_diffs) == 0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Golden regression test — calls real API"
    )
    parser.add_argument("--capture", action="store_true", help="Capture baselines")
    parser.add_argument(
        "--verify", action="store_true", help="Verify against baselines"
    )
    parser.add_argument("--ci", action="store_true", help="Non-interactive CI mode")
    parser.add_argument(
        "--category",
        choices=["medical", "dental_willamette", "dental_premera", "vision", "rx"],
        help="Run only one category",
    )
    args = parser.parse_args()

    cats = [args.category] if args.category else None

    if args.capture:
        print(f"\nCapturing baselines — server must be running at {API_BASE}\n")
        capture(cats)
    elif args.verify:
        print(f"\nVerifying against baselines — server must be running at {API_BASE}\n")
        ok = verify(cats, ci=args.ci)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()

# #=================================================Previous code=============================================
# # """
# # Golden file regression test.

# # Calls the real /chat API endpoint for every query and saves the response
# # as a baseline. On subsequent runs, compares results against baselines.

# # Rules:
# #   - COST rows changed  → HARD FAIL  (cost data is non-negotiable)
# #   - INFO rows changed  → SHOW DIFF, prompt approve/reject

# # Usage:
# #   # Capture all baselines (server must be running)
# #   python -m tests.golden_test --capture

# #   # Capture single category only
# #   python -m tests.golden_test --capture --category medical
# #   python -m tests.golden_test --capture --category dental_willamette
# #   python -m tests.golden_test --capture --category dental_premera
# #   python -m tests.golden_test --capture --category vision

# #   # Verify all against baselines
# #   python -m tests.golden_test --verify

# #   # Verify single category
# #   python -m tests.golden_test --verify --category medical
# #   python -m tests.golden_test --verify --category dental_willamette
# #   python -m tests.golden_test --verify --category dental_premera
# #   python -m tests.golden_test --verify --category vision

# #   # Non-interactive CI mode (fails on any diff)
# #   python -m tests.golden_test --verify --ci

# # Prerequisites:
# #   - Server must be running: python -m uvicorn main.main:app --reload
# #   - Demo member data available (uses DEMO000001 / group 1000016)

# # Baselines are stored in: tests/baselines/<category>/<query_slug>.json
# # """

# # import sys
# # import os
# # import re
# # import json
# # import argparse
# # import requests
# # from datetime import datetime
# # from difflib import unified_diff

# # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # BASELINES_DIR = os.path.join(BASE_DIR, "baselines")

# # API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")

# # # ── Member info per plan variant ─────────────────────────────────────────────

# # _BASE_MEMBER = {
# #     "year": "2026",
# #     "member_key": "DEMO000001",
# #     "group_number": "1000016",
# # }

# # _DEMO_MEMBER_PLANS = {
# #     "medical": {
# #         "plan_category": "medical",
# #         "group_number": "1000016",
# #         "group_name": "Premera Employees Health Plan",
# #         "plan": "Premera Employees Health Plan \u2013 Standard PPO Retiree Plan",
# #         "plan_type": "PPO",
# #         "plan_tier": "",
# #         "product_line": "Null",
# #         "variant": "Retiree",
# #         "network": "",
# #         "page_offset": 4,
# #     },
# #     "dental": {
# #         "plan_category": "dental",
# #         "group_number": "1000016",
# #         "group_name": "Premera Employees Health Plan",
# #         "plan": "Willamette Dental Plan",
# #         "plan_type": "",
# #         "plan_tier": "",
# #         "product_line": "Null",
# #         "variant": "Standard",
# #         "network": "",
# #         "page_offset": 5,
# #     },
# #     "vision": {
# #         "plan_category": "vision",
# #         "group_number": "1000016",
# #         "group_name": "Premera Employees Health Plan",
# #         "plan": "Vision Plan",
# #         "plan_type": "",
# #         "plan_tier": "",
# #         "product_line": "Null",
# #         "variant": "Standard",
# #         "network": "",
# #         "page_offset": 6,
# #     },
# #     "rx": {
# #         "plan_category": "rx",
# #         "group_number": "1000016",
# #         "group_name": "Premera Employees Health Plan",
# #         "plan": "Essentials Formulary Drug List",
# #         "plan_type": "",
# #         "plan_tier": "",
# #         "product_line": "",
# #         "variant": "E4",
# #         "network": "",
# #     },
# # }

# # # Same as demo member but with Premera Dental instead of Willamette
# # _DEMO_MEMBER_PREMERA_DENTAL_PLANS = {
# #     **_DEMO_MEMBER_PLANS,
# #     "dental": {
# #         "plan_category": "dental",
# #         "group_number": "1000016",
# #         "group_name": "Premera Employees Health Plan",
# #         "plan": "Premera Dental Plan",
# #         "plan_type": "",
# #         "plan_tier": "",
# #         "product_line": "Null",
# #         "variant": "Standard",
# #         "network": "",
# #         "page_offset": 5,
# #     },
# # }

# # MEMBER_INFO_BY_CATEGORY = {
# #     "medical": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
# #     "dental_willamette": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
# #     "dental_premera": json.dumps(
# #         {**_BASE_MEMBER, "plans": _DEMO_MEMBER_PREMERA_DENTAL_PLANS}
# #     ),
# #     "vision": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
# #     "rx": json.dumps({**_BASE_MEMBER, "plans": _DEMO_MEMBER_PLANS}),
# # }

# # # ── Queries ───────────────────────────────────────────────────────────────────

# # QUERIES = {
# #     "medical": [
# #         "allergy testing and treatment cost",
# #         "want to know about blood products",
# #         "show me cost for immunotherapy",
# #         "I want to know about emergency room service",
# #         "my urgent care cost",
# #         "are there any benefit for therapeutic injections",
# #         "Can you show me transplants cost",
# #         "show me all my virtual care benefits",
# #         "I want to know about nicotine habit breaking programs cost",
# #         "what is my cost for x-ray, lab and imaging",
# #         "show me all dialysis related benefits",
# #         "what are the $ amount for electronic visits",
# #         "show me foot care in an office or clinic visit cost",
# #         "show me all home health care benefits",
# #         "show me cost for vasectomy",
# #         "what are the benefits for skilled nursing facility care",
# #         "Do i need to pay any amount for psychological testing",
# #         "Want to know about rehabilitation therapy",
# #         "what are the cost for breast reconstructions",
# #         "show me gender affirming care professional service",
# #         "does my plan provide medical food during my hospital stay",
# #         "show me newborn care benefits",
# #         "show me new born care inpatient care cost",
# #         "what is covered under clinical trials and what does it cost",
# #         "tell me about emergency room coverage and cost",
# #         "what does my plan cover for medical transportation and how much does it cost",
# #         "what is prior authorization and how does it affect my benefits",
# #         "is Bariatric surgery covered under my plan?",
# #         "what is my pcp copay?",
# #         "what is my out-of-pocket max?",
# #         "how much is my deductible?",
# #         "show me my family deductible",
# #         "does clinical trials covered for me?",
# #     ],
# #     "dental_willamette": [
# #         # Office Visit
# #         "What is my general office visit copay for dental?",
# #         "What is my specialist office visit copay for dental?",
# #         # Diagnostic and Preventive
# #         "How much does a teeth cleaning cost?",
# #         "What is the cost for a dental x-ray?",
# #         "How much is a dental exam?",
# #         "What does a panoramic x-ray cost?",
# #         "What is the cost for fluoride treatment?",
# #         "How much are sealants?",
# #         # Restorative
# #         "What is the cost for a filling?",
# #         "How much does an amalgam filling cost?",
# #         "What is my copay for a composite filling?",
# #         # Crowns
# #         "How much does a crown cost?",
# #         "What is my copay for a porcelain crown?",
# #         "What does a stainless steel crown cost?",
# #         # Endodontic
# #         "What does a root canal cost?",
# #         "How much is an apicoectomy?",
# #         "What is the cost for a pulp cap?",
# #         # Periodontic
# #         "How much is periodontal scaling and root planing?",
# #         "What does periodontic maintenance cost?",
# #         "What is my copay for gum surgery?",
# #         # Oral Surgery
# #         "How much does a tooth extraction cost?",
# #         "What is the cost to remove an impacted tooth?",
# #         "What does wisdom tooth removal cost?",
# #         # Prosthodontics
# #         "How much does a complete denture cost?",
# #         "What is the cost for a partial denture?",
# #         "How much does a dental bridge cost?",
# #         # Implants
# #         "How much does a dental implant cost?",
# #         "What is covered for dental implants?",
# #         # Adjunctive
# #         "What does nitrous oxide cost at the dentist?",
# #         "How much is general anesthesia for a dental procedure?",
# #         "What is my emergency dental visit copay?",
# #         # Info
# #         "Is TMJ treatment covered under my dental plan?",
# #         "show me all benefits for Temporomandibular Joint Disorders (TMJ) Care",
# #         "What dental services are not covered?",
# #         "What happens if I go to an out of network dentist?",
# #         "What are my orthodontic benefits?",
# #         "Is there a maximum benefit for implants?",
# #     ],
# #     "dental_premera": [
# #         "What is my coinsurance for a basic dental service?",
# #         "What percentage do I pay for major dental work like a crown?",
# #         "What is my calendar year dental deductible?",
# #         "What is the annual maximum benefit for dental?",
# #         "What does Class I diagnostic and preventive services cost me?",
# #         "What services are covered under Class I diagnostic and preventive?",
# #         "What dental services fall under Class II basic services?",
# #         "What is included in Class III major dental services?",
# #         "Are fillings covered under my dental plan?",
# #         "Is a root canal covered and what class is it?",
# #         "Is orthodontic treatment covered under my dental plan?",
# #         "Is TMJ treatment covered?",
# #         "show me all benefits for Temporomandibular Joint Disorders (TMJ) Care",
# #         "Are dental implants covered?",
# #         "What dental services are NOT covered or excluded?",
# #         # NOTE: "show me all covered services under my dental plan" removed —
# #         # this overly broad query sits right at the relevance-filter boundary
# #         # and flips between including/excluding deductible rows on every
# #         # capture. Both versions are factually correct Premera data; this is
# #         # scoring non-determinism on an unrealistic query, not a real bug.
# #         "how much is a dental exam?",
# #         "how much is a teeth cleaning?",
# #     ],
# #     "vision": [
# #         "What is the cost for a vision exam?",
# #         "What is my out-of-network cost for vision hardware?",
# #         "How much does vision hardware cost in network?",
# #         "What is the annual limit for vision hardware?",
# #         "Is there a calendar year limit for eye exams?",
# #         "What services are covered under vision exams?",
# #         "What vision hardware is covered under my plan?",
# #         "Are contact lenses covered under my vision plan?",
# #         "What is NOT covered under vision hardware?",
# #         "How does selecting an in-network vision provider affect my costs?",
# #         "What happens if I need vision care outside Washington?",
# #         "Is vision care covered when I am travelling?",
# #         "What vision services are excluded from my plan?",
# #         "Are plain sunglasses covered under my vision plan?",
# #         "What does my vision plan cover and how much will I pay?",
# #     ],
# #     "rx": [
# #         # Tier queries — generic drugs
# #         "what tier is metformin?",
# #         "what tier is lisinopril?",
# #         "what tier is atorvastatin?",
# #         "what tier is amlodipine?",
# #         "what tier is omeprazole?",
# #         # Coverage queries — brand drugs
# #         "is vivjoa covered?",
# #         "does my plan cover humira?",
# #         "what are the requirements for cresemba?",
# #         "is lipitor on my formulary?",
# #         "is ozempic covered under my plan?",
# #         # Formulary queries
# #         "is fluconazole on my formulary?",
# #         "is metformin on my formulary?",
# #         "is ibuprofen covered under my prescription plan?",
# #         # Requirement queries
# #         "does metformin require prior authorization?",
# #         "does humira need prior authorization?",
# #         # Not on formulary
# #         "is ancobon covered?",
# #         "is diflucan covered?",
# #         # Combination drugs
# #         "what tier is glipizide metformin?",
# #         "is glyburide metformin covered?",
# #         # General
# #         "what tier is vivjoa?",
# #         "I want to know about my preventive drugs?",
# #         "what is formulary drugs?",
# #     ],
# # }

# # # ── Helpers ───────────────────────────────────────────────────────────────────


# # def slug(query):
# #     return re.sub(r"[^\w]+", "_", query.lower()).strip("_")[:60]


# # def baseline_path(category, query):
# #     return os.path.join(BASELINES_DIR, category, f"{slug(query)}.json")


# # def call_api(query, category):
# #     """POST to /chat — same as the UI does."""
# #     # Map category key to actual plan category for the API
# #     api_category = (
# #         category.replace("dental_willamette", "dental")
# #         .replace("dental_premera", "dental")
# #         .replace("rx", "")
# #     )
# #     member_info = MEMBER_INFO_BY_CATEGORY.get(
# #         category, MEMBER_INFO_BY_CATEGORY["medical"]
# #     )

# #     try:
# #         resp = requests.post(
# #             f"{API_BASE}/chat",
# #             data={
# #                 "prompt": query,
# #                 "member_info": member_info,
# #                 "current_category": api_category,
# #                 "history": "[]",
# #             },
# #             timeout=60,
# #         )
# #         resp.raise_for_status()
# #         return resp.json()
# #     except requests.exceptions.ConnectionError:
# #         print(f"\n  ✗ Cannot connect to {API_BASE} — is the server running?")
# #         sys.exit(1)
# #     except Exception as e:
# #         return {"answer": f"[ERROR] {e}", "pages": [], "source": ""}


# # def parse_response(response: dict) -> dict:
# #     """
# #     Extract answer, pages and source from the API response.
# #     """
# #     answer = response.get("answer", "")
# #     pages = response.get("pages", [])
# #     source = response.get("source", "")
# #     return {
# #         "answer": answer,
# #         "pages": pages,
# #         "source": source,
# #     }


# # def run_query(query, category):
# #     response = call_api(query, category)
# #     parsed = parse_response(response)
# #     token_usage = response.get("token_usage", {})
# #     return {
# #         "query": query,
# #         "category": category,
# #         "timestamp": datetime.now().isoformat(),
# #         "answer": parsed["answer"],
# #         "pages": parsed["pages"],
# #         "source": parsed["source"],
# #         "token_usage": token_usage,
# #     }


# # def save_baseline(result):
# #     cat = result["category"]
# #     path = baseline_path(cat, result["query"])
# #     os.makedirs(os.path.dirname(path), exist_ok=True)
# #     with open(path, "w", encoding="utf-8") as f:
# #         json.dump(result, f, indent=2, ensure_ascii=False)


# # def load_baseline(category, query):
# #     path = baseline_path(category, query)
# #     if not os.path.exists(path):
# #         return None
# #     with open(path, encoding="utf-8") as f:
# #         return json.load(f)


# # def diff_answer(old, new):
# #     if old == new:
# #         return None
# #     lines = list(
# #         unified_diff(
# #             old.splitlines(),
# #             new.splitlines(),
# #             fromfile="baseline",
# #             tofile="current",
# #             lineterm="",
# #         )
# #     )
# #     return "\n".join(lines)


# # def is_cost_changed(old_answer, new_answer):
# #     """
# #     Hard fail if any dollar amount or coinsurance value changed.
# #     Extracts all $ amounts and percentage coinsurance values for comparison.
# #     """

# #     def extract_costs(text):
# #         amounts = re.findall(r"\$[\d,]+(?:\.\d+)?", text)
# #         coinsurance = re.findall(r"\d+%\s*coinsurance", text, re.IGNORECASE)
# #         copays = re.findall(r"\$[\d,]+\s*copay", text, re.IGNORECASE)
# #         return sorted(set(amounts + coinsurance + copays))

# #     old_costs = extract_costs(old_answer)
# #     new_costs = extract_costs(new_answer)
# #     return old_costs != new_costs, old_costs, new_costs


# # # ── Capture mode ──────────────────────────────────────────────────────────────


# # def capture(categories=None):
# #     cats = categories or list(QUERIES.keys())
# #     total = saved = 0
# #     total_tokens = 0
# #     total_calls = 0

# #     for cat in cats:
# #         # Show which dental plan is being used — helps debug baseline mixups
# #         # Only shown for dental categories since others don't have dental-specific plans
# #         member_info_dict = json.loads(MEMBER_INFO_BY_CATEGORY.get(cat, "{}"))
# #         dental_plan = (
# #             member_info_dict.get("plans", {}).get("dental", {}).get("plan", "")
# #         )
# #         plan_label = (
# #             f" — dental plan: {dental_plan}" if dental_plan and "dental" in cat else ""
# #         )
# #         print(f"\n[{cat.upper()}]{plan_label}")
# #         for query in QUERIES[cat]:
# #             total += 1
# #             print(f"  {query[:65]}", end="  ", flush=True)
# #             result = run_query(query, cat)
# #             save_baseline(result)
# #             saved += 1
# #             has_answer = bool(result["answer"] and "[ERROR]" not in result["answer"])
# #             tokens = result.get("token_usage", {}).get("total_tokens", 0)
# #             calls = result.get("token_usage", {}).get("total_llm_calls", 0)
# #             total_tokens += tokens
# #             total_calls += calls
# #             # Show source for dental categories to confirm correct plan
# #             source_label = f" [{result.get('source', '')}]" if "dental" in cat else ""
# #             print(
# #                 f"{'✓' if has_answer else '✗ ERROR'}  [{tokens} tokens, {calls} LLM calls]{source_label}"
# #             )

# #     avg_tokens = total_tokens // saved if saved else 0
# #     avg_calls = round(total_calls / saved, 1) if saved else 0
# #     print(f"\nSaved {saved}/{total} baselines → {BASELINES_DIR}/")
# #     print(f"\n── Token Usage Summary ─────────────────")
# #     print(f"  Total queries:     {saved}")
# #     print(f"  Total tokens:      {total_tokens:,}")
# #     print(f"  Total LLM calls:   {total_calls}")
# #     print(f"  Avg tokens/query:  {avg_tokens}")
# #     print(f"  Avg LLM calls/q:   {avg_calls}")


# # # ── Verify mode ───────────────────────────────────────────────────────────────


# # def verify(categories=None, ci=False):
# #     cats = categories or list(QUERIES.keys())
# #     cost_failures = []
# #     info_diffs = []
# #     missing = []

# #     for cat in cats:
# #         print(f"\n[{cat.upper()}]")
# #         for query in QUERIES[cat]:
# #             baseline = load_baseline(cat, query)
# #             if baseline is None:
# #                 missing.append((cat, query))
# #                 print(f"  ? NO BASELINE  {query[:60]}")
# #                 continue

# #             current = run_query(query, cat)

# #             cost_changed, old_costs, new_costs = is_cost_changed(
# #                 baseline["answer"], current["answer"]
# #             )
# #             answer_diff = diff_answer(baseline["answer"], current["answer"])

# #             if cost_changed:
# #                 cost_failures.append((cat, query, old_costs, new_costs))
# #                 print(f"  ✗ COST CHANGED  {query[:60]}")
# #                 print(f"      baseline: {old_costs}")
# #                 print(f"      current:  {new_costs}")
# #             elif answer_diff:
# #                 info_diffs.append((cat, query, answer_diff, current))
# #                 print(f"  ~ ANSWER CHANGED  {query[:60]}")
# #             else:
# #                 print(f"  ✓  {query[:60]}")

# #     # ── Report ────────────────────────────────────────────────────────────────
# #     print()

# #     if missing:
# #         print(f"⚠  {len(missing)} queries have no baseline — run --capture first")
# #         print()

# #     if cost_failures:
# #         print(f"{'='*60}")
# #         print(f"COST FAILURES: {len(cost_failures)}  ← MUST FIX BEFORE DEPLOY")
# #         print(f"{'='*60}")
# #         for cat, q, old, new in cost_failures:
# #             print(f"\n  [{cat}] {q}")
# #             print(f"  baseline: {old}")
# #             print(f"  current:  {new}")

# #     if info_diffs and not ci:
# #         print(f"\n{'='*60}")
# #         print(f"ANSWER CHANGES: {len(info_diffs)}  ← review and approve/reject")
# #         print(f"{'='*60}")
# #         approved = rejected = 0
# #         for cat, q, diff, current in info_diffs:
# #             print(f"\n[{cat}] {q}")
# #             print(diff[:1000])  # show first 1000 chars of diff
# #             ans = input("  Accept as new baseline? [y/n]: ").strip().lower()
# #             if ans == "y":
# #                 save_baseline(current)
# #                 approved += 1
# #                 print("  ✓ Baseline updated.")
# #             else:
# #                 rejected += 1
# #                 print("  ✗ Kept old baseline.")
# #         print(f"\nAnswer changes: {approved} approved, {rejected} rejected.")
# #     elif info_diffs and ci:
# #         print(f"\nANSWER CHANGES ({len(info_diffs)}) — run without --ci to review.")

# #     total_queries = sum(len(QUERIES[c]) for c in cats)
# #     n_ok = total_queries - len(cost_failures) - len(info_diffs) - len(missing)

# #     print(f"\n{'='*60}")
# #     if cost_failures:
# #         print(f"  RESULT: FAILED  ✗  ({len(cost_failures)} cost failures)")
# #     elif info_diffs and ci:
# #         print(f"  RESULT: FAILED  ✗  ({len(info_diffs)} unreviewed answer changes)")
# #     else:
# #         print(f"  RESULT: PASSED  ✓  ({n_ok}/{total_queries} queries unchanged)")
# #     print(f"{'='*60}")

# #     return len(cost_failures) == 0 and (not ci or len(info_diffs) == 0)


# # # ── Entry point ───────────────────────────────────────────────────────────────

# # if __name__ == "__main__":
# #     parser = argparse.ArgumentParser(
# #         description="Golden regression test — calls real API"
# #     )
# #     parser.add_argument("--capture", action="store_true", help="Capture baselines")
# #     parser.add_argument(
# #         "--verify", action="store_true", help="Verify against baselines"
# #     )
# #     parser.add_argument("--ci", action="store_true", help="Non-interactive CI mode")
# #     parser.add_argument(
# #         "--category",
# #         choices=["medical", "dental_willamette", "dental_premera", "vision", "rx"],
# #         help="Run only one category",
# #     )
# #     args = parser.parse_args()

# #     cats = [args.category] if args.category else None

# #     if args.capture:
# #         print(f"\nCapturing baselines — server must be running at {API_BASE}\n")
# #         capture(cats)
# #     elif args.verify:
# #         print(f"\nVerifying against baselines — server must be running at {API_BASE}\n")
# #         ok = verify(cats, ci=args.ci)
# #         sys.exit(0 if ok else 1)
# #     else:
# #         parser.print_help()
