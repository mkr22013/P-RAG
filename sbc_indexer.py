import os

"""
SBC (Summary of Benefits and Coverage) booklet indexer.
Handles QA, cost table and excluded-services sections.
Uses docling for PDF → markdown conversion, then parses the markdown.
"""
import re
import json as json_lib
import ollama

from datetime import datetime
from dotenv import load_dotenv
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableStructureOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from utils import get_smart_keywords

# Docling converter — initialised once so the ML model is loaded only once per run.
_converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=PdfPipelineOptions(
                table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST)
            ),
            backend=PyPdfiumDocumentBackend,
        )
    }
)


def _pdf_to_markdown(pdf_path):
    """Convert a PDF to markdown using docling. Returns empty string on failure."""
    try:
        return _converter.convert(pdf_path).document.export_to_markdown() or ""
    except Exception as e:
        print(f"[!] Docling conversion failed for {pdf_path}: {e}")
        return ""


load_dotenv()

CURRENT_YEAR_INT = datetime.now().year


def classify_document(pdf_path):
    """Extract plan identity from an SBC PDF using docling markdown."""
    md_content = _pdf_to_markdown(pdf_path)
    if not md_content:
        return None
    try:
        header_snippet = md_content[:6500].strip()

        prompt = f"""
            ACT AS A STRICT STRUCTURED DATA EXTRACTOR.

            Extract ONLY if explicitly present in the text.

            Rules:

            1. year:
            - Extract from "Coverage Period"

            2. type:
            - ONLY extract if "Plan Type: <VALUE>" exists
            - Allowed: HMO, PPO, EPO, HSA

            3. tier:
            - Extract from plan title (Gold, Silver, Bronze, Catastrophic)

            4. product_line:
            - Extract plan name after "Premera Blue Cross:"
            - Remove metal tier words

            5. variant:
            - Extract modifiers like Standard, CSR, etc
            - Else return "Standard"

            6. network:
            - ONLY extract if explicitly labeled (e.g. "Network: Sherwood")
            - DO NOT infer from provider names
            - If not found → return null

            RETURN STRICT JSON ONLY.

            TEXT:
            {header_snippet}
            """

        response = ollama.generate(
            model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            prompt=prompt,
            format="json",
            options={"temperature": 0},
        )

        data = json_lib.loads(response["response"])

        return {
            "year": int(re.sub(r"\D", "", str(data.get("year", CURRENT_YEAR_INT)))),
            "type": str(data.get("type", "")).strip().upper(),
            "tier": str(data.get("tier", "Gold")).strip().capitalize(),
            "product_line": str(data.get("product_line", "Plan")).strip(),
            "variant": str(data.get("variant", "Standard")).strip(),
            "network": str(data.get("network", "Standard Network")).strip(),
        }
    except Exception as e:
        print(f"[!] Dynamic classification failed: {e}")
        return None


