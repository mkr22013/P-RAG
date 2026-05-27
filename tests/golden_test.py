"""
Golden file regression test.

Captures the structured COST and INFO rows returned by the retrieval pipeline
for every query in QueriesToTests.txt and saves them as baselines.
On subsequent runs, compares results against baselines.

Rules:
  - COST rows changed  → HARD FAIL  (cost data is non-negotiable)
  - INFO rows changed  → SHOW DIFF, prompt approve/reject

Usage:
  python golden_test.py --capture     # Run all queries, save baselines
  python golden_test.py --verify      # Run all queries, compare to baselines
  python golden_test.py --verify --ci # Non-interactive: fail on any diff
  python golden_test.py --update <query_id>  # Re-approve a single baseline

Baselines are stored in:  baselines/<category>/<query_slug>.json
"""

import sys, os, re, json, argparse, types
from unittest.mock import MagicMock
from datetime import datetime
from difflib import unified_diff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINES_DIR = os.path.join(BASE_DIR, "baselines")

# ── Stub heavy dependencies so we can import client and server ────────────────
sys.modules["ollama"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["pdfplumber"] = MagicMock()
sys.modules["fastmcp"] = MagicMock()
sys.modules["insurance_mcp"] = MagicMock()
sys.modules["insurance_mcp.server"] = MagicMock()

utility_pkg = types.ModuleType("utility")
utility_utils = types.ModuleType("utility.utils")
utility_utils.get_smart_keywords = lambda t: []
sys.modules["utility"] = utility_pkg
sys.modules["utility.utils"] = utility_utils

sys.path.insert(0, BASE_DIR)
import client as cl

# Import server directly so we can call get_plan_data_from_disk without MCP
import importlib, importlib.util


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "server_mod", os.path.join(BASE_DIR, "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod


server_mod = _load_server()

# ── Queries ───────────────────────────────────────────────────────────────────

QUERIES = {
    "medical": [
        "allergy testing and treatment cost",
        "want to know about blood products",
        "show me cost for immunotherapy",
        "I want to know about emergency room service",
        "my urgent care cost",
        "are there any benefit for therapeutic injections",
        "show me all Apicoectomy benefits",
        "Can you show me transplants cost",
        "show me all benefits for Temporomandibular Joint Disorders (TMJ) Care",
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
        "how much is the charge for Retrograde filling",
        "what are the cost for breast reconstructions",
        "show me gender affirming care professional service",
        "does my plan provide medical food during my hospital stay",
        "show me newborn care benefits",
        "show me new born care impatient care cost",
        "does my plan cover Non-preferred generic and brand name drugs?",
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
    ],
    "dental": [
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
        "Are dental implants covered?",
        "What dental services are NOT covered or excluded?",
        "show me all covered services under my dental plan",
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
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def slug(query):
    return re.sub(r"[^\w]+", "_", query.lower()).strip("_")[:60]


def baseline_path(category, query):
    return os.path.join(BASELINES_DIR, category, f"{slug(query)}.json")


def words(q):
    return [re.sub(r"[^\w\s]", "", w) for w in q.lower().split()]


def resolve(query):
    r = cl.resolve_insurance_topic(words(query), query.lower())
    return r["topics"], r["keywords"]


def detect(query):
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        cl, "get_category_from_llm", return_value="medical"
    ):
        return cl.detect_category(words(query), query)


def call_server(query, category):
    """Call get_plan_data_from_disk directly — no Ollama needed."""
    topics, keywords = resolve(query)
    if not topics:
        topics = [query.lower()]

    try:
        result = server_mod.get_plan_data_from_disk(
            query,
            tuple(sorted(topics)),
            category,
            tuple(sorted(set(keywords))),
        )
        return result or ""
    except Exception as e:
        return f"[ERROR] {e}"


def parse_context(context):
    """Parse ### SECTION: COST / INFO from server response into structured dicts."""
    cost_rows, info_rows = [], []

    cost_section = re.search(
        r"### SECTION: COST\s*(.*?)(?=### SECTION:|$)", context, re.DOTALL
    )
    info_section = re.search(
        r"### SECTION: INFO\s*(.*?)(?=### SECTION:|$)", context, re.DOTALL
    )

    def extract_items(text):
        items = []
        for m in re.finditer(r"Item \d+:\s*(\{.*?\})", text, re.DOTALL):
            try:
                items.append(json.loads(m.group(1)))
            except Exception:
                pass
        return items

    if cost_section:
        cost_rows = extract_items(cost_section.group(1))
    if info_section:
        info_rows = extract_items(info_section.group(1))

    return cost_rows, info_rows


def run_query(query, category):
    context = call_server(query, category)
    cost_rows, info_rows = parse_context(context)
    return {
        "query": query,
        "category": category,
        "timestamp": datetime.now().isoformat(),
        "cost_rows": cost_rows,
        "info_rows": info_rows,
    }


def save_baseline(result):
    cat = result["category"]
    path = baseline_path(cat, result["query"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


def load_baseline(category, query):
    path = baseline_path(category, query)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff_rows(label, old, new):
    old_str = json.dumps(old, indent=2, sort_keys=True)
    new_str = json.dumps(new, indent=2, sort_keys=True)
    if old_str == new_str:
        return None
    lines = list(
        unified_diff(
            old_str.splitlines(),
            new_str.splitlines(),
            fromfile=f"baseline {label}",
            tofile=f"current {label}",
            lineterm="",
        )
    )
    return "\n".join(lines)


# ── Capture mode ──────────────────────────────────────────────────────────────


def capture(categories=None):
    cats = categories or list(QUERIES.keys())
    total = saved = 0
    for cat in cats:
        for query in QUERIES[cat]:
            total += 1
            print(f"  [{cat}] {query[:65]}", end="  ", flush=True)
            result = run_query(query, cat)
            save_baseline(result)
            saved += 1
            cost_n = len(result["cost_rows"])
            info_n = len(result["info_rows"])
            print(f"cost={cost_n} info={info_n} ✓")
    print(f"\nSaved {saved}/{total} baselines → {BASELINES_DIR}/")


# ── Verify mode ───────────────────────────────────────────────────────────────


def verify(categories=None, ci=False):
    cats = categories or list(QUERIES.keys())
    cost_failures = []
    info_diffs = []
    missing = []

    for cat in cats:
        for query in QUERIES[cat]:
            baseline = load_baseline(cat, query)
            if baseline is None:
                missing.append((cat, query))
                continue

            current = run_query(query, cat)

            cost_diff = diff_rows("COST", baseline["cost_rows"], current["cost_rows"])
            info_diff = diff_rows("INFO", baseline["info_rows"], current["info_rows"])

            if cost_diff:
                cost_failures.append((cat, query, cost_diff))
                print(f"  ✗ COST CHANGED  [{cat}] {query[:60]}")
            elif info_diff:
                info_diffs.append((cat, query, info_diff, current))
                print(f"  ~ INFO CHANGED  [{cat}] {query[:60]}")
            else:
                print(f"  ✓               [{cat}] {query[:60]}")

    # ── Report ────────────────────────────────────────────────────────────────
    print()

    if missing:
        print(f"⚠  {len(missing)} queries have no baseline — run --capture first:")
        for cat, q in missing:
            print(f"     [{cat}] {q}")
        print()

    if cost_failures:
        print(f"{'='*60}")
        print(f"COST FAILURES: {len(cost_failures)}  ← MUST FIX BEFORE DEPLOY")
        print(f"{'='*60}")
        for cat, q, diff in cost_failures:
            print(f"\n[{cat}] {q}")
            print(diff)

    if info_diffs and not ci:
        print(f"\n{'='*60}")
        print(f"INFO CHANGES: {len(info_diffs)}  ← review and approve/reject")
        print(f"{'='*60}")
        approved = rejected = 0
        for cat, q, diff, current in info_diffs:
            print(f"\n[{cat}] {q}")
            print(diff)
            ans = input("  Accept this change as new baseline? [y/n]: ").strip().lower()
            if ans == "y":
                save_baseline(current)
                approved += 1
                print("  ✓ Baseline updated.")
            else:
                rejected += 1
                print("  ✗ Baseline kept.")
        print(f"\nINFO: {approved} approved, {rejected} rejected.")
    elif info_diffs and ci:
        print(
            f"\nINFO CHANGES ({len(info_diffs)}) — run without --ci to review interactively."
        )

    total_queries = sum(len(QUERIES[c]) for c in cats)
    n_ok = total_queries - len(cost_failures) - len(info_diffs) - len(missing)

    print(f"\n{'='*60}")
    if cost_failures:
        print(f"  RESULT: FAILED  ✗  ({len(cost_failures)} cost failures)")
    elif info_diffs and ci:
        print(f"  RESULT: FAILED  ✗  ({len(info_diffs)} unreviewed info changes)")
    else:
        print(f"  RESULT: PASSED  ✓  ({n_ok}/{total_queries} queries unchanged)")
    print(f"{'='*60}")

    return len(cost_failures) == 0 and (not ci or len(info_diffs) == 0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Golden file regression test")
    parser.add_argument("--capture", action="store_true", help="Capture baselines")
    parser.add_argument(
        "--verify", action="store_true", help="Verify against baselines"
    )
    parser.add_argument("--ci", action="store_true", help="Non-interactive CI mode")
    parser.add_argument(
        "--category",
        choices=["medical", "dental", "vision"],
        help="Run only one category",
    )
    args = parser.parse_args()

    cats = [args.category] if args.category else None

    if args.capture:
        print(f"\nCapturing baselines for: {cats or 'all categories'}\n")
        capture(cats)
    elif args.verify:
        print(f"\nVerifying: {cats or 'all categories'}\n")
        ok = verify(cats, ci=args.ci)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()
