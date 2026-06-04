WELCOME_MESSAGE = """👋 Hi! I'm your **Premera Insurance Plan Assistant**.

I can answer specific questions about your **Medical**, **Dental**, and **Vision** benefits.

Here are some examples to get you started:

🏥 **Medical**
• *"What is my PCP copay?"*
• *"How much is an ER visit?"*
• *"What is my deductible?"*

🦷 **Dental**
• *"How much is a teeth cleaning?"*
• *"What does a crown cost?"*
• *"What is my dental annual maximum benefit?"*

👁️ **Vision**
• *"What is my vision exam copay?"*
• *"How much is my glasses allowance?"*

What would you like to know?"""

GUIDANCE_NO_CATEGORY = """I'm not sure what area of your plan that relates to. I can help with specific questions about your benefits.

Here are some examples:

🏥 **Medical** — *"What is my PCP copay?"* · *"What is my deductible?"* · *"How much is urgent care?"*

🦷 **Dental** — *"How much is a cleaning?"* · *"Are crowns covered?"* · *"What is my dental annual maximum benefit?"*

👁️ **Vision** — *"What is my vision exam cost?"* · *"How much is my glasses allowance?"*

Try asking a specific question and I'll pull up your exact benefits."""

GUIDANCE_CONVERSATIONAL = """I'm your Premera Insurance Plan Assistant — I'm here to help with questions about your benefits.

Try asking something like:

🏥 **Medical** — *"What is my PCP copay?"* · *"What is my deductible?"*

🦷 **Dental** — *"How much is a cleaning?"* · *"What does a crown cost?"*

👁️ **Vision** — *"What is my vision exam cost?"* · *"How much is my glasses allowance?"*"""

GUIDANCE_MEDICAL_VAGUE = """I can help with your **medical benefits**! To get your exact costs, try asking something specific:

• *"What is my PCP copay?"*
• *"How much does an ER visit cost?"*
• *"What is my deductible?"*
• *"What does a specialist visit cost?"*
• *"How much is urgent care?"*
• *"What is my out-of-pocket maximum?"*

The more specific your question, the more precise the answer I can give you."""

GUIDANCE_DENTAL_VAGUE = """I can help with your **dental benefits**! To get your exact costs, try asking something specific:

• *"How much is a teeth cleaning?"*
• *"What does a crown cost?"*
• *"Are fillings covered?"*
• *"What is my annual maximum benefit?"*
• *"How much is a root canal?"*
• *"Is orthodontic treatment covered?"*

The more specific your question, the more precise the answer I can give you."""

GUIDANCE_VISION_VAGUE = """I can help with your **vision benefits**! To get your exact costs, try asking something specific:

• *"What is my vision exam copay?"*
• *"How much is my glasses allowance?"*
• *"Are contact lenses covered?"*
• *"What is my out-of-network vision cost?"*
• *"What vision services are excluded?"*

The more specific your question, the more precise the answer I can give you."""

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

# Keep old prompts for backwards compatibility
BENEFIT_SELECTION_PROMPT = GUIDANCE_NO_CATEGORY
MEDICAL_DETAIL_PROMPT = GUIDANCE_MEDICAL_VAGUE
DENTAL_DETAIL_PROMPT = GUIDANCE_DENTAL_VAGUE
VISION_DETAIL_PROMPT = GUIDANCE_VISION_VAGUE
