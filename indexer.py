import os, sqlite3, re
import json as json_lib
import ollama
from datetime import datetime
from dotenv import load_dotenv

from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.pipeline_options import TableFormerMode

load_dotenv()
DOC_BASE_DIR = "./docs"
INDEX_OUTPUT_DIR = "./indices"
DB_PATH = os.path.join(os.path.dirname(__file__), "p_insurance_index.db")
LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)
CURRENT_YEAR_INT = datetime.now().year


def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS master_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER, plan_type TEXT, plan_tier TEXT,
            pdf_path TEXT UNIQUE, sub_index_path TEXT
        )
    """
    )
    conn.commit()
    conn.close()


def nuclear_flatten(val):
    # Fixed the topic list-to-string issue by joining if it's a list
    if isinstance(val, list):
        val = " ".join(val[:2])
    clean = re.sub(r"[^a-zA-Z0-9\s$.,%]", "", str(val))
    return clean.strip()


def get_smart_keywords(text):
    text_lower = text.lower()
    patterns = {
        "pcp": r"\bpcp\b|primary[- ]?care",
        "specialist": r"specialist",
        "in-network": r"in[- ]?network",
        "out-of-network": r"out[- ]?of[- ]?network",
        "copay": r"co[- ]?pay|copay",
        "deductible": r"deductible",
        "coinsurance": r"co[- ]?insurance",
        "emergency": r"emergency|medical[- ]?attention",
        "urgent-care": r"urgent[- ]?care",
        "pharmacy": r"pharmacy|prescription|rx",
        "dental": r"dental|dentist|ortho|braces",
        "vision": r"vision|eye|glasses",
    }
    found = [
        label for label, pattern in patterns.items() if re.search(pattern, text_lower)
    ]
    if len(found) < 10:
        backups = re.findall(r"\b\w{7,}\b", text_lower)
        for w in backups:
            if w not in found and len(found) < 10:
                found.append(w)
    return found[:10]


def classify_document(md_content):
    try:
        header_snippet = md_content[:2500].strip()
        prompt = f"Identify the 'year', 'type' (Medical, Dental, Vision), and 'tier' (Gold, Silver, Bronze). Return ONLY JSON. Text: {header_snippet}"
        response = ollama.generate(
            model=LOCAL_MODEL,
            prompt=prompt,
            format="json",
            options={"num_ctx": 4096, "temperature": 0},
        )
        data = json_lib.loads(response["response"])
        raw_year = str(data.get("year", CURRENT_YEAR_INT))
        clean_year = re.sub(r"\D", "", raw_year)
        return {
            "year": int(clean_year) if clean_year else CURRENT_YEAR_INT,
            "type": nuclear_flatten(data.get("type", "Medical")),
            "tier": nuclear_flatten(data.get("tier", "Gold")),
        }
    except Exception as e:
        return {"year": CURRENT_YEAR_INT, "type": "Medical", "tier": "Gold"}


def generate_sub_index(md_content, sub_index_path, LOCAL_MODEL="llama3.1"):
    # --- STAGE 0: BARRIER REMOVAL & STITCHING ---
    # Remove headers that split rows mid-table (fixed syntax error)
    clean_md = re.sub(r"\|? ---.*?--- \|?", "", md_content)
    clean_md = re.sub(r"\| \||\|\|", " | ", clean_md)

    # Fragment Stitcher
    raw_lines = [l.strip() for l in clean_md.split("\n") if l.strip()]
    stitched_lines = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if not re.search(r"\d|%", line) and i + 1 < len(raw_lines):
            next_l = raw_lines[i + 1]
            if re.search(r"\d|%", next_l):
                stitched_lines.append(f"{line} | {next_l}")
                i += 2
                continue
        stitched_lines.append(line)
        i += 1

    final_clean_md = "\n".join(stitched_lines)
    sub_index = []
    chunk_size = 2000
    overlap = 300
    final_chunks = [
        final_clean_md[j : j + chunk_size]
        for j in range(0, len(final_clean_md), chunk_size - overlap)
    ]

    for idx, chunk in enumerate(final_chunks):
        clean_chunk = chunk.strip()
        if len(clean_chunk) < 150:
            continue

        # --- UNIVERSAL DATA POISONING FILTER ---
        # Detect Federal Coverage Examples (Mia/Joe)
        example_markers = [
            "In this example",
            "Total Example Cost",
            "Simple Fracture",
            "would pay",
            "Patient pays",
        ]
        example_score = sum(1 for marker in example_markers if marker in clean_chunk)

        if example_score >= 2 or "language assistance" in clean_chunk.lower():
            print(f"[*] Filtering Hypothetical Example/Language Noise: Chunk {idx}")
            continue

        topic_lines = clean_chunk.split("\n")[:2]
        keywords = get_smart_keywords(clean_chunk)

        sub_index.append(
            {
                "page_number": idx,
                "topic": nuclear_flatten(topic_lines),
                "keywords": [k.lower() for k in keywords],
                "content": clean_chunk,
            }
        )

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json_lib.dump(sub_index, f, indent=4)


def build_all():
    setup_db()
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True
    pipeline_options.generate_page_images = False
    pipeline_options.generate_table_images = False
    pipeline_options.ocr_options.force_full_page_ocr = True

    # YOUR FIX: Direct assignment to bypass Docling 2.0 init errors
    table_options = TableStructureOptions(mode=TableFormerMode.ACCURATE)
    table_options.do_cell_matching = True
    pipeline_options.table_structure_options = table_options

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend
            )
        }
    )
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for root, _, files in os.walk(DOC_BASE_DIR):
        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.abspath(os.path.join(root, filename))

            cursor.execute(
                "SELECT sub_index_path FROM master_index WHERE pdf_path = ?",
                (pdf_path,),
            )
            if cursor.fetchone():
                cursor.execute(
                    "DELETE FROM master_index WHERE pdf_path = ?", (pdf_path,)
                )

            try:
                print(f"[*] Processing: {filename}...")
                result = converter.convert(pdf_path)
                md_text = result.document.export_to_markdown()
                plan_info = classify_document(md_text)

                fn = f"{plan_info['year']}_{plan_info['type']}_{plan_info['tier']}.json".lower().replace(
                    " ", "_"
                )
                sub_index_path = os.path.abspath(os.path.join(INDEX_OUTPUT_DIR, fn))

                generate_sub_index(md_text, sub_index_path)
                cursor.execute(
                    "INSERT INTO master_index (year, plan_type, plan_tier, pdf_path, sub_index_path) VALUES (?,?,?,?,?)",
                    (
                        plan_info["year"],
                        plan_info["type"],
                        plan_info["tier"],
                        pdf_path,
                        sub_index_path,
                    ),
                )
                print(f"✅ SUCCESS: {filename}")
            except Exception as e:
                print(f"❌ FAILED {filename}: {e}")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    build_all()
