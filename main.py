from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json
import ollama
from client import get_ai_response  # Your existing RAG logic
from fpdf import FPDF
import logging

# Initialize FastAPI app
app = FastAPI(title="Insurance RAG API")

# CORS Middleware (Allow React to interact with the API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"
    ],  # Use "*" temporarily, update with actual domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # For PDF downloads
)

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Helper function to validate and parse history from Form
def parse_history(history_str: str):
    try:
        history_list = json.loads(history_str)
        if not isinstance(history_list, list):
            raise ValueError("History must be a list.")
        return history_list
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON format for history.")
    except ValueError as e:
        raise e


# --- ENDPOINT 1: CHAT (Existing RAG) ---
@app.post("/chat")
async def chat_endpoint(prompt: str = Form(...), history: str = Form("[]")):
    """
    Chat endpoint that processes user prompts and history for AI response.
    """
    try:
        # Parse the history from the Form data
        history_list = parse_history(history)
        logger.info(
            f"[*] Received chat request with prompt: {prompt} and history: {history_list}"
        )

        # Get AI response based on the prompt and history
        answer = await get_ai_response(prompt, history_list)
        return {"answer": answer}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error processing chat request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 2: VISION SCANNER (Llama 3.2-Vision) ---
@app.post("/scan-card")
async def scan_card(file: UploadFile = File(...)):
    """
    Scan and analyze insurance card using Llama3.2-vision.
    """
    try:
        image_bytes = await file.read()
        logger.info(f"[*] Image received ({len(image_bytes)} bytes). Calling Llama...")

        # Call the Llama model for analysis
        response = ollama.chat(
            model="llama3.2-vision",
            messages=[
                {
                    "role": "user",
                    "content": """ACT AS A MEDICAL BILLING SPECIALIST. 
                            Analyze this Premera/Blue Cross card with 100% precision...
                            Return ONLY JSON.""",
                    "images": [image_bytes],
                }
            ],
        )

        # Process the AI response
        ai_text = response["message"]["content"]
        logger.info(f"[*] AI RESPONDED: {ai_text}")

        if not ai_text:
            return {"data": "⚠️ AI returned empty result."}

        # Try to parse the response as JSON and return it
        try:
            ai_data = json.loads(ai_text)
            return {"data": ai_data}
        except json.JSONDecodeError:
            return {"data": "⚠️ AI returned invalid JSON response."}

    except Exception as e:
        logger.error(f"❌ Backend Error: {str(e)}")
        return {"data": f"Error: {str(e)}"}


# --- ENDPOINT 3: PDF GENERATOR (FPDF2) ---
@app.post("/download-pdf")
async def download_pdf(content: str = Form(...)):
    """
    Generate and return a downloadable PDF based on the provided content.
    """
    try:
        # Clean the input content and parse table data
        lines = [line.strip() for line in content.split("\n") if "|" in line]
        table_data = []
        for line in lines:
            if "---" in line or ":---" in line:  # Skip separators
                continue
            row = [cell.strip() for cell in line.split("|") if cell.strip()]
            if row:
                table_data.append(row)

        # Initialize the PDF document
        pdf = FPDF(orientation="L", unit="mm", format="A4")
        pdf.add_page()

        # Header
        pdf.set_font("helvetica", "B", 16)
        pdf.cell(
            0,
            10,
            "Insurance Benefit Comparison Report",
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(5)

        # Calculate column widths dynamically based on the number of columns
        num_cols = len(table_data[0]) if table_data else 0
        font_size = 10 if num_cols <= 4 else (9 if num_cols <= 6 else 8)
        pdf.set_font("helvetica", size=font_size)

        col_widths = calculate_column_widths(num_cols)

        # Generate Table with automatic text wrapping
        with pdf.table(
            width=275, col_widths=col_widths, text_align="CENTER", line_height=6
        ) as table:
            for data_row in table_data:
                row = table.row()
                for cell_value in data_row:
                    row.cell(str(cell_value))

        # Output the PDF as bytes
        pdf_raw = pdf.output()
        pdf_bytes = bytes(pdf_raw)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": "attachment; filename=Benefit_Comparison.pdf",
                "Content-Length": str(len(pdf_bytes)),
            },
        )

    except Exception as e:
        logger.error(f"❌ PDF ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


def calculate_column_widths(num_cols: int):
    """Helper function to calculate column widths based on the number of columns."""
    if num_cols > 1:
        benefit_col_w = 35
        remaining_space = 240  # Total usable width is ~275mm
        data_col_w = remaining_space / (num_cols - 1)
        return [benefit_col_w] + [data_col_w] * (num_cols - 1)
    else:
        return [270]


# Run the app with Uvicorn (only if the script is being executed directly)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
