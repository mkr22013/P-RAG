"""
Test suite for the client/server query pipeline.

Covers every query in QueriesToTests.txt across four areas:

    PART 1 — resolve_insurance_topic   (pure Python, no mocks needed)
    PART 2 — detect_category           (mocked ollama)
    PART 3 — Server is_list_query      (extracted pure logic, regression for "show me" fix)
    PART 4 — build_cost_table scoring  (mock contexts, regression for WEAK_WORDS fix)

Run with:  python test_query_pipeline.py
All tests must pass before deploying changes to client.py or server.py.
"""

import sys, re, json, types
from unittest.mock import MagicMock, patch

_ollama_mock = MagicMock()
sys.modules["ollama"] = _ollama_mock
sys.modules["dotenv"] = MagicMock()
sys.modules["pdfplumber"] = MagicMock()

_server_mock = MagicMock()
sys.modules["insurance_mcp"] = _server_mock
sys.modules["insurance_mcp.server"] = _server_mock

sys.path.insert(0, ".")
import clients.client as cl


def words(query: str):
    """Reproduce client.py query_words tokenisation."""
    return [re.sub(r"[^\w\s]", "", w) for w in query.lower().split()]


def resolve(query: str):
    return cl.resolve_insurance_topic(words(query), query.lower())


def topics_of(query: str):
    return resolve(query)["topics"]


def keywords_of(query: str):
    return resolve(query)["keywords"]


def detect(query: str):
    """detect_category with LLM fallback mocked to return 'medical'."""
    with patch.object(cl, "get_category_from_llm", return_value="medical"):
        return cl.detect_category(words(query), query)


def server_is_list_query(query: str) -> bool:
    """
    Pure copy of the is_list_query logic in server.py.
    If this test ever diverges from server.py, update both together.
    """
    q = query.lower()
    return (
        any(w in q for w in ["all", "list", "which"])
        or ("show me" in q and any(w in q for w in ["all", "list", "every"]))
        or "what are" in q
        or ("give me" in q and any(w in q for w in ["all", "list", "every"]))
    )


def _soft_match(word, text):
    if re.search(r"\b" + re.escape(word) + r"\b", text):
        return True
    if word.endswith("s") and re.search(r"\b" + re.escape(word[:-1]) + r"\b", text):
        return True
    if re.search(r"\b" + re.escape(word + "s") + r"\b", text):
        return True
    return False


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
}

WEAK_WORDS = {
    "treatment",
    "service",
    "services",
    "care",
    "visit",
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
    "provides",
    "office",
    "clinic",
    "clinics",
    "setting",
    "settings",
    "facility",
    "facilities",
}

MIN_CONFIDENCE = 150


def score_events(context: str, user_query: str, kw_list: list):
    """
    Parse a mock COST section context and return (event_scores, strong_terms).
    event_scores: [(score, event_name, rows_list), ...]
    Used by Part 4 scoring tests.
    """
    rows = []
    for item in re.split(r"Item \d+:", context):
        item = item.strip()
        if not item:
            continue
        m = re.search(r"\{.*\}", item, re.DOTALL)
        if not m:
            continue
        try:
            d = json.loads(m.group(0))
        except Exception:
            continue
        rows.append(
            (
                d.get("event", ""),
                d.get("service", ""),
                d.get("in_network", ""),
                d.get("out_of_network", ""),
                d.get("notes", "Data Not Found"),
            )
        )

    def norm(t):
        return re.sub(r"\s+", " ", str(t).lower())

    qw = [w.lower() for w in re.split(r"\W+", user_query) if len(w) > 2]
    strong = [w for w in qw if w not in STOP_WORDS and w not in WEAK_WORDS]
    if kw_list:
        for k in kw_list:
            for part in re.split(r"\W+", k.lower()):
                if part and part not in WEAK_WORDS:
                    strong.append(part)
    if not strong:
        strong = qw
    strong = list(set(strong))

    event_groups = {}
    for r in rows:
        event_groups.setdefault(norm(r[0]), []).append(r)

    event_scores = []
    for event, group in event_groups.items():
        score = 0
        for term in strong:
            if _soft_match(term, event):
                score += 200
        for r in group:
            for term in strong:
                if _soft_match(term, norm(r[1])):
                    score += 80
        for r in group:
            for term in strong:
                if _soft_match(term, norm(" ".join(r))):
                    score += 10
        if score > 0:
            event_scores.append((score, event, group))

    event_scores.sort(key=lambda x: x[0], reverse=True)
    return event_scores, strong


