import re
from .utils import smart_match, NOISE_WORDS

# ── Helpers ───────────────────────────────────────────────────────────────────


def _exact(word: str, query_words: list) -> bool:
    """
    Exact word match only — no fuzzy.
    Use for medical topics where fuzzy matching causes cross-contamination.
    e.g. smart_match("vasectomy") fuzzy-fires on "reconstruction" — exact match prevents this.
    """
    return word in query_words


def _exact_phrase(phrase: str, query_lower: str) -> bool:
    """Exact phrase match in full query string."""
    return phrase in query_lower


def _detect_emergency(
    query_words: list, query_lower: str, topics: list, add_keyword
) -> None:
    """
    Shared emergency detection — called by ALL category resolvers.

    Emergency queries can appear in any category:
    - Medical:  ER visits, ambulance
    - Dental:   after-hours emergency visit (D9440 copay)
    - Vision:   eye injury (rare)

    Previously this fired for ALL queries because the single resolve_insurance_topic
    function ran all sections for every query regardless of category. Splitting into
    4 functions caused a regression — dental emergency queries lost the "emergency"
    and "after hours" keywords which were needed to score the D9440 chunk.

    Solution: extract as a shared helper called by every resolver.
    """
    if smart_match("urgent care", query_words, query_lower) or (
        smart_match("urgent", query_words, query_lower)
        and any(
            smart_match(w, query_words, query_lower)
            for w in ["care", "clinic", "center"]
        )
    ):
        topics.append("urgent care")
        add_keyword("urgent care")
    elif (
        re.search(r"\ber\b", query_lower)
        or smart_match("emergency", query_words, query_lower)
        or smart_match("ambulance", query_words, query_lower)
    ):
        topics.append("emergency")
        add_keyword("emergency room")
        add_keyword("emergency")
        add_keyword("after hours")  # Dental: needed for D9440 after-hours visit chunk


def _build_result(topics: list, extracted_keywords: list, query_words: list) -> dict:
    """
    Common post-processing: residual keyword extraction + dedup + sort.
    Called by all 4 category resolvers before returning.
    """
    RESIDUAL_STOP = NOISE_WORDS
    already_captured = " ".join(topics + extracted_keywords).lower()
    for word in query_words:
        if len(word) > 3 and word not in RESIDUAL_STOP and word not in already_captured:
            extracted_keywords.append(word)

    extracted_keywords = sorted(set(extracted_keywords))
    topics = sorted(set(topics))

    print(f"[*] Resolved Topics After Residual Extraction: {topics}")
    print(f"[*] Extracted Keywords After Residual Extraction: {extracted_keywords}")

    return {
        "topics": list(set(topics)),
        "keywords": list(set(extracted_keywords)),
    }


# ── Medical Topic Resolver ────────────────────────────────────────────────────