def generate_sub_index(sub_index_path, pdf_path):
    """Parse docling markdown into structured QA, cost, and excluded-service chunks."""
    sub_index = []
    seen_keys = set()
    current_event = ""
    pending_service = (
        ""  # service name from a non-table plain-text line (page-break artefact)
    )

    # ── helpers ──────────────────────────────────────────────────────────────
    def n(v):
        s = re.sub(r"\s+", " ", str(v or "")).strip()
        return re.sub(r"^\*+|\*+$|^_+|_+$", "", s).strip()  # strip markdown bold/italic

    COST_RE = re.compile(r"%|\$|no charge|not covered", re.I)
    LIMIT_RE = re.compile(
        r"limited to|prior auth|penalty|only covered|covers up to|dispensed|"
        r"calendar year|lifetime|none|deductible applies|copay waived|"
        r"cost sharing|you may have to pay|ask your provider|"
        r"depending on the type|maternity care|more information about",
        re.I,
    )
    MULTI_TIER_RE = re.compile(r"kinwell|all other|retail|mail", re.I)

    NOISE_EXACT = {
        "coinsurance",
        "copay",
        "copayment",
        "common",
        "services",
        "event",
        "medical event",
        "surgery",
        "information",
    }
    NOISE_PHRASES = (
        "services you may need",
        "common medical event",
        "what you will pay",
        "you will pay the least",
        "you will pay the most",
        "limitations, exceptions",
        "important questions",
        "why this matters",
        "network provider",
        "out-of-network provider",
    )

    def noise(s):
        t = n(s).lower().strip(".,;: ")
        # Long cells and questions/answers are never column headers
        if len(t) > 80 or t.endswith("?") or re.match(r"^(yes|no)[\s.,]", t):
            return False
        return (
            not t
            or t in NOISE_EXACT
            or any(p in t for p in NOISE_PHRASES)
            or ("limitations" in t and "exceptions" in t)
            or re.fullmatch(r"-{3,}", t)
        )

    def add(topic, content, cat):
        key = json_lib.dumps(content, sort_keys=True)
        if key not in seen_keys:
            seen_keys.add(key)
            sub_index.append(
                {
                    "topic": topic,
                    "category": cat,
                    "benefit_category": "medical",
                    "content": content,
                    "keywords": get_smart_keywords(content),
                }
            )

    def cells(line):
        """Split a markdown table row into clean, non-noise cells."""
        cols = [n(c) for c in line.split("|")]
        if all(re.fullmatch(r"-{3,}", c) for c in cols if c):
            return None  # separator row
        # A cell passes if it's not noise, OR if it contains cost data —
        # some page-continuation headers embed cost values alongside header text
        # (e.g. "Network Provider (You will pay the least) 20% coinsurance")
        # and we need those cost values even though the cell is otherwise noisy.
        return [c for c in cols if c and (COST_RE.search(c) or not noise(c))]

    # ── section boundaries ────────────────────────────────────────────────────
    md_content = _pdf_to_markdown(pdf_path)
    if not md_content:
        with open(sub_index_path, "w") as f:
            json_lib.dump([], f)
        return []
    md = re.sub(r"<!--.*?-->", "", md_content, flags=re.DOTALL)
    lines = [l.strip() for l in md.split("\n") if l.strip()]

    qa_start = qa_end = cost_start = cost_end = excl_start = None
    for i, line in enumerate(lines):
        ll = line.lower()
        if "important questions" in ll and qa_start is None:
            qa_start = i
        if qa_start and "what you will pay" in ll and qa_end is None:
            qa_end = i
        if "what you will pay" in ll and cost_start is None:
            cost_start = i
        if cost_start and "excluded services" in ll and cost_end is None:
            cost_end = i
        if "excluded services" in ll and excl_start is None:
            excl_start = i

    # ── QA section ───────────────────────────────────────────────────────────
    Q_PREFIXES = (
        "are there",
        "what is",
        "what are",
        "do you",
        "will you",
        "is there",
        "does this",
    )

    for line in lines[qa_start or 0 : qa_end or 0]:
        if "|" not in line:
            continue
        row = cells(line)
        if not row:
            continue

        q = next((c for c in row if "?" in c), "")
        if not q:
            # Docling sometimes truncates the question cell (drops the "?").
            # If a cell starts with a known SBC question prefix, treat it as the question.
            q = next(
                (c for c in row if any(c.lower().startswith(p) for p in Q_PREFIXES)),
                "",
            )
            if q and not q.endswith("?"):
                q += "?"
        if not q:
            continue

        # Docling sometimes merges the answer into the question cell:
        # "Do you need a referral to see a specialist?  No."
        # Split on "?" and promote the trailing text to answer if no answer exists yet.
        original_q_cell = q  # save before split — used to exclude from rest
        trailing_ans = ""
        if "?" in q:
            parts = q.split("?", 1)
            q = parts[0].strip() + "?"
            trailing_ans = parts[1].strip()

        # Exclude the original (pre-split) cell so "No." isn't re-included as answer
        rest = sorted([c for c in row if c != original_q_cell], key=len)
        if not rest and not trailing_ans:
            continue

        if trailing_ans:
            # Answer was merged into question cell ("...specialist?  No.")
            # trailing_ans IS the answer; whatever remains is the explanation.
            ans = trailing_ans
            expl = rest[0] if rest else ""
        elif len(rest) == 1:
            c0 = rest[0]
            if re.match(r"^(yes|no)[\s.,]", c0, re.I):
                ans, expl = c0, ""
            elif len(c0) > 80 or re.search(
                r"\b(you must|you may|you will|you can|this plan|if you|generally)\b",
                c0,
                re.I,
            ):
                ans, expl = "", c0
            else:
                ans, expl = c0, ""
        else:
            ans, expl = rest[0], rest[-1]

        add(q, {"question": q, "answer": ans, "explanation": expl}, "qa")

    # ── cost section ─────────────────────────────────────────────────────────
    pending_in_net = ""
    pending_out_net = ""

    for line in lines[cost_start or 0 : cost_end or 0]:
        if "|" not in line:
            # Non-table line: update event OR capture service name for next row
            s = re.sub(r"^[*_#>\s]+|[*_]+$", "", line.strip()).strip()
            if re.match(r"if you\b", s, re.I):
                current_event = s
                pending_service = ""
            elif (
                s
                and not noise(s)
                and len(s) < 80
                and not re.match(r"^\d+%|^\$|^not\s+covered", s, re.I)
            ):
                pending_service = s
            continue

        row = cells(line)
        if not row:
            continue

        # Event cell — update and remove from row; any remainder is a service name
        for c in list(row):
            if c.lower().startswith("if you"):
                parts = re.split(r"\n|(?<=\w)\s{2,}", c)
                current_event = parts[0].strip()
                row.remove(c)
                row.extend(
                    p.strip() for p in parts[1:] if p.strip() and not noise(p.strip())
                )
                break

        # Classify remaining cells
        cost_cells, other_cells = [], []
        for c in row:
            # LIMIT_RE takes priority — limitation text often contains % (e.g. "0% coinsurance
            # at Kinwell Clinics. Deductible applies.") which would wrongly match COST_RE first.
            if LIMIT_RE.search(c.lower()) or len(c) > 100:
                other_cells.append(c)
            elif COST_RE.search(c):
                # Multi-tier cells (Kinwell/retail/mail) must stay whole — never split them.
                if not MULTI_TIER_RE.search(c) and not re.search(r"\$\d", c):
                    # Merged service+cost: "Imaging (CT/PET scans, MRIs) 20% coinsurance"
                    # Split prefix before first cost token → service candidate.
                    m = re.search(r"(\d+%|\$\d|no charge|not covered)", c, re.I)
                    if m and m.start() > 0:
                        prefix = c[: m.start()].strip()
                        if prefix and not noise(prefix):
                            other_cells.append(prefix)
                        c = c[m.start() :].strip()
                    # Guard A: two % tokens in one cell → in_network + out_of_network
                    toks = re.findall(
                        r"(?:not\s+covered|\d+%[^%$\n]*?(?:coinsurance|copay[^\s]*)?)",
                        c,
                        re.I,
                    )
                    if len(toks) >= 2:
                        cost_cells.extend(t.strip() for t in toks[:2])
                        continue
                cost_cells.append(c)
            else:
                other_cells.append(c)

        in_net = cost_cells[0] if cost_cells else ""
        out_net = cost_cells[1] if len(cost_cells) > 1 else ""

        # Inject pending service when this row has no service cell
        if not other_cells and pending_service:
            other_cells = [pending_service]

        if not other_cells:
            # Cost-only row: save costs so the next service row with empty costs can use them.
            if cost_cells:
                pending_in_net = in_net
                pending_out_net = out_net
            continue

        # Apply pending costs BEFORE clearing (row has service but may have no costs)
        if not in_net and pending_in_net:
            in_net = pending_in_net
            out_net = out_net or pending_out_net

        # Clear all pending state — this row owns its data now
        pending_service = pending_in_net = pending_out_net = ""

        # Service = shortest multi-word/slash cell that isn't clearly limitation text.
        # Use LIMIT_RE only to exclude limitation-like cells from being selected as
        # the service name (e.g. "None None", long prose) — not to classify content.
        # Limitations = everything else, dumped as-is.
        svc_pool = [c for c in other_cells if not LIMIT_RE.search(c.lower())]
        if svc_pool:
            multi = [c for c in svc_pool if " " in c or "/" in c]
            raw_service = min(multi or svc_pool, key=len)
        else:
            raw_service = min(other_cells, key=len)  # fallback if all match LIMIT_RE

        service = re.sub(
            r"\s+(?:coinsurance|copay\S*|deductible)\s*$", "", raw_service, flags=re.I
        ).strip()
        limitations = " ".join(
            c for c in other_cells if c != raw_service
        )  # exclude raw (pre-strip)

        if re.match(r"^\d+%|^\$|^not\s+covered", service, re.I):
            if not out_net:
                out_net = service
            continue

        # When out_of_network is empty and in_network starts with a dollar copay
        # (e.g. emergency care where in/out rates are identical), copy it across
        # so the LLM has the value in the right field.
        if not out_net and re.match(r"^\$\d", in_net):
            out_net = in_net

        add(
            service,
            {
                "event": current_event,
                "service": service,
                "in_network": in_net,
                "out_of_network": out_net,
                "limitations": limitations,
            },
            "cost",
        )

    # ── excluded / other services ─────────────────────────────────────────────
    # Docling only captures column 1 of the 3-column excluded-services table.
    # Use pdfplumber on the raw PDF text so all columns are captured.
    if pdf_path:
        import pdfplumber

        section = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "excluded services" not in text.lower():
                    continue
                for line in text.split("\n"):
                    ll = line.lower().strip()
                    if "your rights to continue coverage" in ll:
                        break
                    if "does not cover" in ll:
                        section = "not_covered"
                    elif "other covered services" in ll:
                        section = "covered"
                    elif section and re.match(r"^[•\-\u2022]", line.strip()):
                        item = n(re.sub(r"^[•\-\u2022]\s*", "", line.strip()))
                        if item and not noise(item):
                            add(item, {"type": section, "service": item}, "excluded")
    else:
        # Fallback to markdown parsing (no pdf_path provided)
        section = ""
        for line in lines[excl_start or 0 :]:
            ll = line.lower()
            if "your rights to continue coverage" in ll:
                break
            if "does not cover" in ll:
                section = "not_covered"
            elif "other covered services" in ll:
                section = "covered"
            elif section and re.match(r"^[-•\*]", line):
                item = n(line.lstrip("•-* "))
                if item and not noise(item):
                    add(item, {"type": section, "service": item}, "excluded")

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)

    return sub_index


# ═══════════════════════════════════════════════════════
# MEDICAL BENEFITS BOOKLET — classify + index functions
# ═══════════════════════════════════════════════════════
