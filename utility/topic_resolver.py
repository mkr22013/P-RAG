import re
from .utils import smart_match, NOISE_WORDS


def resolve_insurance_topic(query_words, full_query_text, p_type=None):
    """
    Resolves topics and extracts clean keywords even with typos.
    """
    topics = []
    extracted_keywords = []
    query_lower = full_query_text.lower()

    def add_keyword(phrase):
        phrase = phrase.lower().strip()
        if phrase not in extracted_keywords:
            extracted_keywords.append(phrase)

    # 1. DEDUCTIBLE / OOP
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["deductible", "limit", "oop", "coinsurance"]
    ):
        # Check for out-of-pocket specifically
        if any(
            smart_match(p, query_words, query_lower)
            for p in ["out of pocket", "out-of-pocket", "oop", "pocket"]
        ):
            topics.append("out-of-pocket")
            add_keyword("out of pocket")
        else:
            topics.append("deductible")
            add_keyword("deductible")

    # 2. URGENT vs EMERGENCY
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
        if smart_match("emergency room", query_words, query_lower):
            add_keyword("emergency room")
        else:
            add_keyword("emergency")

    # 3. DIAGNOSTIC / IMAGING (Fuzzy Safe)
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

    # 4. NETWORK
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
            # Single-word keywords so server INFO loop can match against event names
            # and chunk_keywords. "Exclusions And Limitations" and "Referrals" entries
            # both contain "exclusions"/"participating"/"limitations" as standalone words.
            add_keyword("exclusions")
            add_keyword("participating")
            add_keyword("limitations")
            add_keyword("referrals")
            # Suppress COST for out-of-network queries (no D-codes apply)
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
            "examination",
            "checkup",
            "check-up",
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

        if any(
            smart_match(w, query_words, query_lower) for w in CLASS_I_TERMS
        ) or CLASS_I_RE.search(query_lower):
            topics.append("class i")
            add_keyword("class i diagnostic and preventive services")
            # Willamette: also add D-code compatible keyword
            add_keyword("diagnostic and preventive")
            for term in CLASS_I_TERMS:
                if smart_match(term, query_words, query_lower):
                    add_keyword(term)

        # Dental procedure synonyms — maps plain-language terms to
        # D-code service names for Willamette index compatibility
        _DENTAL_SYNONYMS = {
            "cleaning": ["prophylaxis"],
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
            # Add the specific matched term as keyword
            for term in CLASS_II_TERMS:
                if smart_match(term, query_words, query_lower):
                    add_keyword(term)

        if any(
            smart_match(w, query_words, query_lower) for w in CLASS_III_TERMS
        ) or CLASS_III_RE.search(query_lower):
            topics.append("class iii")
            add_keyword("class iii major services")
            # Add the specific matched term as keyword so the server
            # ranks that procedure's entry highest (e.g. "implant" → implant entry)
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

        # Office visit / copay queries (primarily Willamette-style plans)
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

        # Broad dental list query (no specific class/procedure found)
        # → add all class topics so server fetches from every class.
        # Safe for Willamette too: class i/ii/iii topics score near-zero
        # against D-code entries so scoring falls back to keyword matching,
        # which works the same as the LLM path would have.
        if not topics and any(
            w in query_lower for w in ["all", "list", "every", "covered services"]
        ):
            topics.extend(["class i", "class ii", "class iii"])
            add_keyword("class i diagnostic and preventive services")
            add_keyword("class ii basic services")
            add_keyword("class iii major services")

        # No fallback — generic "dental" query goes to LLM

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
        # No fallback — generic "vision" query goes to LLM

    # 7. PRIMARY / SPECIALIST
    if smart_match("primary care", query_words, query_lower) or any(
        smart_match(w, query_words, query_lower)
        for w in ["pcp", "primary", "physician"]
    ):
        topics.append("Professional Visit Copay")
        add_keyword("Professional Visit Copay")
    if smart_match("specialist", query_words, query_lower):
        topics.append("specialist")
        add_keyword("specialist visit")

    # 8. MENTAL HEALTH
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

    # 9. MATERNITY
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["pregnant", "maternity", "delivery", "prenatal"]
    ):
        topics.append("maternity")

    # 10. HOSPITAL
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["hospital", "nursing", "facility"]
    ):
        topics.append("hospital")

    # 12. PHARMACY
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["drug", "prescription", "pharmacy", "generic", "brand", "specialty"]
    ):
        topics.append("pharmacy")

    # 13. REHABILITATION
    if any(
        smart_match(w, query_words, query_lower)
        for w in ["rehab", "therapy", "physical", "speech", "occupational"]
    ):
        topics.append("rehabilitation")
        if smart_match("physical therapy", query_words, query_lower):
            add_keyword("physical therapy")
        if smart_match("speech therapy", query_words, query_lower):
            add_keyword("speech therapy")
        if smart_match("occupational therapy", query_words, query_lower):
            add_keyword("occupational therapy")

    # 14. OTHER FLAGS
    if smart_match("referral", query_words, query_lower):
        topics.append("referral")
    if smart_match("authorization", query_words, query_lower):
        topics.append("prior authorization")

    print(f"[*] Resolved Topics : {topics}")
    print(f"[*] Extracted Keywords : {extracted_keywords}")

    # ── Residual keyword extraction (at end of resolve_insurance_topic) ───────
    RESIDUAL_STOP = NOISE_WORDS

    already_captured = " ".join(topics + extracted_keywords).lower()

    for word in query_words:
        if len(word) > 3 and word not in RESIDUAL_STOP and word not in already_captured:
            extracted_keywords.append(word)

    # Dental overview detection: broad "what does my plan cover" queries
    # that produce no specific topic get expanded to all class topics.
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
    # Gate 4: no medical/vision words — prevents "my dental and medical plan" confusion
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
        and p_type == "dental"
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

    # Sort for consistent cache keys regardless of word order
    extracted_keywords = sorted(set(extracted_keywords))
    topics = sorted(set(topics))

    print(f"[*] Resolved Topics After Residual Extraction: {topics}")
    print(f"[*] Extracted Keywords After Residual Extraction: {extracted_keywords}")

    return {
        "topics": list(set(topics)),
        "keywords": list(set(extracted_keywords)),
    }