def resolve_medical_topic(query_words: list, full_query_text: str) -> dict:
    """
    Medical-specific topic resolver.

    Uses EXACT word matching (_exact) instead of smart_match for all medical
    topics. This prevents fuzzy cross-contamination between medical terms
    (e.g. "vasectomy" fuzzy-matching "reconstruction" via smart_match).

    smart_match is kept ONLY for deductible/OOP/emergency/diagnostic sections
    where it was already proven safe and needed for typo handling.

    New medical topics should ALWAYS use _exact() not smart_match().
    """
    topics = []
    extracted_keywords = []
    query_lower = full_query_text.lower()

    def add_keyword(phrase):
        phrase = phrase.lower().strip()
        if phrase not in extracted_keywords:
            extracted_keywords.append(phrase)

    # 1. DEDUCTIBLE / OOP — smart_match OK (short safe terms)
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["deductible", "limit", "oop", "coinsurance"]
    ):
        if any(
            smart_match(p, query_words, query_lower)
            for p in ["out of pocket", "out-of-pocket", "oop", "pocket"]
        ):
            topics.append("out-of-pocket")
            add_keyword("out of pocket")
        else:
            topics.append("deductible")
            add_keyword("deductible")

    # 2. URGENT vs EMERGENCY — shared helper
    _detect_emergency(query_words, query_lower, topics, add_keyword)

    # 3. DIAGNOSTIC / IMAGING — smart_match OK (short safe terms)
    _dental_xray_terms = ["panoramic", "bitewing", "periapical"]
    _is_dental_xray = any(
        smart_match(w, query_words, query_lower) for w in _dental_xray_terms
    )
    if (
        any(
            smart_match(w, query_words, query_lower)
            for w in ["xray", "blood", "diagnostic"]
        )
        and not re.search(r"\bclass\s+[i123]", query_lower)
        and not _is_dental_xray
    ):
        topics.append("diagnostic")
        if smart_match("blood", query_words, query_lower):
            add_keyword("blood work")
        if smart_match("xray", query_words, query_lower) or "x-ray" in query_lower:
            add_keyword("x-ray")

    if any(
        smart_match(w, query_words, query_lower)
        for w in ["mri", "scan", "imaging", "ct"]
    ):
        topics.append("imaging")
        if smart_match("mri", query_words, query_lower):
            add_keyword("mri")
        if smart_match("ct", query_words, query_lower):
            add_keyword("ct scan")

    # 4. NETWORK — smart_match OK
    _OUT_OF_NETWORK_TERMS = [
        "out of network",
        "out-of-network",
        "out of area",
        "out-of-area",
        "non participating",
        "non-participating",
        "nonparticipating",
    ]
    if any(
        smart_match(w, query_words, query_lower)
        for w in [
            "network",
            "provider",
            "balance billing",
            "nonparticipating",
            "non-participating",
            "non participating",
        ]
    ):
        topics.append("network")
        if any(smart_match(w, query_words, query_lower) for w in _OUT_OF_NETWORK_TERMS):
            add_keyword("exclusions")
            add_keyword("participating")
            add_keyword("limitations")
            add_keyword("referrals")
            if "dental exclusions" not in topics:
                topics.append("dental exclusions")

    # 7. PRIMARY / SPECIALIST — smart_match OK (short safe terms)
    if smart_match("primary care", query_words, query_lower) or any(
        smart_match(w, query_words, query_lower)
        for w in ["pcp", "primary", "physician"]
    ):
        topics.append("Professional Visit Copay")
        add_keyword("Professional Visit Copay")
    if smart_match("specialist", query_words, query_lower):
        topics.append("specialist")
        add_keyword("specialist visit")

    # 8. MENTAL HEALTH — smart_match OK
    if re.search(r"\bmental\b", query_lower) or any(
        smart_match(w, query_words, query_lower)
        for w in ["behavioral", "psychiatrist", "psychological"]
    ):
        topics.append("mental-health")
        if smart_match("psychological testing", query_words, query_lower):
            add_keyword("psychological testing")
        if smart_match("neuropsychological testing", query_words, query_lower):
            add_keyword("neuropsychological testing")
        if smart_match("mental health visit", query_words, query_lower):
            add_keyword("mental health visit")
        if smart_match("behavioral health", query_words, query_lower):
            add_keyword("behavioral health")

    # 9. MATERNITY — exact match
    if any(
        _exact(w, query_words)
        for w in ["pregnant", "maternity", "delivery", "prenatal"]
    ):
        topics.append("maternity")

    # 10. HOSPITAL — exact match
    if any(_exact(w, query_words) for w in ["hospital", "nursing", "facility"]):
        topics.append("hospital")
        # Add "hospital" as keyword ONLY when member explicitly says "hospital"
        # (not when triggered by "nursing" or "facility" — those are different
        # benefit topics and should not score against Hospital event chunks)
        # This gives Hospital → Inpatient Care chunk higher score than
        # Hospice → Inpatient Care chunk for queries like
        # "what is my coinsurance for an inpatient hospital stay?"
        # Without this, both chunks score equally on "inpatient" keyword alone
        # and Hospice can outrank Hospital due to dict order.
        if _exact("hospital", query_words) and not _exact("food", query_words):
            add_keyword("hospital")
        # Add "inpatient care" keyword when member says "inpatient" — directly
        # matches the service field "Inpatient Care" in Hospital chunk
        if _exact("inpatient", query_words):
            add_keyword("inpatient care")

    # 12. PHARMACY — exact match
    if any(
        _exact(w, query_words)
        for w in ["drug", "prescription", "pharmacy", "generic", "brand", "specialty"]
    ):
        topics.append("pharmacy")

    # 13. REHABILITATION — exact match (prevents fuzzy cross-fire)
    # smart_match caused "vasectomy" to match "reconstruction" via fuzzy
    if any(
        _exact(w, query_words)
        for w in [
            "rehab",
            "rehabilitation",
            "therapy",
            "physical",
            "speech",
            "occupational",
            "chiropractic",
            "chiropractor",
            "acupuncture",
            "acupuncturist",
        ]
    ):
        topics.append("rehabilitation")
        if _exact("physical", query_words):
            add_keyword("physical therapy")
        if _exact("speech", query_words):
            add_keyword("speech therapy")
        if _exact("occupational", query_words):
            add_keyword("occupational therapy")
        if _exact("chiropractic", query_words) or _exact("chiropractor", query_words):
            add_keyword(
                "rehabilitation therapy"
            )  # chiropractic not in index — match via event
        if _exact("acupuncture", query_words) or _exact("acupuncturist", query_words):
            add_keyword("rehabilitation therapy")

    # 14. OTHER FLAGS — exact match
    if _exact("referral", query_words):
        topics.append("referral")
    if _exact("authorization", query_words):
        topics.append("prior authorization")

    # ── NEW MEDICAL TOPICS ────────────────────────────────────────────────────
    # Add new medical topics here using _exact() or _exact_phrase() only.
    # NEVER use smart_match for new medical topics — causes cross-contamination.

    # 15. ALLERGY
    if any(_exact(w, query_words) for w in ["allergy", "allergies", "allergic"]):
        topics.append("allergy testing and treatment")
        add_keyword("allergy testing")
        add_keyword("allergy treatment")

    # 16. IMMUNOTHERAPY / CHEMOTHERAPY / RADIATION
    if any(
        _exact(w, query_words)
        for w in ["immunotherapy", "chemotherapy", "radiation", "infusion"]
    ):
        topics.append("immunotherapy")
        for kw in ["immunotherapy", "chemotherapy", "radiation", "infusion"]:
            if _exact(kw, query_words):
                add_keyword(kw)

    # 17. THERAPEUTIC INJECTIONS
    if _exact("therapeutic", query_words):
        topics.append("therapeutic injections")
        add_keyword("therapeutic injections")

    # 18. TRANSPLANTS
    if any(_exact(w, query_words) for w in ["transplant", "transplants"]):
        topics.append("transplants")
        add_keyword("transplants")

    # 19. VIRTUAL CARE / TELEHEALTH
    if any(_exact(w, query_words) for w in ["virtual", "telehealth", "telemedicine"]):
        topics.append("telemedicine")
        topics.append("virtual care")
        add_keyword("telemedicine")
        add_keyword("virtual care")

    # 20. NICOTINE
    if any(_exact(w, query_words) for w in ["nicotine", "smoking", "tobacco"]):
        topics.append("nicotine programs")
        add_keyword("nicotine habit")

    # 21. DIALYSIS
    if _exact("dialysis", query_words):
        topics.append("dialysis")
        add_keyword("dialysis")

    # 22. FOOT CARE — exact match (LLM maps to "office visits" topic)
    if any(_exact(w, query_words) for w in ["foot", "feet", "podiatry", "podiatrist"]):
        topics.append("office visits")
        add_keyword("foot care")
        add_keyword("office visit")

    # 23. HOME HEALTH CARE
    if _exact("home", query_words) and any(
        _exact(w, query_words) for w in ["health", "care"]
    ):
        topics.append("home health care")
        add_keyword("home health care")

    # 24. VASECTOMY — let LLM handle
    # LLM correctly maps: vasectomy → surgical procedures + keyword vasectomy
    # Hardcoding caused cross-contamination via smart_match fuzzy firing

    # 25. BREAST RECONSTRUCTION
    if any(
        _exact(w, query_words)
        for w in ["breast", "reconstruction", "reconstructions", "mastectomy"]
    ):
        topics.append("breast reconstruction")
        add_keyword("breast reconstruction")

    # 26. GENDER AFFIRMING CARE
    if any(_exact(w, query_words) for w in ["gender", "affirming", "transgender"]):
        topics.append("rehabilitation")
        topics.append("office visits")
        add_keyword("gender affirming care")
        add_keyword("professional service")

    # 27. NEWBORN CARE
    if (
        _exact("newborn", query_words)
        or _exact("neonatal", query_words)
        or (_exact("new", query_words) and _exact("born", query_words))
    ):
        topics.append("newborn care")
        add_keyword("newborn care")

    # 28. CLINICAL TRIALS
    if any(_exact(w, query_words) for w in ["clinical", "trial", "trials"]):
        topics.append("clinical trials")
        add_keyword("clinical trials")

    # 29. MEDICAL TRANSPORTATION
    if _exact("transportation", query_words):
        topics.append("medical transportation")
        add_keyword("medical transportation")

    # 30. BARIATRIC SURGERY
    if _exact("bariatric", query_words):
        topics.append("bariatric surgery")
        add_keyword("bariatric surgery")

    # 31. OUT-OF-POCKET MAXIMUM
    if (
        _exact_phrase("out-of-pocket", query_lower)
        or _exact_phrase("out of pocket", query_lower)
        or (
            _exact("pocket", query_words)
            and any(_exact(w, query_words) for w in ["max", "maximum"])
        )
    ):
        if "out-of-pocket" not in topics and "deductible" not in topics:
            topics.append("out-of-pocket")
            add_keyword("out of pocket maximum")

    # 32. PREVENTIVE CARE
    if _exact("preventive", query_words):
        topics.append("preventive care")
        add_keyword("preventive care")

    # 33. MEDICAL FOOD
    if _exact("food", query_words) or _exact_phrase("medical food", query_lower):
        topics.append("medical food")
        add_keyword("medical foods")

    print(f"[*] Resolved Topics : {topics}")
    print(f"[*] Extracted Keywords : {extracted_keywords}")

    return _build_result(topics, extracted_keywords, query_words)