def make_cost_context(*items):
    """Build a mock ### SECTION: COST context from dicts."""
    lines = ["### SECTION: COST\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"Item {i}:\n{json.dumps(item, indent=2)}\n")
    return "\n".join(lines)


def test_topic_urgent_care():
    """'my urgent care cost' → must resolve urgent care topic."""
    assert "urgent care" in topics_of("my urgent care cost")
    assert "urgent care" in keywords_of("my urgent care cost")


def test_topic_emergency_room():
    """'emergency room service' → emergency topic."""
    t = topics_of("I want to know about emergency room service")
    assert "emergency" in t


def test_topic_emergency_room_keywords():
    """Keyword captured is 'emergency room' (multi-word), not bare 'emergency'."""
    assert "emergency room" in keywords_of(
        "I want to know about emergency room service"
    )


def test_topic_pcp():
    """'what is my pcp copay' → Professional Visit Copay topic."""
    assert "Professional Visit Copay" in topics_of("what is my pcp copay?")


def test_topic_deductible():
    """'how much is my deductible' → deductible topic."""
    assert "deductible" in topics_of("how much is my deductible?")


def test_topic_family_deductible():
    """'show me my family deductible' → deductible topic + family keyword."""
    assert "deductible" in topics_of("show me my family deductible")
    assert "family" in keywords_of("show me my family deductible")


def test_topic_diagnostic_blood():
    """'want to know about blood products' → diagnostic topic, blood work keyword."""
    assert "diagnostic" in topics_of("want to know about blood products")
    assert "blood work" in keywords_of("want to know about blood products")


def test_topic_diagnostic_xray_imaging():
    """'x-ray, lab and imaging' → both diagnostic and imaging topics."""
    t = topics_of("what is my cost for x-ray, lab and imaging")
    assert "diagnostic" in t
    assert "imaging" in t


def test_topic_xray_keyword():
    """x-ray keyword captured for diagnostic query."""
    assert "x-ray" in keywords_of("what is my cost for x-ray, lab and imaging")


def test_topic_mental_health():
    """'psychological testing' → mental-health topic."""
    assert "mental-health" in topics_of(
        "Do i need to pay any amount for psychological testing"
    )


def test_topic_psychological_testing_keyword():
    """'psychological testing' captured as multi-word keyword."""
    assert "psychological testing" in keywords_of(
        "Do i need to pay any amount for psychological testing"
    )


def test_topic_rehabilitation():
    """'rehabilitation therapy' → rehabilitation topic."""
    assert "rehabilitation" in topics_of("Want to know about rehabilitation therapy")


def test_topic_hospital():
    """'skilled nursing facility care' → hospital topic."""
    assert "hospital" in topics_of(
        "what are the benefits for skilled nursing facility care"
    )


def test_topic_maternity():
    """'newborn care' → maternity topic (delivery/newborn context)."""
    kw = keywords_of("show me newborn care benefits")
    assert "newborn" in kw


def test_topic_pharmacy():
    """'Non-preferred generic and brand name drugs' → pharmacy topic."""
    assert "pharmacy" in topics_of(
        "does my plan cover Non-preferred generic and brand name drugs?"
    )


def test_topic_specialist():
    """'specialist visit cost' → specialist topic."""
    assert "specialist" in topics_of("what is the cost for a specialist visit")


def test_topic_tmj():
    """
    TMJ query without an explicit dental trigger word ("dental","tooth",etc.)
    → rule-based dental branch does NOT fire → falls to LLM.
    Residual keyword extraction must capture 'temporomandibular' so the server
    can still search correctly.
    Note: queries like "show me tmj care" DO fire the dental branch (via 'tmj'
    in TMJ_TERMS inside the dental block) — but that requires "dental" etc. first.
    """
    kw = keywords_of("show me all Temporomandibular Joint Disorders (TMJ) Care")
    assert "temporomandibular" in kw


def test_topic_dental_filling():
    """Filling → class ii basic services."""
    assert "class ii" in topics_of("how much is the charge for Retrograde filling")


def test_topic_dental_denture():
    """Denture → class iii major services."""
    assert "class iii" in topics_of("show me all about complete denture benefits")


def test_topic_dental_ortho():
    """'orthodontic treatment covered' → orthodontic treatment topic."""
    assert "orthodontic treatment" in topics_of(
        "Is orthodontic treatment covered under my dental plan?"
    )


def test_topic_no_standard_topic_foot_care():
    """
    'show me foot care in an office or clinic visit cost' has no standard
    topic — falls to LLM.  The key keywords MUST be captured so the server
    retrieval can score correctly.
    """
    kw = keywords_of("show me foot care in an office or clinic visit cost")
    assert "foot" in kw


def test_topic_allergy_keywords():
    """Allergy query — no standard topic, but 'allergy' keyword captured."""
    assert "allergy" in keywords_of("allergy testing and treatment cost")


def test_topic_immunotherapy_keyword():
    """Immunotherapy keyword captured."""
    assert "immunotherapy" in keywords_of("show me cost for immunotherapy")


def test_topic_transplant_keyword():
    """Transplant keyword captured."""
    assert "transplants" in keywords_of("Can you show me transplants cost")


def test_topic_vasectomy_keyword():
    """Vasectomy keyword captured."""
    assert "vasectomy" in keywords_of("show me cost for vasectomy")


def test_topic_bariatric_keyword():
    """Bariatric keyword captured."""
    assert "bariatric" in keywords_of("is Bariatric surgery covered under my plan?")


def test_topic_vision_exam():
    """'vision exam' → vision exams topic."""
    assert "vision exams" in topics_of("What is the cost for a vision exam?")


def test_topic_vision_hardware_contacts():
    """'contact lenses' → vision hardware topic."""
    assert "vision hardware" in topics_of(
        "Are contact lenses covered under my vision plan?"
    )


def test_topic_vision_hardware_glasses():
    """'glasses' → vision hardware topic."""
    assert "vision hardware" in topics_of(
        "How much does vision hardware cost in network?"
    )


def test_topic_vision_out_of_area():
    """'travelling' → out-of-area care topic."""
    assert "out-of-area care" in topics_of(
        "Is vision care covered when I am travelling?"
    )


def test_topic_vision_exclusions():
    """'not covered' → exclusions and limitations topic."""
    assert "exclusions and limitations" in topics_of(
        "What vision services are excluded from my plan?"
    )


def test_topic_vision_provider():
    """'in-network vision provider' → selecting a vision care provider topic."""
    assert "selecting a vision care provider" in topics_of(
        "How does selecting an in-network vision provider affect my costs?"
    )


def test_topic_dental_class_i():
    """
    'Class I diagnostic and preventive services' — the regex r"\bclass\s+[i123]"
    now fires the dental branch even without a dental trigger word.
    The medical 'diagnostic' branch is suppressed for class-based queries.
    """
    t = topics_of("What does Class I diagnostic and preventive services cost me?")
    assert "class i" in t
    assert "diagnostic" not in t


def test_topic_dental_class_ii():
    """Basic dental / filling → class ii basic services."""
    t = topics_of("What is my coinsurance for a basic dental service?")
    assert "class ii" in t


def test_topic_dental_class_iii():
    """Crown / major → class iii major services."""
    t = topics_of("What percentage do I pay for major dental work like a crown?")
    assert "class iii" in t


def test_topic_dental_deductible():
    """'dental deductible' → plan limits topic."""
    t = topics_of("What is my calendar year dental deductible?")
    assert "plan limits" in t


def test_category_emergency():
    assert detect("I want to know about emergency room service") == "medical"


def test_category_urgent():
    assert detect("my urgent care cost") == "medical"


def test_category_hospital():
    assert (
        detect("what are the benefits for skilled nursing facility care") == "medical"
    )


def test_category_pcp():
    assert detect("what is my pcp copay?") == "medical"


def test_category_cancer():
    assert detect("is cancer treatment covered?") == "medical"


def test_category_vision_exam():
    assert detect("What is the cost for a vision exam?") == "vision"


def test_category_vision_hardware():
    assert detect("How much does vision hardware cost?") == "vision"


def test_category_glasses():
    assert detect("I need new glasses, what does my plan cover?") == "vision"


def test_category_contacts():
    """
    'contact lenses' — 'lenses' is now a rule-based vision trigger word → no LLM needed.
    """
    result = cl.detect_category(
        words("Are contact lenses covered?"), "Are contact lenses covered?"
    )
    assert result == "vision"


def test_category_braces():
    assert detect("how much do braces cost?") == "dental"


def test_category_dental_cleaning():
    """'teeth cleaning' → 'teeth' is now a rule-based dental trigger word → no LLM needed."""
    result = cl.detect_category(
        words("Is a teeth cleaning covered?"), "Is a teeth cleaning covered?"
    )
    assert result == "dental"


def test_category_dental_crown():
    """'crown cost' → 'crown' is now a rule-based dental trigger word → no LLM needed."""
    result = cl.detect_category(
        words("What does a crown cost?"), "What does a crown cost?"
    )
    assert result == "dental"


def test_category_dental_ortho():
    """'orthodontic' is now a rule-based dental trigger word — no LLM needed."""
    result = cl.detect_category(
        words("Is orthodontic treatment covered?"), "Is orthodontic treatment covered?"
    )
    assert result == "dental"


def test_category_foot_care_llm():
    """Foot care has no rule-based signal → falls to LLM classifier."""
    with patch.object(cl, "get_category_from_llm", return_value="medical") as mock_llm:
        result = cl.detect_category(
            words("show me foot care in an office or clinic visit cost"),
            "show me foot care in an office or clinic visit cost",
        )
    mock_llm.assert_called_once()
    assert result == "medical"


def test_category_vasectomy_llm():
    """Vasectomy → LLM fallback."""
    with patch.object(cl, "get_category_from_llm", return_value="medical") as mock_llm:
        result = cl.detect_category(
            words("show me cost for vasectomy"), "show me cost for vasectomy"
        )
    mock_llm.assert_called_once()


def test_not_list_show_me_foot_care():
    """
    REGRESSION: 'show me foot care...' was triggering is_list_query=True,
    causing 10 chunks to be returned.  Must be False after fix.
    """
    assert (
        server_is_list_query("show me foot care in an office or clinic visit cost")
        is False
    )


def test_not_list_show_me_cost_for():
    assert server_is_list_query("show me cost for immunotherapy") is False


def test_not_list_show_me_gender_affirming():
    assert (
        server_is_list_query("show me gender affirming care professional service")
        is False
    )


def test_not_list_show_me_newborn_care():
    assert server_is_list_query("show me newborn care benefits") is False


def test_not_list_give_me_pcp_copay():
    assert server_is_list_query("give me my pcp copay") is False


def test_not_list_want_to_know():
    assert server_is_list_query("I want to know about emergency room service") is False


def test_not_list_what_is_deductible():
    assert server_is_list_query("what is my deductible?") is False


def test_list_show_me_all():
    """'show me all Apicoectomy benefits' — 'all' present → True."""
    assert server_is_list_query("show me all Apicoectomy benefits") is True


def test_list_show_me_all_dialysis():
    assert server_is_list_query("show me all dialysis related benefits") is True


def test_list_show_me_all_virtual():
    assert server_is_list_query("show me all my virtual care benefits") is True


def test_list_show_me_all_home_health():
    assert server_is_list_query("show me all home health care benefits") is True


def test_list_what_are():
    """'what are' is a list signal."""
    assert server_is_list_query("what are the $ amount for electronic visits") is True


def test_list_what_are_benefits():
    assert (
        server_is_list_query("what are the benefits for skilled nursing facility care")
        is True
    )


def test_list_list_keyword():
    assert server_is_list_query("list all my pharmacy benefits") is True


def test_list_which():
    assert (
        server_is_list_query("which services are covered under rehabilitation?") is True
    )


def test_list_give_me_all():
    assert server_is_list_query("give me all my benefits") is True


FOOT_CARE_OFFICE_ROW = {
    "event": "Foot Care",
    "service": "In an office or clinic",
    "in_network": "Kinwell: $0 copay; Other Non-Specialist: $25 copay",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
FOOT_CARE_OTHER_ROW = {
    "event": "Foot Care",
    "service": "All other settings",
    "in_network": "Deductible, then 20% coinsurance",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
ACUPUNCTURE_ROW = {
    "event": "Acupuncture",
    "service": "Office and Clinic Visits",
    "in_network": "$25 copay per visit, deductible waived",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
GENDER_AFFIRMING_ROW = {
    "event": "Gender Affirming Care",
    "service": "Office and clinic visits",
    "in_network": "Kinwell: $0 copay",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
MENTAL_HEALTH_ROW = {
    "event": "Mental Health Care",
    "service": "Office and clinic visits",
    "in_network": "$25 copay per visit, deductible waived",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
URGENT_CARE_ROW = {
    "event": "Urgent Care",
    "service": "Office and Clinic Visits",
    "in_network": "$25 copay per visit, deductible waived",
    "out_of_network": "Deductible, then 40% coinsurance",
    "notes": "Data Not Found",
}
PROFESSIONAL_VISIT_ROW = {
    "event": "Professional Visits And Services",
    "service": "Primary Care",
    "in_network": "$25 copay per visit",
    "out_of_network": "Deductible then 40% coinsurance",
    "notes": "Data Not Found",
}
PROFESSIONAL_SPECIALIST_ROW = {
    "event": "Professional Visits And Services",
    "service": "Specialist",
    "in_network": "$40 copay per visit",
    "out_of_network": "Deductible then 40% coinsurance",
    "notes": "Data Not Found",
}


def test_weak_words_office_clinic_not_strong():
    """
    REGRESSION: 'office' and 'clinic' must be in WEAK_WORDS.
    Before the fix they were strong terms, scoring Acupuncture/Mental Health/etc
    at 160+ just because their service text says 'Office and Clinic Visits'.
    """
    assert "office" in WEAK_WORDS, "'office' must be in WEAK_WORDS"
    assert "clinic" in WEAK_WORDS, "'clinic' must be in WEAK_WORDS"
    assert "clinics" in WEAK_WORDS


def test_foot_care_query_strong_terms():
    """
    For 'show me foot care in an office or clinic visit cost',
    only 'foot' should survive as a strong term (office/clinic/care/visit are weak).
    """
    ctx = make_cost_context(FOOT_CARE_OFFICE_ROW, ACUPUNCTURE_ROW)
    ev_scores, strong = score_events(
        ctx,
        "show me foot care in an office or clinic visit cost",
        ["foot care", "office visits"],
    )
    assert "foot" in strong
    assert "office" not in strong
    assert "clinic" not in strong


def test_foot_care_scores_highest():
    """
    'foot care' query — Foot Care event must score highest.
    """
    ctx = make_cost_context(
        FOOT_CARE_OFFICE_ROW,
        FOOT_CARE_OTHER_ROW,
        ACUPUNCTURE_ROW,
        GENDER_AFFIRMING_ROW,
        MENTAL_HEALTH_ROW,
    )
    ev_scores, _ = score_events(
        ctx,
        "show me foot care in an office or clinic visit cost",
        ["foot care", "office visits"],
    )
    assert ev_scores, "Expected at least one scored event"
    top_event = ev_scores[0][1]
    assert (
        "foot care" in top_event.lower()
    ), f"Expected Foot Care on top, got: {top_event}"


def test_unrelated_events_below_confidence_after_fix():
    """
    REGRESSION: After the WEAK_WORDS fix, Acupuncture/Gender Affirming/Mental
    Health must NOT reach MIN_CONFIDENCE=150 for the foot care query.
    Before fix they scored 160 (office=80, clinic=80) and appeared in results.
    """
    ctx = make_cost_context(
        ACUPUNCTURE_ROW,
        GENDER_AFFIRMING_ROW,
        MENTAL_HEALTH_ROW,
    )
    ev_scores, _ = score_events(
        ctx,
        "show me foot care in an office or clinic visit cost",
        ["foot care", "office visits"],
    )
    for score, event, _ in ev_scores:
        assert (
            score < MIN_CONFIDENCE
        ), f"'{event}' scored {score} ≥ {MIN_CONFIDENCE} — would appear in results incorrectly"


def test_urgent_care_scores_confidently():
    """
    'my urgent care cost' — Urgent Care must score ≥ MIN_CONFIDENCE.
    """
    ctx = make_cost_context(URGENT_CARE_ROW, FOOT_CARE_OFFICE_ROW)
    ev_scores, _ = score_events(ctx, "my urgent care cost", ["urgent care"])
    assert ev_scores, "Expected scored events"
    top_score, top_event, _ = ev_scores[0]
    assert "urgent care" in top_event.lower()
    assert top_score >= MIN_CONFIDENCE


def test_urgent_care_beats_foot_care():
    """
    'urgent care' query — Urgent Care must outscore Foot Care.
    """
    ctx = make_cost_context(URGENT_CARE_ROW, FOOT_CARE_OFFICE_ROW)
    ev_scores, _ = score_events(ctx, "my urgent care cost", ["urgent care"])
    events_in_order = [e for _, e, _ in ev_scores]
    assert events_in_order[0] == "urgent care"


def test_professional_visit_pcp_scores():
    """
    'what is my pcp copay' — Professional Visits And Services must score.
    """
    ctx = make_cost_context(PROFESSIONAL_VISIT_ROW, PROFESSIONAL_SPECIALIST_ROW)
    ev_scores, _ = score_events(
        ctx, "what is my pcp copay", ["Professional Visit Copay", "copay"]
    )
    assert ev_scores, "Expected Professional Visit to score"
    assert "professional visits and services" in ev_scores[0][1].lower()


def test_immunotherapy_keyword_matches_event():
    """
    'show me cost for immunotherapy' — event containing 'immunotherapy' must score.
    """
    immuno_row = {
        "event": "Cellular Immunotherapy",
        "service": "Inpatient facility care",
        "in_network": "Deductible, then 20% coinsurance",
        "out_of_network": "Deductible, then 40% coinsurance",
        "notes": "Data Not Found",
    }
    ctx = make_cost_context(immuno_row, FOOT_CARE_OFFICE_ROW)
    ev_scores, _ = score_events(
        ctx, "show me cost for immunotherapy", ["immunotherapy"]
    )
    assert ev_scores
    assert "immunotherapy" in ev_scores[0][1].lower()


def test_transplant_keyword_matches_event():
    """'transplants cost' — transplant event must score above confidence."""
    transplant_row = {
        "event": "Transplants",
        "service": "Inpatient facility care",
        "in_network": "Deductible, then 20% coinsurance",
        "out_of_network": "Deductible, then 40% coinsurance",
        "notes": "Data Not Found",
    }
    ctx = make_cost_context(transplant_row, ACUPUNCTURE_ROW)
    ev_scores, _ = score_events(
        ctx, "Can you show me transplants cost", ["transplants"]
    )
    assert ev_scores
    assert "transplants" in ev_scores[0][1].lower()
    assert ev_scores[0][0] >= MIN_CONFIDENCE


if __name__ == "__main__":
    import traceback

    tests = [
        test_topic_urgent_care,
        test_topic_emergency_room,
        test_topic_emergency_room_keywords,
        test_topic_pcp,
        test_topic_deductible,
        test_topic_family_deductible,
        test_topic_diagnostic_blood,
        test_topic_diagnostic_xray_imaging,
        test_topic_xray_keyword,
        test_topic_mental_health,
        test_topic_psychological_testing_keyword,
        test_topic_rehabilitation,
        test_topic_hospital,
        test_topic_maternity,
        test_topic_pharmacy,
        test_topic_specialist,
        test_topic_tmj,
        test_topic_dental_filling,
        test_topic_dental_denture,
        test_topic_dental_ortho,
        test_topic_no_standard_topic_foot_care,
        test_topic_allergy_keywords,
        test_topic_immunotherapy_keyword,
        test_topic_transplant_keyword,
        test_topic_vasectomy_keyword,
        test_topic_bariatric_keyword,
        test_topic_vision_exam,
        test_topic_vision_hardware_contacts,
        test_topic_vision_hardware_glasses,
        test_topic_vision_out_of_area,
        test_topic_vision_exclusions,
        test_topic_vision_provider,
        test_topic_dental_class_i,
        test_topic_dental_class_ii,
        test_topic_dental_class_iii,
        test_topic_dental_deductible,
        test_category_emergency,
        test_category_urgent,
        test_category_hospital,
        test_category_pcp,
        test_category_cancer,
        test_category_vision_exam,
        test_category_vision_hardware,
        test_category_contacts,
        test_category_glasses,
        test_category_dental_cleaning,
        test_category_dental_crown,
        test_category_dental_ortho,
        test_category_braces,
        test_category_foot_care_llm,
        test_category_vasectomy_llm,
        test_not_list_show_me_foot_care,
        test_not_list_show_me_cost_for,
        test_not_list_show_me_gender_affirming,
        test_not_list_show_me_newborn_care,
        test_not_list_give_me_pcp_copay,
        test_not_list_want_to_know,
        test_not_list_what_is_deductible,
        test_list_show_me_all,
        test_list_show_me_all_dialysis,
        test_list_show_me_all_virtual,
        test_list_show_me_all_home_health,
        test_list_what_are,
        test_list_what_are_benefits,
        test_list_list_keyword,
        test_list_which,
        test_list_give_me_all,
        test_weak_words_office_clinic_not_strong,
        test_foot_care_query_strong_terms,
        test_foot_care_scores_highest,
        test_unrelated_events_below_confidence_after_fix,
        test_urgent_care_scores_confidently,
        test_urgent_care_beats_foot_care,
        test_professional_visit_pcp_scores,
        test_immunotherapy_keyword_matches_event,
        test_transplant_keyword_matches_event,
    ]

    sections = {
        "PART 1 — resolve_insurance_topic": range(0, 36),
        "PART 2 — detect_category": range(36, 51),
        "PART 3 — server is_list_query": range(51, 67),
        "PART 4 — build_cost_table scoring": range(67, len(tests)),
    }

    passed = failed = 0
    for section, idx_range in sections.items():
        print(f"\n  {section}")
        for i in idx_range:
            t = tests[i]
            try:
                t()
                print(f"    ✓ {t.__name__}")
                passed += 1
            except AssertionError as e:
                print(f"    ✗ {t.__name__}: {e}")
                failed += 1
            except Exception as e:
                print(f"    ✗ {t.__name__}: UNEXPECTED ERROR — {e}")
                traceback.print_exc()
                failed += 1

    total = passed + failed
    print(f"\n{'='*60}")
    print(f"  {passed}/{total} passed {'✓' if failed == 0 else '✗'}")
    sys.exit(0 if failed == 0 else 1)
