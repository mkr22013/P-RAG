from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import re
import io
import ollama
from client import get_ai_response  # Your existing RAG logic
from fpdf import FPDF

app = FastAPI(title="Insurance RAG API")

# Allow React (usually port 5173) to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Use "*" temporarily to ensure the connection works
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # CRITICAL for PDF downloads
)


# --- ENDPOINT 1: CHAT (Existing RAG) ---
@app.post("/chat")
async def chat_endpoint(prompt: str = Form(...), history: str = Form("[]")):
    import json

    try:
        # Convert the history string from React back into a list
        history_list = json.loads(history)
        answer = await get_ai_response(prompt, history_list)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 2: VISION SCANNER (Llama 3.2-Vision) ---
@app.post("/scan-card")
async def scan_card(file: UploadFile = File(...)):
    try:
        image_bytes = await file.read()
        print(f"[*] Image received ({len(image_bytes)} bytes). Calling Llama...")

        # 1. Call the model
        response = ollama.chat(
            model="llama3.2-vision",
            messages=[
                {
                    "role": "user",
                    "content": """
                    ACT AS A MEDICAL BILLING SPECIALIST. 
                    Analyze this Premera/Blue Cross card with 100% precision:

                    1. ALPHA PREFIX: The 3 letters at the very start of the ID (e.g., 'PBV').
                    2. FULL MEMBER ID: The prefix + all following numbers/letters.
                    3. GROUP NUMBER: This is a 7-digit number SEPARATE from the Member ID. 
                    It is usually labeled 'Group' or 'GRP'. Look specifically for 7 digits.
                    4. NETWORK: (e.g., 'Heritage Signature').

                    STRICT RULE: Do not confuse the Member ID digits with the Group Number. 
                    If the Group Number is not explicitly labeled, look for a standalone 7-digit string.
                    Return ONLY JSON.
                    """,
                    "images": [image_bytes],
                }
            ],
            options={"temperature": 0},  # Faster and more stable
        )

        # 2. CAPTURE THE TEXT BEFORE SENDING
        ai_text = response["message"]["content"]
        print(f"[*] AI RESPONDED: {ai_text}")  # <--- Check this in your terminal!

        if not ai_text:
            return {"data": "⚠️ AI returned empty result."}

        # 3. Explicitly return a JSON object
        return {"data": str(ai_text)}

    except Exception as e:
        print(f"❌ Backend Error: {str(e)}")
        return {"data": f"Error: {str(e)}"}


# --- ENDPOINT 3: PDF GENERATOR (FPDF2) ---
@app.post("/download-pdf")
async def download_pdf(content: str = Form(...)):
    try:
        # 1. PARSE & CLEAN MARKDOWN
        lines = [line.strip() for line in content.split("\n") if "|" in line]
        table_data = []
        for line in lines:
            if "---" in line:
                continue
            # Filter out empty strings from leading/trailing pipes
            row = [cell.strip() for cell in line.split("|") if cell.strip()]
            if row:
                table_data.append(row)

        # 2. INITIALIZE LANDSCAPE PDF (297mm wide)
        pdf = FPDF(orientation="L", unit="mm", format="A4")
        pdf.add_page()

        # Header - Use 'helvetica' for 2.7.8+ compatibility
        pdf.set_font("helvetica", "B", 16)
        pdf.cell(
            0,
            10,
            "Insurance Benefit Comparison Report",
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(10)

        # 3. GENERATE TABLE (The Fix for Overlapping Text)
        pdf.set_font("helvetica", size=10)

        # Calculate column widths: Benefit (50mm) + Data columns (split remaining 220mm)
        num_cols = len(table_data[0]) if table_data else 0
        if num_cols > 1:
            data_w = 220 / (num_cols - 1)
            col_widths = [50] + [data_w] * (num_cols - 1)
        else:
            col_widths = [270]

        # pdf.table handles the "Out-of-Network" wrapping and row height perfectly
        with pdf.table(width=270, col_widths=col_widths, text_align="CENTER") as table:
            for data_row in table_data:
                row = table.row()
                for cell_value in data_row:
                    row.cell(str(cell_value))

        # 4. THE BYTEARRAY FIX
        # Convert bytearray to bytes so FastAPI doesn't try to .encode() it
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
        print(f"❌ PDF ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
