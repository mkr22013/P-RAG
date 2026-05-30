import re
import json


def generate_ironclad_instruction():
    return (
        "### ROLE: Health Insurance Benefits Auditor\n"
        "### TASK: Extract benefit details from provided plan data\n\n"
        "### 🚨 HARD OUTPUT CONTRACT (NON-NEGOTIABLE)\n"
        "1. You MUST return EXACTLY ONE Markdown table\n"
        "2. Returning MORE THAN ONE table = FAILURE\n"
        "3. Returning ZERO tables = FAILURE\n"
        "4. Any text outside the table = FAILURE\n\n"
        "### 🚨 TABLE FORMAT (FIXED)\n"
        "| Benefit | In-Network | Out-of-Network | Limitations |\n"
        "| :--- | :--- | :--- | :--- |\n\n"
        "### 🚨 FIELD MAPPING (CRITICAL)\n"
        "1. Each ROW contains: event, service, in_network, out_of_network, notes\n"
        "2. 'service' is the actual benefit item\n"
        "3. 'event' provides context for the service\n"
        "4. You MUST construct Benefit as:\n"
        "   → Benefit = event + ' - ' + service\n"
        "5. If event is missing, use only service\n"
        "6. NEVER ignore event if it exists\n\n"
        "### 🚨 ROW HANDLING RULE (CRITICAL)\n"
        "1. Each service represents ONE distinct row\n"
        "2. If multiple services exist under the same event, you MUST return multiple rows\n"
        "3. DO NOT merge different services into one row\n"
        "4. Merging rows = FAILURE\n\n"
        "### 🚨 RELEVANCE FILTER (CRITICAL)\n"
        "1. Include ONLY rows that directly answer the user’s question\n"
        "2. DO NOT include unrelated rows even if they are in the same section\n"
        "3. If multiple rows match the question, include ALL of them\n"
        "4. DO NOT drop valid rows just to reduce count\n"
        "5. Including irrelevant rows = FAILURE\n\n"
        "### 🚨 STRICT PARSING MODE\n"
        "1. Input context is already structured into ROW blocks\n"
        "2. Each ROW contains explicit fields\n"
        "3. Extract values EXACTLY as written\n"
        "4. DO NOT interpret, summarize, or infer\n"
        "5. DO NOT use prior knowledge\n\n"
        "### 🚨 DATA RULES\n"
        "1. Preserve exact wording\n"
        "2. If a field is missing, empty, or whitespace only → use 'Data Not Found'\n"
        "3. NEVER leave a Markdown cell empty (| |)\n\n"
        "### 🚨 ANTI-HALLUCINATION RULE\n"
        "1. Use ONLY items explicitly present in context\n"
        "2. DO NOT add new benefits not present in context\n"
        "3. DO NOT create rows that do not exist in input\n\n"
        "### 🚨 EXCLUDED / OTHER SERVICES\n"
        "If the answer belongs to excluded/other services:\n"
        "| Results |\n"
        "| :--- |\n\n"
        "### 🚨 FINAL INSTRUCTION\n"
        "Return EXACTLY ONE Markdown table and NOTHING ELSE."
    )




