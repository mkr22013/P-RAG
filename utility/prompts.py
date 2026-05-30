BENEFIT_SELECTION_PROMPT = """
    Can you please let me know if you are looking for Medical, Dental, or Vision benefits?

    Please select from the following options:
    • Medical (Doctor visits, prescriptions, hospital stays)
    • Dental (Cleanings, X-rays, orthodontics)
    • Vision (Eye exams, glasses, contact lenses)

    Please type your selection below to get started.
    """

MEDICAL_DETAIL_PROMPT = """
    I see you're interested in your Medical benefits! To give you the right details, what specifically are you looking for?

    Please select or type an option:
    • Deductibles & Out-of-Pocket Max
    • Emergency Room (ER) or Urgent Care costs
    • X-Rays, Lab Work, or Imaging
    • Office Visit Copays (PCP or Specialist)

    What would you like to check first?
    """

DENTAL_DETAIL_PROMPT = """
    Great, let's look at your Dental coverage. What specific information do you need?

    Please select or type an option:
    • Preventive Care (Cleanings & Exams)
    • Orthodontics (Braces or Aligners)
    • Basic Services (Fillings or Extractions)
    • Major Services (Crowns, Bridges, or Dentures)

    Which of these can I help you with?
    """

VISION_DETAIL_PROMPT = """
    I can certainly help with your Vision benefits! Which part of your coverage are you curious about?

    Please select or type an option:
    • Routine Eye Exams
    • Eyeglass Frames & Lenses
    • Contact Lens Allowance
    • Laser Vision Correction (LASIK)

    Please type your selection below to see your benefits.
    """

TOPIC_EXTRACTION_PROMPT = """
    You are a medical insurance classification assistant.

    Your task is to extract:
    1. "topics" → benefit-level categories (NOT plan types)
    2. "keywords" → specific services mentioned

    ----------------------------------------
    STRICT RULES:
    ----------------------------------------

    1. DO NOT return these as topics:
    - medical
    - dental
    - vision

    These are PLAN TYPES, not benefit topics.

    2. Topics should describe SPECIFIC BENEFITS such as:
    - preventive care
    - emergency care
    - imaging
    - office visits
    - hospital services
    - pharmacy
    - rehabilitation

    (These are examples, not a fixed list.)

    3. DO NOT invent vague categories like:
    - other
    - other medical
    - general
    - miscellaneous

    4. If no clear topic exists → return:
    "topics": ["UNKNOWN"]

    5. Return canonical benefit names as they appear in the booklet.
    For vision queries use: "vision hardware", "vision exams", "out-of-area care",
    "exclusions and limitations", "selecting a vision care provider".
    Example: "what does my vision plan cover" →
    {"topics": ["vision hardware", "vision exams"], "keywords": ["vision hardware", "vision exams"]}

    5. ALWAYS extract keywords (critical for retrieval)

    ----------------------------------------
    GUIDELINES:
    ----------------------------------------

    - Prefer specific topics over generic ones
    - Use 1-2 words when possible
    - Avoid combining multiple topics into one phrase
    Wrong: "emergency urgent care"
    Right: "emergency", "urgent care"

    ----------------------------------------
    EXAMPLES:
    ----------------------------------------

    User: "allergy testing and treatment cost"
    Output:
    {"topics": ["preventive care"], "keywords": ["allergy testing", "treatment"]}

    User: "emergency room cost"
    Output:
    {"topics": ["emergency"], "keywords": ["emergency room"]}

    User: "x ray and blood work"
    Output:
    {"topics": ["imaging"], "keywords": ["x ray", "blood work"]}

    User: "tmj care"
    Output:
    {"topics": ["UNKNOWN"], "keywords": ["tmj", "temporomandibular joint"]}

    User: "transplant cost"
    Output:
    {"topics": ["UNKNOWN"], "keywords": ["transplant"]}

    ----------------------------------------
    Return strictly valid JSON.
    """