# ── Dental Topic Resolver ─────────────────────────────────────────────────────


def resolve_dental_topic(query_words: list, full_query_text: str) -> dict:
    """
    Dental-specific topic resolver.

    Uses smart_match throughout — dental terms are short and specific
    enough that fuzzy matching is safe and needed for typo handling.
    (e.g. member types "root canal" as "rout canal" — smart_match catches it)
    """
    topics = []
    extracted_keywords = []
    query_lower = full_query_text.lower()

    def add_keyword(phrase):
        phrase = phrase.lower().strip()
        if phrase not in extracted_keywords:
            extracted_keywords.append(phrase)

    # EMERGENCY — shared helper (dental emergency = after-hours visit D9440)
    _detect_emergency(query_words, query_lower, topics, add_keyword)

    # NETWORK (shared — needed for "out of network dentist" queries)
    _OUT_OF_NETWORK_TERMS = [
        "out of network",
        "out-of-network",
        "out of area",
        "out-of-area",
        "non participating",
        "non-participating",
        "nonparticipating",
    ]
    if any(
        smart_match(w, query_words, query_lower)
        for w in [
            "network",
            "provider",
            "balance billing",
            "nonparticipating",
            "non-participating",
            "non participating",
        ]
    ):
        topics.append("network")
        if any(smart_match(w, query_words, query_lower) for w in _OUT_OF_NETWORK_TERMS):
            add_keyword("exclusions")
            add_keyword("participating")
            add_keyword("limitations")
            add_keyword("referrals")
            if "dental exclusions" not in topics:
                topics.append("dental exclusions")

    # 5. DENTAL
    if any(
        smart_match(w, query_words, query_lower)
        for w in [
            "dental",
            "tooth",
            "teeth",
            "gum",
            "cavity",
            "filling",
            "crown",
            "denture",
            "molar",
            "root canal",
            "canal",
            "implant",
            "tmj",
            "jaw",
            "bridge",
            "veneer",
            "onlay",
            "inlay",
            "orthodontic",
            "orthodontia",
            "panoramic",
            "sealant",
            "fluoride",
            "prophylaxis",
            "cleaning",
            "class i",
            "class ii",
            "class iii",
            "class 1",
            "class 2",
            "class 3",
            "apicoectomy",
            "retrograde",
            "braces",
            "aligner",
            "retainer",
            "invisalign",
        ]
    ) or re.search(r"\bclass\s+[i123]", query_lower):

        CLASS_I_TERMS = [
            "cleaning",
            "prophylaxis",
            "fluoride",
            "sealant",
            "preventive",
            "oral exam",
            "dental exam",
            "exam",
            "evaluation",
            "xray",
            "panoramic",
        ]
        CLASS_II_TERMS = [
            "filling",
            "extraction",
            "root canal",
            "canal",
            "pulp",
            "periodontic",
            "scaling",
            "basic",
        ]
        CLASS_III_TERMS = [
            "crown",
            "bridge",
            "denture",
            "implant",
            "veneer",
            "onlay",
            "inlay",
            "major",
        ]
        CLASS_I_RE = re.compile(r"\bclass\s+(i\b(?!i)|1\b|one\b)", re.I)
        CLASS_II_RE = re.compile(r"\bclass\s+(ii\b(?!i)|2\b|two\b)", re.I)
        CLASS_III_RE = re.compile(r"\bclass\s+(iii\b|3\b|three\b)", re.I)
        ORTHO_TERMS = ["braces", "orthodontic", "orthodontist", "retainer"]
        TMJ_TERMS = ["tmj", "jaw", "temporomandibular"]
        LIMIT_TERMS = [
            "annual max",
            "annual maximum",
            "benefit maximum",
            "dental deductible",
        ]

        SPECIFIC_PROCEDURES = {
            "apicoectomy": ["apicoectomy"],
            "retrograde": ["retrograde"],
        }
        for proc, kws in SPECIFIC_PROCEDURES.items():
            if smart_match(proc, query_words, query_lower):
                topics.append(proc)
                for kw in kws:
                    add_keyword(kw)

        if any(
            smart_match(w, query_words, query_lower) for w in CLASS_I_TERMS
        ) or CLASS_I_RE.search(query_lower):
            topics.append("class i")
            add_keyword("class i diagnostic and preventive services")
            add_keyword("diagnostic and preventive")
            for term in CLASS_I_TERMS:
                if smart_match(term, query_words, query_lower):
                    add_keyword(term)

        _DENTAL_SYNONYMS = {
            "cleaning": ["prophylaxis"],
            "exam": ["evaluation", "oral evaluation", "periodic", "comprehensive"],
            "dental exam": [
                "evaluation",
                "oral evaluation",
                "periodic",
                "comprehensive",
            ],
            "filling": ["amalgam", "composite", "restorative"],
            "extraction": ["erupted", "impacted"],
            "x-ray": ["radiographic", "bitewing", "periapical"],
            "xray": ["radiographic", "bitewing", "periapical"],
            "gum": ["periodontic", "gingivectomy"],
            "denture": ["removable"],
            "implant": ["endosteal"],
            "fluoride": ["varnish"],
            "bridge": ["pontic", "prosthodontics"],
            "crown": ["porcelain", "stainless"],
        }
        for term, synonyms in _DENTAL_SYNONYMS.items():
            if smart_match(term, query_words, query_lower):
                for syn in synonyms:
                    add_keyword(syn)

        if any(
            smart_match(w, query_words, query_lower) for w in CLASS_II_TERMS
        ) or CLASS_II_RE.search(query_lower):
            topics.append("class ii")
            add_keyword("class ii basic services")
            for term in CLASS_II_TERMS:
                if smart_match(term, query_words, query_lower):
                    add_keyword(term)

        if any(
            smart_match(w, query_words, query_lower) for w in CLASS_III_TERMS
        ) or CLASS_III_RE.search(query_lower):
            topics.append("class iii")
            add_keyword("class iii major services")
            for term in CLASS_III_TERMS:
                if smart_match(term, query_words, query_lower):
                    add_keyword(term)

        if any(smart_match(w, query_words, query_lower) for w in ORTHO_TERMS):
            topics.append("orthodontic treatment")
            add_keyword("orthodontic treatment")

        if any(smart_match(w, query_words, query_lower) for w in TMJ_TERMS):
            topics.append("tmj")
            add_keyword("tmj")
            add_keyword("temporomandibular")

        if any(smart_match(w, query_words, query_lower) for w in LIMIT_TERMS):
            topics.append("plan limits")
            add_keyword("plan limits")

        DENTAL_EXCLUSION_TERMS = [
            "not covered",
            "excluded",
            "exclusions",
            "limitations",
            "what is not",
            "what are not",
        ]
        if any(
            smart_match(w, query_words, query_lower) for w in DENTAL_EXCLUSION_TERMS
        ):
            topics.append("dental exclusions")
            add_keyword("exclusions")
            add_keyword("limitations")

        OFFICE_VISIT_TERMS = [
            "copay",
            "office visit",
            "general visit",
            "specialist visit",
            "visit copay",
        ]
        if any(smart_match(w, query_words, query_lower) for w in OFFICE_VISIT_TERMS):
            topics.append("office visit")
            add_keyword("office visit copayments")
            add_keyword("copay")

        if not topics and any(
            w in query_lower for w in ["all", "list", "every", "covered services"]
        ):
            topics.extend(["class i", "class ii", "class iii"])
            add_keyword("class i diagnostic and preventive services")
            add_keyword("class ii basic services")
            add_keyword("class iii major services")

    print(f"[*] Resolved Topics : {topics}")
    print(f"[*] Extracted Keywords : {extracted_keywords}")

    # Dental overview detection
    _OVERVIEW_TERMS = [
        "what does my plan cover",
        "what is covered",
        "what's covered",
        "overview",
        "summary",
        "all benefits",
        "all covered",
        "each type",
        "types of service",
        "types of coverage",
        "everything covered",
        "full coverage",
        "complete coverage",
        "services are covered",
        "services covered",
        "what services",
    ]
    _dental_words = ["dental", "tooth", "teeth", "gum", "oral", "dentist"]
    _non_dental_words = [
        "medical",
        "vision",
        "eye",
        "glasses",
        "pcp",
        "hospital",
        "pharmacy",
        "prescription",
        "specialist",
        "deductible and medical",
    ]
    _safe_dental_overview = any(w in query_lower for w in _dental_words) and not any(
        w in query_lower for w in _non_dental_words
    )
    if (
        not topics
        and _safe_dental_overview
        and any(smart_match(w, query_words, query_lower) for w in _OVERVIEW_TERMS)
    ):
        topics = ["class i", "class ii", "class iii", "plan limits"]
        for kw in [
            "class i diagnostic and preventive services",
            "class ii basic services",
            "class iii major services",
            "plan limits",
            "deductible",
            "annual maximum",
        ]:
            add_keyword(kw)
        print("[*] DENTAL OVERVIEW QUERY → expanding to all class topics")

    return _build_result(topics, extracted_keywords, query_words)