def build_cost_table(context: str, user_query: str, keywords: list, found_topics: list | None = None) -> str | None:
    if found_topics is None:
        found_topics = []
    rows = []
    items = re.split(r"Item \d+:", context)
    for item in items:
        item = item.strip()
        if not item:
            continue
        json_match = re.search(r"\{.*\}", item, re.DOTALL)
        if not json_match:
            continue
        try:
            data = json.loads(json_match.group(0))
        except Exception:
            continue
        event = data.get("event", "")
        service = data.get("service", "")
        in_net = data.get("in_network", "")
        out_net = data.get("out_of_network", "")
        limitation = data.get("notes") or "Data Not Found"
        rows.append((event, service, in_net, out_net, limitation))
    if not rows:
        return "No relevant cost information found."
    # if len(rows) > 10:
    #     print("[*] TOO MANY ROWS → FALLBACK TO LLM")
    #     return "__USE_LLM__"

    def norm(text):
        return re.sub(r"\s+", " ", str(text).lower())

    def soft_match(term, text):
        if re.search(r"\b" + re.escape(term) + r"\b", text):
            return True
        if term.endswith("s"):
            if re.search(r"\b" + re.escape(term[:-1]) + r"\b", text):
                return True
        if re.search(r"\b" + re.escape(term + "s") + r"\b", text):
            return True
        return False

    query_words = [
        w.lower() for w in re.split(r"\W+", user_query) if len(w) > 2
    ]
    STOP_WORDS = {
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
        "tell",
        "about",
        "want",
        "know",
        "get",
        "give",
        "find",
        "help",
        "need",
        "does",
        "do",
        "did",
        "will",
        "would",
        "should",
        "could",
        "how",
        "when",
        "where",
        "which",
        "who",
        "why",
        "and",
        "or",
        "but",
        "not",
        "no",
        "any",
        "some",
        "with",
        "in",
        "on",
        "at",
        "to",
        "of",
        "from",
        "by",
        "as",
        "an",
        "a",
        "this",
        "that",
        "these",
        "those",
        "its",
        "my",
        "your",
        "our",
        "their",
        "if",
        "so",
        "also",
        "just",
        "more",
        "like",
        "than",
        "then",
        "into",
        "out",
        "up",
        "has",
        "have",
        "had",
        "was",
        "were",
        "been",
        "be",
        "cost",
        "costs",
        "price",
        "fee",
        "amount",
        "amounts",
        "pay",
        "paying",
        "charge",
        "charges",
    }
    WEAK_WORDS = {
        "treatment",
        "service",
        "services",
        "care",
        "visit",
        "visits",
        "procedure",
        "therapy",
        "exam",
        "test",
        "testing",
        "program",
        "programs",
        "cost",
        "benefit",
        "benefits",
        "coverage",
        "affect",
        "affects",
        "apply",
        "applies",
        "work",
        "works",
        "covered",
        "cover",
        "covers",
        "plan",
        "plans",
        "under",
        "include",
        "includes",
        "provide",
        "office",
        "clinic",
        "clinics",
        "setting",
        "settings",
        "facility",
        "facilities",
        "provides",
        # Location/setting words — they qualify WHERE a service is rendered,
        # not WHAT the benefit is.  Keeping them as strong terms causes
        # every event with "Office and Clinic Visits" in its service row to
        # score ≥ MIN_CONFIDENCE and pollute results for specific queries
        # like "show me foot care in an office or clinic visit cost".
        "office",
        "clinic",
        "clinics",
        "setting",
        "settings",
        "facility",
        "facilities",
        "general",
        "standard",
        "regular",
        "specific",
    }
    strong_terms = [
        w for w in query_words if w not in STOP_WORDS and w not in WEAK_WORDS
    ]
    if keywords:
        for k in keywords:
            for part in re.split(r"\W+", k.lower()):
                if part and part not in WEAK_WORDS:
                    strong_terms.append(part)
                    # "copay" prefix matches "copayments" event names
                    if part == "copay":
                        strong_terms.append("copayments")

    # Common abbreviations and plain-language → benefit name mappings.
    # Handles "PCP visit", "ER", "meds", "telehealth" etc. without LLM.
    SYNONYMS = {
        "pcp": ["professional visits"],
        "gp": ["professional visits"],
        "er": ["emergency room"],
        "ed": ["emergency room"],
        "rx": ["prescription drug"],
        "meds": ["prescription drug"],
        "medicine": ["prescription drug"],
        "telehealth": ["virtual care"],
        "telemedicine": ["virtual care"],
        "mental": ["mental health"],
        "psych": ["mental health"],
        "lab": ["diagnostic"],
        "labs": ["diagnostic"],
        "xray": ["diagnostic"],
        "mri": ["diagnostic"],
        "uc": ["urgent care"],
        "oop": ["out-of-pocket"],
        "immunotherapy": ["cellular immunotherapy"],
        "chemo": ["chemotherapy"],
        "physio": ["rehabilitation"],
        "snf": ["skilled nursing"],
        "hme": ["home medical equipment"],
    }
    expanded = []
    for term in strong_terms:
        if term in SYNONYMS:
            expanded.extend(SYNONYMS[term])
    strong_terms = list(set(strong_terms + expanded))

    if not strong_terms:
        strong_terms = query_words
    strong_terms = list(set(strong_terms))
    if any("class" in (k or "") for k in keywords):
        print(f"[BCT] strong_terms={sorted(strong_terms)}")
    query_phrase = " ".join(strong_terms)
    event_groups = {}
    for r in rows:
        event_groups.setdefault(norm(r[0]), []).append(r)
    event_scores = []
    for event, group_rows in event_groups.items():
        score = 0
        event_text = norm(event)
        if query_phrase:
            for r in group_rows:
                if query_phrase in norm(" ".join(r)):
                    score += 500
                    break
        for term in strong_terms:
            if soft_match(term, event_text):
                score += 200
        for r in group_rows:
            service_text = norm(r[1])
            for term in strong_terms:
                if soft_match(term, service_text):
                    score += 80
        for r in group_rows:
            full_text = norm(" ".join(r))
            for term in strong_terms:
                if soft_match(term, full_text):
                    score += 10
        if score > 0:
            event_scores.append((score, event, group_rows))

    # Minimum confidence threshold.
    # Score anatomy: event name match = 200/term, phrase match = 500.
    # A score of 150+ means at least one strong term matched the event name.
    # Below that = weak/accidental match → LLM handles it better.
    MIN_CONFIDENCE = 150

    if event_scores:
        event_scores.sort(key=lambda x: x[0], reverse=True)
        if any("class" in (k or "") for k in keywords):
            for s, e, r in event_scores:
                print(f"[BCT] score={s:5d} rows={len(r):2d} event={e[:45]!r}")
        best_score, _, _ = event_scores[0]
        if best_score < MIN_CONFIDENCE:
            print(f"[*] LOW CONFIDENCE (score={best_score}) → LLM")
            return "__USE_LLM__"

        # Include ALL events that scored above MIN_CONFIDENCE ONLY when
        # keywords explicitly name multiple distinct benefits
        # e.g. ["vision hardware", "vision exams"] → show both
        # "allergy testing" → only show top event (Psychological Testing
        # would wrongly score high on "testing" alone)
        multi_word_kws = [kw for kw in keywords if " " in kw.lower()]
        confident = [
            (s, e, r) for s, e, r in event_scores if s >= MIN_CONFIDENCE
        ]

        if len(confident) > 1 and len(multi_word_kws) >= 2:
            print(
                f"[*] MULTI-EVENT MATCH ({len(confident)} events) → SHOWING ALL"
            )
            best_rows = [row for _, _, rows in confident for row in rows]
        else:
            best_rows = event_scores[0][2]
    else:
        # No event matched — too vague, let LLM handle it
        return "__USE_LLM__"

    # 🔥 Multi-class list query: balance rows across events so no class
    # is crowded out by a higher-scoring event hitting the row cap.
    # e.g. "show me all covered services" returns Class I (score 1000),
    # Class II (600), Class III (600) — without balancing, [:10] would
    # cut off Class II entirely since Class I + Class III fills 11 rows.
    class_topics_for_balance = [
        t
        for t in found_topics
        if re.match(r"^class\s+[i123]+$", t.lower().strip())
    ]
    if len(class_topics_for_balance) > 1 and len(best_rows) > 10:
        per_event = {}
        for r in best_rows:
            per_event.setdefault(r[0], []).append(r)
        max_per = max(1, 10 // len(per_event))
        balanced = []
        for event_rows in per_event.values():
            balanced.extend(event_rows[:max_per])
        best_rows = balanced
        print(
            f"[*] BALANCED ROWS: {len(best_rows)} across {len(per_event)} events"
        )
    elif len(best_rows) > 10:
        print("[*] TOO MANY FILTERED ROWS → USING TOP 10")
        best_rows = best_rows[:10]

    # Copay filter — only show copayment events when "copay" is in query.
    # Matches both: event name contains "copay" (Office Visit Copayments)
    # AND in_network value contains "copay" (e.g. "$20 copay" for D9440).
    if "copay" in user_query.lower():
        _copay_rows = [
            r
            for r in best_rows
            if "copay" in r[0].lower() or "copay" in r[2].lower()
        ]
        if _copay_rows:
            best_rows = _copay_rows

    # Suppress COST entirely when every row has no usable in_network value
    # (e.g. orthodontic D8xxx entries with "Data Not Found" — misleading to show)
    _no_data_rows = [
        r for r in best_rows if r[2] in ("", "Data Not Found", "Data not found")
    ]
    if len(_no_data_rows) == len(best_rows) and best_rows:
        return None

    # Service-level keyword filter — when specific procedure keywords are
    # present (e.g. radiographic, bitewing, prophylaxis), only show rows
    # whose service name contains at least one of those terms.
    # Prevents unrelated services in the same event from appearing
    # (e.g. D1310 Nutritional Counseling showing in an x-ray query).
    _SERVICE_SPECIFIC = {
        "radiographic",
        "bitewing",
        "periapical",
        "panoramic",
        "prophylaxis",
        "sealant",
        "fluoride",
        "varnish",
        "amalgam",
        "composite",
        "endodontic",
        "canal",
        "pontic",
        "extraction",
        "scaling",
        "debridement",
        "gingivectomy",
    }
    _svc_kws = [kw for kw in keywords if kw in _SERVICE_SPECIFIC]
    if _svc_kws:
        _svc_rows = [
            r for r in best_rows if any(kw in norm(r[1]) for kw in _svc_kws)
        ]
        if _svc_rows:
            best_rows = _svc_rows

    # Dental class-specific filter — when a single class topic is detected
    # (class i, class ii, or class iii), only show rows from that class.
    # Prevents coinsurance-word contamination pulling in wrong classes.
    # Does NOT fire when multiple class topics are present (comparison queries).
    class_topics = [
        t
        for t in found_topics
        if re.match(r"^class\s+[i123]+$", t.lower().strip())
    ]
    if len(class_topics) == 1:
        cf = class_topics[0].lower()
        class_filtered = [r for r in best_rows if cf in r[0].lower()]
        if class_filtered:
            best_rows = class_filtered
            print(f"[*] CLASS FILTER applied: {cf} → {len(best_rows)} rows")

    is_list_query = any(
        w in user_query.lower() for w in ["all", "list", "which", "show me"]
    )
    final_rows = best_rows[:10] if is_list_query else best_rows
    table = (
        "| Benefit | Service | In-Network | Out-of-Network | Limitations |\n"
    )
    table += "| :--- | :--- | :--- | :--- | :--- |\n"
    for e, s, i, o, l in final_rows:
        table += f"| {e} | {s} | {i} | {o} | {l} |\n"
    return table


def build_info_response(context: str, user_query: str, keywords: list) -> str | None:
    rows = []
    items = re.split(r"Item \d+:", context)
    for item in items:
        item = item.strip()
        if not item:
            continue
        json_match = re.search(r"\{.*\}", item, re.DOTALL)
        if not json_match:
            continue
        try:
            data = json.loads(json_match.group(0))
        except Exception:
            continue
        event = data.get("event", "")
        information = (
            data.get("information")
            or data.get("limitations")
            or "Data Not Found"
        )
        if event and information and information != "Data Not Found":
            rows.append((event, information))
    if not rows:
        return "__USE_LLM__"

    # Relevance filter: when specific keywords exist, only keep rows
    # whose event name matches at least one specific keyword.
    # Prevents "Dental Emergency" / "Dental Implant Surgery" from
    # appearing for unrelated queries just because "dental" matches.
    _GENERIC_KWS = {
        "dental",
        "vision",
        "medical",
        "plan",
        "care",
        "benefit",
        "benefits",
        "coverage",
    }
    _specific_kws = [
        k for k in keywords if k not in _GENERIC_KWS and len(k) > 3
    ]
    if _specific_kws:

        def _relevant(event_name):
            ev = event_name.lower()
            return any(
                re.search(r"\b" + re.escape(k) + r"\b", ev)
                for k in _specific_kws
            )

        filtered = [(e, i) for e, i in rows if _relevant(e)]
        if filtered:
            rows = filtered

    table = "| Topic | Coverage Information |\n"
    table += "| :--- | :--- |\n"
    for event, info in rows:
        table += f"| {event} | {info} |\n"
    return table

# --------------------------------------------------------
# 🔥 MULTI-SECTION → try no-LLM first, fall back to LLM
# --------------------------------------------------------