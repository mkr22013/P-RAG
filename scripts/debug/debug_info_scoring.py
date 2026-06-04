"""
Debug: checks if info entries exist in the index and simulates server scoring.
Automatically finds the medical index file in ../docs/ or current folder.

Usage: python debug_info_scoring.py
"""

import json, re, os, glob

# ── CONFIG — change query here ────────────────────────────────────────────────
QUERY = "allergy testing cost"
# ─────────────────────────────────────────────────────────────────────────────


def find_index():
    """Auto-find the medical index JSON file."""
    patterns = [
        "./indices/*medical*.json",
        "./indices/*ppo*.json",
        "../indices/*medical*.json",
        "../indices/*ppo*.json",
        "./*medical*.json",
        "./*ppo*.json",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            # If multiple, prefer the one with 'medical' in name
            medical = [m for m in matches if "medical" in m.lower()]
            return medical[0] if medical else matches[0]
    # Fallback: walk up looking for indices folder
    for root, dirs, files in os.walk("."):
        for f in files:
            if (
                f.endswith(".json")
                and ("medical" in f.lower() or "ppo" in f.lower())
                and "sbc" not in f.lower()
                and "dental" not in f.lower()
                and "vision" not in f.lower()
            ):
                return os.path.join(root, f)
    return None


def soft_match(term, text):
    text = text.lower()
    term = term.lower()
    if term in text:
        return True
    if term.endswith("s") and term[:-1] in text:
        return True
    if (term + "s") in text:
        return True
    return False


def score_chunk(chunk, keywords, strong_query_words):
    chunk_topic = chunk.get("topic", "")
    content = json.dumps(chunk.get("content", {}))
    full_text = chunk_topic + " " + content
    strong_kws = chunk.get("keywords", [])
    category = chunk.get("category", "")

    # INFO entries: score on event name only (fixes prose content noise)
    if category == "info":
        content_dict = chunk.get("content", {})
        event_lower = (
            content_dict.get("event", "").lower()
            if isinstance(content_dict, dict)
            else ""
        )
        info_score = 0
        for w in strong_query_words:
            if soft_match(w, event_lower):
                info_score += 200
        for kw in keywords:
            if soft_match(kw, event_lower):
                info_score += 100
        return info_score

    # COST/QA entries: full scoring
    score = 0
    for kw in strong_kws:
        if soft_match(kw, chunk_topic):
            score += 100
        elif soft_match(kw, content):
            score += 60
    for kw in keywords:
        if soft_match(kw, full_text):
            score += 40
    for w in strong_query_words:
        if soft_match(w, chunk_topic):
            score += 20
        elif soft_match(w, content):
            score += 10
    return score


# ── main ──────────────────────────────────────────────────────────────────────
index_path = find_index()
if not index_path:
    print(
        "[!] Could not find medical index JSON. Place this script next to your docs/ folder."
    )
    exit(1)

print(f"Index file: {index_path}")
data = json.load(open(index_path, encoding="utf-8"))

info_entries = [e for e in data if e.get("category") == "info"]
cost_entries = [e for e in data if e.get("category") == "cost"]
print(
    f"Total: {len(data)}  |  Cost: {len(cost_entries)}  |  Info: {len(info_entries)}\n"
)

# Simulate keyword extraction
query = QUERY.lower()
STOP = {
    "show",
    "me",
    "you",
    "can",
    "what",
    "are",
    "is",
    "the",
    "for",
    "all",
    "and",
    "does",
    "it",
    "cost",
    "about",
    "want",
    "know",
    "my",
    "tell",
}
words = [w for w in re.split(r"\W+", query) if len(w) > 2 and w not in STOP]
keywords = words
strong_query_words = words

print(f"Query:       {QUERY!r}")
print(f"Query words: {words}\n")

# Score all info entries
scored = []
for e in info_entries:
    s = score_chunk(e, keywords, strong_query_words)
    if s > 0:
        scored.append((s, e))
scored.sort(key=lambda x: x[0], reverse=True)

print(f"INFO entries scoring > 0: {len(scored)}")
for sc, e in scored[:10]:
    event = e["content"].get("event", "")
    kws = e.get("keywords", [])[:5]
    print(f"  score={sc:4d}  event={event!r}")
    print(f"           keywords={kws}")

print()
print("─" * 60)
print("EXPECTED INFO ENTRIES (event name contains query word):")
query_words = set(words)
found_any = False
for e in info_entries:
    event_lower = e["content"].get("event", "").lower()
    if any(w in event_lower for w in query_words):
        found_any = True
        s = score_chunk(e, keywords, strong_query_words)
        kws = e.get("keywords", [])[:8]
        text = e["content"].get("limitations", "")[:120]
        print(f"\n  event   : {e['content'].get('event')!r}")
        print(f"  score   : {s}")
        print(f"  keywords: {kws}")
        print(f"  content : {text!r}...")

if not found_any:
    print("  ✗ NONE FOUND — index may not have been regenerated yet")
    print()
    print("  All info event names:")
    for e in info_entries:
        print(f"    {e['content'].get('event','')!r}")