# ── Vision Topic Resolver ─────────────────────────────────────────────────────


def resolve_vision_topic(query_words: list, full_query_text: str) -> dict:
    """
    Vision-specific topic resolver.
    Uses smart_match — vision terms are short and specific.
    """
    topics = []
    extracted_keywords = []
    query_lower = full_query_text.lower()

    def add_keyword(phrase):
        phrase = phrase.lower().strip()
        if phrase not in extracted_keywords:
            extracted_keywords.append(phrase)

    # EMERGENCY — shared helper (vision emergency = eye injury)
    _detect_emergency(query_words, query_lower, topics, add_keyword)

    # 6. VISION
    if any(
        smart_match(w, query_words, query_lower) for w in ["vision", "eye", "glasses"]
    ):

        HARDWARE_TERMS = [
            "hardware",
            "contact",
            "contacts",
            "lenses",
            "lens",
            "frames",
            "eyeglass",
            "eyeglasses",
            "bifocal",
            "trifocal",
            "progressive",
            "sunglasses",
        ]
        EXAM_TERMS = [
            "eye exam",
            "vision exam",
            "eye examination",
            "optometrist",
            "optometry",
            "refraction",
        ]
        OUT_OF_AREA_TERMS = [
            "out of area",
            "out-of-area",
            "outside washington",
            "outside alaska",
            "travelling",
            "traveling",
        ]
        PROVIDER_TERMS = [
            "in-network provider",
            "vision provider",
            "vision care provider",
            "out-of-network vision",
        ]
        EXCLUSION_TERMS = ["not covered", "excluded", "exclusions and limitations"]

        if any(smart_match(w, query_words, query_lower) for w in HARDWARE_TERMS):
            topics.append("vision hardware")
            add_keyword("vision hardware")

        if any(smart_match(w, query_words, query_lower) for w in EXAM_TERMS):
            topics.append("vision exams")
            add_keyword("vision exams")

        if any(smart_match(w, query_words, query_lower) for w in OUT_OF_AREA_TERMS):
            topics.append("out-of-area care")
            add_keyword("out-of-area care")

        if any(smart_match(w, query_words, query_lower) for w in PROVIDER_TERMS):
            topics.append("selecting a vision care provider")
            add_keyword("selecting a vision care provider")

        if any(smart_match(w, query_words, query_lower) for w in EXCLUSION_TERMS):
            topics.append("exclusions and limitations")
            add_keyword("exclusions and limitations")

    print(f"[*] Resolved Topics : {topics}")
    print(f"[*] Extracted Keywords : {extracted_keywords}")

    return _build_result(topics, extracted_keywords, query_words)


