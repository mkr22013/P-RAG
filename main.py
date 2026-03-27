from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import re
import io
import ollama
from client import get_ai_response # Your existing RAG logic
from fpdf import FPDF

app = FastAPI(title="Insurance RAG API")

# Allow React (usually port 5173) to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Use "*" temporarily to ensure the connection works
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"] # CRITICAL for PDF downloads
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
            messages=[{
                'role': 'user',
                'content': """
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
                'images': [image_bytes]
            }],
            options={"temperature": 0} # Faster and more stable
        )
        
        # 2. CAPTURE THE TEXT BEFORE SENDING
        ai_text = response['message']['content']
        print(f"[*] AI RESPONDED: {ai_text}") # <--- Check this in your terminal!

        if not ai_text:
            return {"data": "⚠️ AI returned empty result."}

        # 3. Explicitly return a JSON object
        return {"data": str(ai_text)}

    except Exception as e:
        print(f"❌ Backend Error: {str(e)}")
        return {"data": f"Error: {str(e)}"}

# --- ENDPOINT 3: PDF GENERATOR (FPDF2) ---
import io
import re
from fastapi import Form, HTTPException
from fastapi.responses import StreamingResponse
from fpdf import FPDF

import io
import re
from fastapi import Form, HTTPException
from fastapi.responses import StreamingResponse
from fpdf import FPDF

@app.post("/download-pdf")
async def download_pdf(content: str = Form(...)):
    try:
        # Initialize FPDF
        pdf = FPDF()
        pdf.add_page()
        
        # 1. HEADER - Large & Bold
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 15, "Insurance Benefit Comparison Report", 
                 new_x="LMARGIN", new_y="NEXT", align='C')
        pdf.ln(5)

        # 2. SEPARATE TEXT FROM TABLE
        lines = content.split('\n')
        table_data = []
        regular_text = []

        for line in lines:
            if "|" in line:
                # Clean row: remove outer pipes, split, and strip
                row = [cell.strip() for cell in line.split('|') if cell.strip()]
                # Skip separator lines like |---|---|
                if row and not all(c == '-' for c in row):
                    table_data.append(row)
            else:
                if line.strip():
                    regular_text.append(line.strip())

        # 3. RENDER INTRO/SUMMARY TEXT
        pdf.set_font("Helvetica", size=11)
        for text in regular_text:
            # Clean markdown bold/italics markers
            clean_text = re.sub(r'\*+', '', text)
            pdf.multi_cell(0, 8, clean_text)
            pdf.ln(2)

        # 4. RENDER THE BEAUTIFUL TABLE (Prose Style)
        if table_data:
            pdf.ln(5)
            pdf.set_font("Helvetica", size=10)
            
            # Using the modern fpdf2 table method for borders & shading
            with pdf.table(
                borders_layout="SINGLE_TOP_LINE",
                cell_fill_color=(245, 247, 250), # Light gray shading
                cell_fill_mode="ROWS",
                line_height=8,
                text_align="LEFT",
                width=190 # Set table width to fit page margins
            ) as t:
                for data_row in table_data:
                    row = t.row()
                    for datum in data_row:
                        row.cell(datum)

        # 5. STREAMING OUTPUT
        pdf_bytes = pdf.output() 
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=Benefit_Comparison.pdf"}
        )

    except Exception as e:
        print(f"PDF Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
