import os, json, sqlite3, ollama, re
from docling.document_converter import DocumentConverter
from dotenv import load_dotenv

from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.pipeline_options import TableFormerMode # Crucial for 'mode'

load_dotenv()
DOC_BASE_DIR = "./docs"
INDEX_OUTPUT_DIR = "./indices"
DB_PATH = "p_insurance_index.db"
LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

os.makedirs(INDEX_OUTPUT_DIR, exist_ok=True)


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
    clean = re.sub(r"[^a-zA-Z0-9\s$.,%]", "", str(val))
    return clean.strip()

def classify_document(md_content):
    """Surgically extracts metadata using only the first 2000 characters."""
    try:
        # CRITICAL: We slice the text BEFORE sending it to the function logic
        header_snippet = md_content[:2500].strip()

        print(
            f"[*] Classifying document using snippet ({len(header_snippet)} chars)..."
        )

        prompt = (
            "Identify the 'year', 'type' (Medical, Dental, Vision), and 'tier' (Gold, Silver, Bronze). "
            f"Return ONLY JSON. Text: {header_snippet}"
        )

        # Add a short timeout to prevent the 'Infinite Hang'
        response = ollama.generate(
            model=LOCAL_MODEL,
            prompt=prompt,
            format="json",
            options={"num_ctx": 4096, "temperature": 0},
        )

        data = json.loads(response["response"])

        raw_year = str(data.get("year", "2026"))
        clean_year = re.sub(r"\D", "", raw_year)

        return {
            "year": (
                int(clean_year) if clean_year else 2026
            ),  # Default to 2026 if extraction fails
            "type": nuclear_flatten(data.get("type", "Medical")),
            "tier": nuclear_flatten(data.get("tier", "Gold")),
        }
    except Exception as e:
        print(f"⚠️ Classification Error: {e}. Using defaults.")
        # FALLBACK: If the LLM hangs or fails, return a default so the script continues
        return {"year": 2026, "type": "Medical", "tier": "Gold"}

def generate_sub_index(md_content, sub_index_path):
    sub_index = []
    print(f"I am in generate_sub_index and the content length is {len(md_content)}")
    # 1. FORCED CHUNKING
    chunk_size = 4000
    final_chunks = [
        md_content[i : i + chunk_size] for i in range(0, len(md_content), chunk_size)
    ]
    total = len(final_chunks)

    # 2. DETECT MASSIVE FILES
    # If the file is huge, we use "Fast Indexing" to avoid Ollama hanging
    is_massive = len(md_content) > 10000

    print(
        f"[*] Indexing {total} sections ({'FAST MODE' if is_massive else 'AI MODE'})..."
    )

    for i, chunk in enumerate(final_chunks):
        if len(chunk.strip()) < 100:
            continue
        print(f"    -> Processing Part {i+1} of {total}...")

        if is_massive:
            # RULE-BASED: Just grab the first line as topic and common words as keywords
            topic = chunk.strip().split("\n")[0][:50]
            keywords = list(set(re.findall(r"\b\w{5,}\b", chunk.lower())))[
                :5
            ]  # 5+ letter words
        else:
            # AI-BASED: Only for small files
            prompt = f"Summary and 3 keywords. Return ONLY JSON: {{'topic': '...', 'keywords': []}}. Text: {chunk[:1000]}"
            try:
                # Add num_ctx to increase Ollama's "mouth" size
                response = ollama.generate(
                    model=LOCAL_MODEL,
                    prompt=prompt,
                    format="json",
                    options={"num_ctx": 4096},
                )
                metadata = json.loads(response["response"])
                topic = metadata.get("topic", "Detail")
                keywords = metadata.get("keywords", [])
            except:
                topic, keywords = "Detail", ["insurance"]

        sub_index.append(
            {
                "page_number": i,
                "topic": nuclear_flatten(topic),
                "keywords": [nuclear_flatten(k).lower() for k in keywords],
                "content": chunk.strip(),
            }
        )

    with open(sub_index_path, "w", encoding="utf-8") as f:
        json.dump(sub_index, f, indent=4)
    print(f"[*] SAVED SUB-INDEX: {sub_index_path}")

def build_all():
    setup_db()

    # All below options are for Docling 2.0+ and are critical for preventing hangs on complex PDFs with many tables or images.
    # OPTIMIZATION: Disable image generation to prevent hangs on complex tables
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True
    pipeline_options.generate_page_images = False  # Speed up processing
    pipeline_options.generate_table_images = False  # Prevent memory issues
    # This forces Docling to treat EVERY page as an image and OCR it.
    # It is slower, but it captures 100% of 'hidden' or 'graphic' text.
    pipeline_options.ocr_options.force_full_page_ocr = True
    
    # 2. OCR PRECISION (Directly under ocr_options)
    pipeline_options.ocr_options.force_full_page_ocr = True
    
    # 3. FIX FOR 'mode' AND 'do_cell_matching' ERRORS
    # Cast to TableStructureOptions to resolve the linter's 'Attribute not found' error
    table_options: TableStructureOptions = pipeline_options.table_structure_options # type: ignore
    
    # Use the Enum TableFormerMode.ACCURATE instead of a string
    table_options.mode = TableFormerMode.ACCURATE 
    
    # This helps reconstruct Premera's complex multi-line benefit cells
    table_options.do_cell_matching = True 
    
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

            # --- IDEMPOTENCY: Overwrite Logic ---
            cursor.execute(
                "SELECT sub_index_path FROM master_index WHERE pdf_path = ?",
                (pdf_path,),
            )
            existing = cursor.fetchone()
            if existing:
                print(f"[*] Overwriting existing index for: {filename}")
                if os.path.exists(existing[0]):
                    os.remove(existing[0])
                cursor.execute(
                    "DELETE FROM master_index WHERE pdf_path = ?", (pdf_path,)
                )

            try:
                print(
                    f"[*] Docling parsing: {filename} (Complex files may take 2+ mins)..."
                )
                # Docling 2.0+ supports timeout directly in the convert call
                result = converter.convert(pdf_path)
                md_text = result.document.export_to_markdown()

                print(f"[*] Successfully converted {filename} ({len(md_text)} chars).")

                plan_info = classify_document(md_text)
                if plan_info and plan_info["year"] != 0:
                    fn = f"{plan_info['year']}_{plan_info['type']}_{plan_info['tier']}.json".lower().replace(
                        " ", "_"
                    )
                    sub_index_path = os.path.abspath(os.path.join(INDEX_OUTPUT_DIR, fn))

                    generate_sub_index(md_text, sub_index_path)

                    cursor.execute(
                        """
                        INSERT INTO master_index (year, plan_type, plan_tier, pdf_path, sub_index_path)
                        VALUES (?, ?, ?, ?, ?)
                    """,
                        (
                            plan_info["year"],
                            plan_info["type"],
                            plan_info["tier"],
                            pdf_path,
                            sub_index_path,
                        ),
                    )
                    print(
                        f"✅ SUCCESS: {plan_info['year']} {plan_info['tier']} {plan_info['type']}"
                    )

            except Exception as e:
                print(f"❌ FAILED to process {filename}: {str(e)}")
                continue

    conn.commit()
    conn.close()

if __name__ == "__main__":
    build_all()