# ── Rx Topic Resolver ─────────────────────────────────────────────────────────


def resolve_rx_topic(query_words: list, full_query_text: str) -> dict:
    """
    Rx-specific topic resolver.
    Rx queries are almost entirely keyword-driven (drug names).
    Topics are minimal — mostly "pharmacy" as a fallback.
    """
    topics = []
    extracted_keywords = []
    query_lower = full_query_text.lower()

    def add_keyword(phrase):
        phrase = phrase.lower().strip()
        if phrase not in extracted_keywords:
            extracted_keywords.append(phrase)

    # Rx topic — generic pharmacy fallback
    if any(
        smart_match(w, query_words, query_lower)
        for w in [
            "drug",
            "prescription",
            "pharmacy",
            "generic",
            "brand",
            "specialty",
            "formulary",
            "tier",
            "refill",
            "prior",
            "authorization",
        ]
    ):
        topics.append("pharmacy")

    print(f"[*] Resolved Topics : {topics}")
    print(f"[*] Extracted Keywords : {extracted_keywords}")

    return _build_result(topics, extracted_keywords, query_words)


# ── Main Entry Point ──────────────────────────────────────────────────────────


def resolve_insurance_topic(
    query_words: list, full_query_text: str, p_type: str | None = None
) -> dict:
    """
    Main entry point — routes to category-specific resolver.

    Architecture:
        resolve_medical_topic()  <- exact match only (safe for long medical terms)
        resolve_dental_topic()   <- smart_match OK (short specific dental terms)
        resolve_vision_topic()   <- smart_match OK (short specific vision terms)
        resolve_rx_topic()       <- minimal, keyword-driven

    Changes in one resolver CANNOT affect other categories — complete isolation.
    Add new medical topics to resolve_medical_topic() using _exact() only.
    Add new dental topics to resolve_dental_topic() using smart_match safely.
    """
    if p_type == "dental":
        return resolve_dental_topic(query_words, full_query_text)
    elif p_type == "vision":
        return resolve_vision_topic(query_words, full_query_text)
    elif p_type == "rx":
        return resolve_rx_topic(query_words, full_query_text)
    else:
        return resolve_medical_topic(query_words, full_query_text)


# # ========================================Previous working code before creating different resolve topic functions========================================

# # import re
# # from .utils import smart_match, NOISE_WORDS


# # def resolve_insurance_topic(query_words, full_query_text, p_type=None):
# #     """
# #     Resolves topics and extracts clean keywords even with typos.
# #     """
# #     topics = []
# #     extracted_keywords = []
# #     query_lower = full_query_text.lower()

# #     def add_keyword(phrase):
# #         phrase = phrase.lower().strip()
# #         if phrase not in extracted_keywords:
# #             extracted_keywords.append(phrase)

# #     # 1. DEDUCTIBLE / OOP
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["deductible", "limit", "oop", "coinsurance"]
# #     ):
# #         # Check for out-of-pocket specifically
# #         if any(
# #             smart_match(p, query_words, query_lower)
# #             for p in ["out of pocket", "out-of-pocket", "oop", "pocket"]
# #         ):
# #             topics.append("out-of-pocket")
# #             add_keyword("out of pocket")
# #         else:
# #             topics.append("deductible")
# #             add_keyword("deductible")

# #     # 2. URGENT vs EMERGENCY
# #     if smart_match("urgent care", query_words, query_lower) or (
# #         smart_match("urgent", query_words, query_lower)
# #         and any(
# #             smart_match(w, query_words, query_lower)
# #             for w in ["care", "clinic", "center"]
# #         )
# #     ):
# #         topics.append("urgent care")
# #         add_keyword("urgent care")
# #     elif (
# #         re.search(r"\ber\b", query_lower)
# #         or smart_match("emergency", query_words, query_lower)
# #         or smart_match("ambulance", query_words, query_lower)
# #     ):
# #         topics.append("emergency")
# #         # Always add "emergency room" — prevents matching "E-Visit" chunks
# #         add_keyword("emergency room")
# #         add_keyword("emergency")

# #     # 3. DIAGNOSTIC / IMAGING (Fuzzy Safe)
# #     _dental_xray_terms = ["panoramic", "bitewing", "periapical"]
# #     _is_dental_xray = any(
# #         smart_match(w, query_words, query_lower) for w in _dental_xray_terms
# #     )
# #     if (
# #         any(
# #             smart_match(w, query_words, query_lower)
# #             for w in ["xray", "blood", "diagnostic"]
# #         )
# #         and not re.search(r"\bclass\s+[i123]", query_lower)
# #         and not _is_dental_xray
# #     ):
# #         topics.append("diagnostic")
# #         if smart_match("blood", query_words, query_lower):
# #             add_keyword("blood work")
# #         if smart_match("xray", query_words, query_lower) or "x-ray" in query_lower:
# #             add_keyword("x-ray")

# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["mri", "scan", "imaging", "ct"]
# #     ):
# #         topics.append("imaging")
# #         if smart_match("mri", query_words, query_lower):
# #             add_keyword("mri")
# #         if smart_match("ct", query_words, query_lower):
# #             add_keyword("ct scan")

# #     # 4. NETWORK
# #     _OUT_OF_NETWORK_TERMS = [
# #         "out of network",
# #         "out-of-network",
# #         "out of area",
# #         "out-of-area",
# #         "non participating",
# #         "non-participating",
# #         "nonparticipating",
# #     ]
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in [
# #             "network",
# #             "provider",
# #             "balance billing",
# #             "nonparticipating",
# #             "non-participating",
# #             "non participating",
# #         ]
# #     ):
# #         topics.append("network")
# #         if any(smart_match(w, query_words, query_lower) for w in _OUT_OF_NETWORK_TERMS):
# #             # Single-word keywords so server INFO loop can match against event names
# #             # and chunk_keywords. "Exclusions And Limitations" and "Referrals" entries
# #             # both contain "exclusions"/"participating"/"limitations" as standalone words.
# #             add_keyword("exclusions")
# #             add_keyword("participating")
# #             add_keyword("limitations")
# #             add_keyword("referrals")
# #             # Suppress COST for out-of-network queries (no D-codes apply)
# #             if "dental exclusions" not in topics:
# #                 topics.append("dental exclusions")

# #     # 5. DENTAL
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in [
# #             "dental",
# #             "tooth",
# #             "teeth",
# #             "gum",
# #             "cavity",
# #             "filling",
# #             "crown",
# #             "denture",
# #             "molar",
# #             "root canal",
# #             "canal",
# #             "implant",
# #             "tmj",
# #             "jaw",
# #             "bridge",
# #             "veneer",
# #             "onlay",
# #             "inlay",
# #             "orthodontic",
# #             "orthodontia",
# #             "panoramic",
# #             "sealant",
# #             "fluoride",
# #             "prophylaxis",
# #             "cleaning",
# #             "class i",
# #             "class ii",
# #             "class iii",
# #             "class 1",
# #             "class 2",
# #             "class 3",
# #             "apicoectomy",
# #             "retrograde",
# #             "braces",
# #             "aligner",
# #             "retainer",
# #             "invisalign",
# #         ]
# #     ) or re.search(r"\bclass\s+[i123]", query_lower):
# #         CLASS_I_TERMS = [
# #             "cleaning",
# #             "prophylaxis",
# #             "fluoride",
# #             "sealant",
# #             "preventive",
# #             "oral exam",
# #             "dental exam",
# #             "exam",
# #             "evaluation",
# #             "xray",
# #             "panoramic",
# #         ]
# #         CLASS_II_TERMS = [
# #             "filling",
# #             "extraction",
# #             "root canal",
# #             "canal",
# #             "pulp",
# #             "periodontic",
# #             "scaling",
# #             "basic",
# #         ]
# #         CLASS_III_TERMS = [
# #             "crown",
# #             "bridge",
# #             "denture",
# #             "implant",
# #             "veneer",
# #             "onlay",
# #             "inlay",
# #             "major",
# #         ]
# #         CLASS_I_RE = re.compile(r"\bclass\s+(i\b(?!i)|1\b|one\b)", re.I)
# #         CLASS_II_RE = re.compile(r"\bclass\s+(ii\b(?!i)|2\b|two\b)", re.I)
# #         CLASS_III_RE = re.compile(r"\bclass\s+(iii\b|3\b|three\b)", re.I)
# #         ORTHO_TERMS = ["braces", "orthodontic", "orthodontist", "retainer"]
# #         TMJ_TERMS = ["tmj", "jaw", "temporomandibular"]
# #         LIMIT_TERMS = [
# #             "annual max",
# #             "annual maximum",
# #             "benefit maximum",
# #             "dental deductible",
# #         ]

# #         # Specific procedure handling — must come before broad class detection
# #         # so "all" in query doesn't trigger broad list fetch
# #         SPECIFIC_PROCEDURES = {
# #             "apicoectomy": ["apicoectomy"],
# #             "retrograde": ["retrograde"],
# #         }
# #         for proc, kws in SPECIFIC_PROCEDURES.items():
# #             if smart_match(proc, query_words, query_lower):
# #                 topics.append(proc)
# #                 for kw in kws:
# #                     add_keyword(kw)

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_I_TERMS
# #         ) or CLASS_I_RE.search(query_lower):
# #             topics.append("class i")
# #             add_keyword("class i diagnostic and preventive services")
# #             # Willamette: also add D-code compatible keyword
# #             add_keyword("diagnostic and preventive")
# #             for term in CLASS_I_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         # Dental procedure synonyms — maps plain-language terms to
# #         # D-code service names for Willamette index compatibility
# #         _DENTAL_SYNONYMS = {
# #             "cleaning": ["prophylaxis"],
# #             "exam": ["evaluation", "oral evaluation", "periodic", "comprehensive"],
# #             "dental exam": [
# #                 "evaluation",
# #                 "oral evaluation",
# #                 "periodic",
# #                 "comprehensive",
# #             ],
# #             "filling": ["amalgam", "composite", "restorative"],
# #             "extraction": ["erupted", "impacted"],
# #             "x-ray": ["radiographic", "bitewing", "periapical"],
# #             "xray": ["radiographic", "bitewing", "periapical"],
# #             "gum": ["periodontic", "gingivectomy"],
# #             "denture": ["removable"],
# #             "implant": ["endosteal"],
# #             "fluoride": ["varnish"],
# #             "bridge": ["pontic", "prosthodontics"],
# #             "crown": ["porcelain", "stainless"],
# #         }
# #         for term, synonyms in _DENTAL_SYNONYMS.items():
# #             if smart_match(term, query_words, query_lower):
# #                 for syn in synonyms:
# #                     add_keyword(syn)

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_II_TERMS
# #         ) or CLASS_II_RE.search(query_lower):
# #             topics.append("class ii")
# #             add_keyword("class ii basic services")
# #             # Add the specific matched term as keyword
# #             for term in CLASS_II_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         if any(
# #             smart_match(w, query_words, query_lower) for w in CLASS_III_TERMS
# #         ) or CLASS_III_RE.search(query_lower):
# #             topics.append("class iii")
# #             add_keyword("class iii major services")
# #             # Add the specific matched term as keyword so the server
# #             # ranks that procedure's entry highest (e.g. "implant" → implant entry)
# #             for term in CLASS_III_TERMS:
# #                 if smart_match(term, query_words, query_lower):
# #                     add_keyword(term)

# #         if any(smart_match(w, query_words, query_lower) for w in ORTHO_TERMS):
# #             topics.append("orthodontic treatment")
# #             add_keyword("orthodontic treatment")

# #         if any(smart_match(w, query_words, query_lower) for w in TMJ_TERMS):
# #             topics.append("tmj")
# #             add_keyword("tmj")
# #             add_keyword("temporomandibular")

# #         if any(smart_match(w, query_words, query_lower) for w in LIMIT_TERMS):
# #             topics.append("plan limits")
# #             add_keyword("plan limits")

# #         DENTAL_EXCLUSION_TERMS = [
# #             "not covered",
# #             "excluded",
# #             "exclusions",
# #             "limitations",
# #             "what is not",
# #             "what are not",
# #         ]
# #         if any(
# #             smart_match(w, query_words, query_lower) for w in DENTAL_EXCLUSION_TERMS
# #         ):
# #             topics.append("dental exclusions")
# #             add_keyword("exclusions")
# #             add_keyword("limitations")

# #         # Office visit / copay queries (primarily Willamette-style plans)
# #         OFFICE_VISIT_TERMS = [
# #             "copay",
# #             "office visit",
# #             "general visit",
# #             "specialist visit",
# #             "visit copay",
# #         ]
# #         if any(smart_match(w, query_words, query_lower) for w in OFFICE_VISIT_TERMS):
# #             topics.append("office visit")
# #             add_keyword("office visit copayments")
# #             add_keyword("copay")

# #         # Broad dental list query (no specific class/procedure found)
# #         if not topics and any(
# #             w in query_lower for w in ["all", "list", "every", "covered services"]
# #         ):
# #             topics.extend(["class i", "class ii", "class iii"])
# #             add_keyword("class i diagnostic and preventive services")
# #             add_keyword("class ii basic services")
# #             add_keyword("class iii major services")

# #         # No fallback — generic "dental" query goes to LLM

# #     # 6. VISION
# #     if any(
# #         smart_match(w, query_words, query_lower) for w in ["vision", "eye", "glasses"]
# #     ):

# #         HARDWARE_TERMS = [
# #             "hardware",
# #             "contact",
# #             "contacts",
# #             "lenses",
# #             "lens",
# #             "frames",
# #             "eyeglass",
# #             "eyeglasses",
# #             "bifocal",
# #             "trifocal",
# #             "progressive",
# #             "sunglasses",
# #         ]
# #         EXAM_TERMS = [
# #             "eye exam",
# #             "vision exam",
# #             "eye examination",
# #             "optometrist",
# #             "optometry",
# #             "refraction",
# #         ]
# #         OUT_OF_AREA_TERMS = [
# #             "out of area",
# #             "out-of-area",
# #             "outside washington",
# #             "outside alaska",
# #             "travelling",
# #             "traveling",
# #         ]
# #         PROVIDER_TERMS = [
# #             "in-network provider",
# #             "vision provider",
# #             "vision care provider",
# #             "out-of-network vision",
# #         ]
# #         EXCLUSION_TERMS = ["not covered", "excluded", "exclusions and limitations"]

# #         if any(smart_match(w, query_words, query_lower) for w in HARDWARE_TERMS):
# #             topics.append("vision hardware")
# #             add_keyword("vision hardware")

# #         if any(smart_match(w, query_words, query_lower) for w in EXAM_TERMS):
# #             topics.append("vision exams")
# #             add_keyword("vision exams")

# #         if any(smart_match(w, query_words, query_lower) for w in OUT_OF_AREA_TERMS):
# #             topics.append("out-of-area care")
# #             add_keyword("out-of-area care")

# #         if any(smart_match(w, query_words, query_lower) for w in PROVIDER_TERMS):
# #             topics.append("selecting a vision care provider")
# #             add_keyword("selecting a vision care provider")

# #         if any(smart_match(w, query_words, query_lower) for w in EXCLUSION_TERMS):
# #             topics.append("exclusions and limitations")
# #             add_keyword("exclusions and limitations")
# #         # No fallback — generic "vision" query goes to LLM

# #     # 7. PRIMARY / SPECIALIST
# #     if smart_match("primary care", query_words, query_lower) or any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["pcp", "primary", "physician"]
# #     ):
# #         topics.append("Professional Visit Copay")
# #         add_keyword("Professional Visit Copay")
# #     if smart_match("specialist", query_words, query_lower):
# #         topics.append("specialist")
# #         add_keyword("specialist visit")

# #     # 8. MENTAL HEALTH
# #     if re.search(r"\bmental\b", query_lower) or any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["behavioral", "psychiatrist", "psychological"]
# #     ):
# #         topics.append("mental-health")
# #         if smart_match("psychological testing", query_words, query_lower):
# #             add_keyword("psychological testing")
# #         if smart_match("neuropsychological testing", query_words, query_lower):
# #             add_keyword("neuropsychological testing")
# #         if smart_match("mental health visit", query_words, query_lower):
# #             add_keyword("mental health visit")
# #         if smart_match("behavioral health", query_words, query_lower):
# #             add_keyword("behavioral health")

# #     # 9. MATERNITY
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["pregnant", "maternity", "delivery", "prenatal"]
# #     ):
# #         topics.append("maternity")

# #     # 10. HOSPITAL
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["hospital", "nursing", "facility"]
# #     ):
# #         topics.append("hospital")

# #     # 12. PHARMACY
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["drug", "prescription", "pharmacy", "generic", "brand", "specialty"]
# #     ):
# #         topics.append("pharmacy")

# #     # 13. REHABILITATION
# #     if any(
# #         smart_match(w, query_words, query_lower)
# #         for w in ["rehab", "therapy", "physical", "speech", "occupational"]
# #     ):
# #         topics.append("rehabilitation")
# #         if smart_match("physical therapy", query_words, query_lower):
# #             add_keyword("physical therapy")
# #         if smart_match("speech therapy", query_words, query_lower):
# #             add_keyword("speech therapy")
# #         if smart_match("occupational therapy", query_words, query_lower):
# #             add_keyword("occupational therapy")

# #     # 14. OTHER FLAGS
# #     if smart_match("referral", query_words, query_lower):
# #         topics.append("referral")
# #     if smart_match("authorization", query_words, query_lower):
# #         topics.append("prior authorization")

# #     print(f"[*] Resolved Topics : {topics}")
# #     print(f"[*] Extracted Keywords : {extracted_keywords}")

# #     # ── Residual keyword extraction (at end of resolve_insurance_topic) ───────
# #     RESIDUAL_STOP = NOISE_WORDS

# #     already_captured = " ".join(topics + extracted_keywords).lower()

# #     for word in query_words:
# #         if len(word) > 3 and word not in RESIDUAL_STOP and word not in already_captured:
# #             extracted_keywords.append(word)

# #     # Dental overview detection: broad "what does my plan cover" queries
# #     # that produce no specific topic get expanded to all class topics.
# #     _OVERVIEW_TERMS = [
# #         "what does my plan cover",
# #         "what is covered",
# #         "what's covered",
# #         "overview",
# #         "summary",
# #         "all benefits",
# #         "all covered",
# #         "each type",
# #         "types of service",
# #         "types of coverage",
# #         "everything covered",
# #         "full coverage",
# #         "complete coverage",
# #         "services are covered",
# #         "services covered",
# #         "what services",
# #     ]
# #     _dental_words = ["dental", "tooth", "teeth", "gum", "oral", "dentist"]
# #     # Gate 4: no medical/vision words — prevents "my dental and medical plan" confusion
# #     _non_dental_words = [
# #         "medical",
# #         "vision",
# #         "eye",
# #         "glasses",
# #         "pcp",
# #         "hospital",
# #         "pharmacy",
# #         "prescription",
# #         "specialist",
# #         "deductible and medical",
# #     ]
# #     _safe_dental_overview = any(w in query_lower for w in _dental_words) and not any(
# #         w in query_lower for w in _non_dental_words
# #     )
# #     if (
# #         not topics
# #         and p_type == "dental"
# #         and _safe_dental_overview
# #         and any(smart_match(w, query_words, query_lower) for w in _OVERVIEW_TERMS)
# #     ):
# #         topics = ["class i", "class ii", "class iii", "plan limits"]
# #         for kw in [
# #             "class i diagnostic and preventive services",
# #             "class ii basic services",
# #             "class iii major services",
# #             "plan limits",
# #             "deductible",
# #             "annual maximum",
# #         ]:
# #             add_keyword(kw)
# #         print("[*] DENTAL OVERVIEW QUERY → expanding to all class topics")

# #     # Sort for consistent cache keys regardless of word order
# #     extracted_keywords = sorted(set(extracted_keywords))
# #     topics = sorted(set(topics))

# #     print(f"[*] Resolved Topics After Residual Extraction: {topics}")
# #     print(f"[*] Extracted Keywords After Residual Extraction: {extracted_keywords}")

# #     return {
# #         "topics": list(set(topics)),
# #         "keywords": list(set(extracted_keywords)),
# #     }
