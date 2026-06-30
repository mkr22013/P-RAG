from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json
import os
import ollama
from clients.client import get_ai_response
from main.member_info_provider import get_member_info
from main.auth0middleware import Auth0Middleware
from infrastructure.blob_storage import download_index
from fpdf import FPDF
import logging

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("[*] P-RAG API starting up")

    # Load the shared drug_names.json (used by category.py for Rx category
    # detection and spelling correction) into the local indices folder.
    # download_index() already handles both environments:
    #   - Local dev (no AZURE_BLOB_CONNECTION_STRING): reads directly from
    #     the local indices/ folder, written there by rx_indexer.py
    #   - Production: downloads from Blob storage to the local indices/
    #     folder, so category.py's existing file-based loader works
    #     identically in both environments without any code branching there
    try:
        from utility.category import DRUG_NAMES_FILE

        local_dir = os.path.dirname(DRUG_NAMES_FILE)
        os.makedirs(local_dir, exist_ok=True)

        drug_names = (
            await download_index("drug_names.json")
            if os.getenv("AZURE_BLOB_CONNECTION_STRING")
            else None
        )
        if drug_names is not None:
            with open(DRUG_NAMES_FILE, "w", encoding="utf-8") as f:
                json.dump(drug_names, f)
            logger.info(
                f"[*] drug_names.json synced from Blob: {len(drug_names)} words"
            )
        elif os.path.exists(DRUG_NAMES_FILE):
            logger.info("[*] drug_names.json found locally — using existing file")
        else:
            logger.warning(
                "[*] drug_names.json not found locally or in Blob — "
                "run the Rx indexer at least once to generate it"
            )
    except Exception as e:
        logger.warning(f"[*] drug_names.json startup sync skipped: {e}")

    yield
    logger.info("[*] P-RAG API shutting down")
    try:
        import httpx

        # Close any open httpx clients
    except Exception:
        pass


# Initialize FastAPI app
app = FastAPI(title="Insurance RAG API", lifespan=lifespan)

# Auth0 middleware — skipped in dev when AUTH0_DOMAIN/AUDIENCE not set
app.add_middleware(Auth0Middleware)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# --- ENDPOINT: HEALTH CHECK ---
@app.get("/health")
async def health():
    """Health check — excluded from auth."""
    return {"status": "ok"}


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


# --- ENDPOINT 0: WELCOME ---
@app.get("/welcome")
async def welcome_endpoint(member_key: str = "", group_number: str = ""):
    """Returns welcome message and member plan info on first load."""
    from utility.prompts import WELCOME_MESSAGE

    return {
        "answer": WELCOME_MESSAGE,
        "member_info": get_member_info(
            member_key=member_key, group_number=group_number
        ),
    }


# --- ENDPOINT 0b: MEMBER INFO ---
@app.get("/member-info")
async def member_info_endpoint(member_key: str = "", group_number: str = ""):
    """Returns member plan details. Calls external API when configured."""
    return get_member_info(member_key=member_key, group_number=group_number)


# --- ENDPOINT 1: CHAT ---
# Serves both UI and external agents via the same endpoint.
#
# UI flow:
#   POST /chat
#   member_info = { full plan JSON }  ← already resolved by UI via /member-info
#   member_key  = ""                  ← not needed, member_info already present
#   group_number = ""
#
# External agent flow:
#   POST /chat
#   member_info  = {}                 ← empty, server resolves it
#   member_key   = "DEMO000001"       ← agent passes member identifier
#   group_number = "1000016"          ← agent passes group number
#   → server calls get_member_info(member_key, group_number) to resolve
#
# Resolution priority:
#   1. member_info if provided (UI path)
#   2. member_key + group_number if member_info empty (external agent path)
#   3. Demo member fallback if neither provided
@app.post("/chat")
async def chat_endpoint(
    prompt: str = Form(...),
    history: str = Form("[]"),
    member_info: str = Form("{}"),
    current_category: str = Form(""),
    member_key: str = Form(""),  # for external agents — skipped if member_info provided
    group_number: str = Form(
        ""
    ),  # for external agents — skipped if member_info provided
):
    """
    Main benefit query endpoint.

    UI usage: sends member_info JSON directly (already resolved).
    External agent usage: sends member_key + group_number, server resolves member_info.
    """
    try:
        history_list = parse_history(history)

        try:
            member_info_dict = json.loads(member_info) if member_info else {}
        except json.JSONDecodeError:
            member_info_dict = {}

        # External agents pass member_key + group_number instead of full member_info
        if not member_info_dict and member_key:
            member_info_dict = get_member_info(
                member_key=member_key, group_number=group_number
            )

        # Final fallback — demo member
        if not member_info_dict:
            member_info_dict = get_member_info()

        logger.info(
            f"[*] Received chat request with prompt: {prompt} and history: {history_list}"
        )

        # Reset token log for this request
        from utility.llm import reset_token_log, get_token_summary

        reset_token_log()

        result = await get_ai_response(
            prompt, history_list, member_info_dict, current_category
        )

        # Add token usage to response
        token_summary = get_token_summary()
        print(
            f"[TOKENS TOTAL] calls={token_summary['total_llm_calls']} input={token_summary['total_input_tokens']} output={token_summary['total_output_tokens']} total={token_summary['total_tokens']}"
        )

        if isinstance(result, dict):
            return {**result, "token_usage": token_summary}  # type: ignore[return-value]
        return {"answer": result, "token_usage": token_summary}  # type: ignore[return-value]

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

        # Call the Llama model — simple synchronous call as original
        response = ollama.chat(
            model="llama3.2-vision",
            messages=[
                {
                    "role": "user",
                    "content": 'Read this insurance card. Return ONLY JSON: {"prefix":"","identification":"","suffix":"","group_number":""}',
                    "images": [image_bytes],
                }
            ],
        )

        ai_text = response["message"]["content"]
        logger.info(f"[*] AI RESPONDED: {ai_text}")

        if not ai_text:
            return {"data": "⚠️ AI returned empty result."}

        try:
            ai_data = json.loads(ai_text)
            prefix = str(ai_data.get("prefix", "")).strip()
            identification = str(ai_data.get("identification", "")).strip()
            suffix = str(ai_data.get("suffix", "")).strip()
            group_number = str(ai_data.get("group_number", "")).strip()

            member_key = f"{prefix}{identification}{suffix}"
            logger.info(f"[*] MEMBER KEY: {member_key} | GROUP: {group_number}")

            member_info = get_member_info(
                member_key=member_key, group_number=group_number
            )

            return {
                "data": ai_data,
                "member_key": member_key,
                "group_number": group_number,
                "member_info": member_info,
            }
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

# # ===================================Previously working code before adding Rx Keywords into blob and access it===========================
# # from contextlib import asynccontextmanager
# # from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
# # from fastapi.middleware.cors import CORSMiddleware
# # from fastapi.responses import StreamingResponse
# # import json
# # import ollama
# # from clients.client import get_ai_response
# # from main.member_info_provider import get_member_info
# # from main.auth0middleware import Auth0Middleware
# # from fpdf import FPDF
# # import logging

# # # Set up basic logging
# # logging.basicConfig(level=logging.INFO)
# # logger = logging.getLogger(__name__)


# # @asynccontextmanager
# # async def lifespan(app: FastAPI):
# #     """Startup and shutdown lifecycle."""
# #     logger.info("[*] P-RAG API starting up")
# #     yield
# #     logger.info("[*] P-RAG API shutting down")
# #     try:
# #         import httpx

# #         # Close any open httpx clients
# #     except Exception:
# #         pass


# # # Initialize FastAPI app
# # app = FastAPI(title="Insurance RAG API", lifespan=lifespan)

# # # Auth0 middleware — skipped in dev when AUTH0_DOMAIN/AUDIENCE not set
# # app.add_middleware(Auth0Middleware)

# # # CORS Middleware
# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=["*"],
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# #     expose_headers=["Content-Disposition"],
# # )


# # # --- ENDPOINT: HEALTH CHECK ---
# # @app.get("/health")
# # async def health():
# #     """Health check — excluded from auth."""
# #     return {"status": "ok"}


# # # Helper function to validate and parse history from Form
# # def parse_history(history_str: str):
# #     try:
# #         history_list = json.loads(history_str)
# #         if not isinstance(history_list, list):
# #             raise ValueError("History must be a list.")
# #         return history_list
# #     except json.JSONDecodeError:
# #         raise ValueError("Invalid JSON format for history.")
# #     except ValueError as e:
# #         raise e


# # # --- ENDPOINT 0: WELCOME ---
# # @app.get("/welcome")
# # async def welcome_endpoint(member_key: str = "", group_number: str = ""):
# #     """Returns welcome message and member plan info on first load."""
# #     from utility.prompts import WELCOME_MESSAGE

# #     return {
# #         "answer": WELCOME_MESSAGE,
# #         "member_info": get_member_info(
# #             member_key=member_key, group_number=group_number
# #         ),
# #     }


# # # --- ENDPOINT 0b: MEMBER INFO ---
# # @app.get("/member-info")
# # async def member_info_endpoint(member_key: str = "", group_number: str = ""):
# #     """Returns member plan details. Calls external API when configured."""
# #     return get_member_info(member_key=member_key, group_number=group_number)


# # # --- ENDPOINT 1: CHAT ---
# # # Serves both UI and external agents via the same endpoint.
# # #
# # # UI flow:
# # #   POST /chat
# # #   member_info = { full plan JSON }  ← already resolved by UI via /member-info
# # #   member_key  = ""                  ← not needed, member_info already present
# # #   group_number = ""
# # #
# # # External agent flow:
# # #   POST /chat
# # #   member_info  = {}                 ← empty, server resolves it
# # #   member_key   = "DEMO000001"       ← agent passes member identifier
# # #   group_number = "1000016"          ← agent passes group number
# # #   → server calls get_member_info(member_key, group_number) to resolve
# # #
# # # Resolution priority:
# # #   1. member_info if provided (UI path)
# # #   2. member_key + group_number if member_info empty (external agent path)
# # #   3. Demo member fallback if neither provided
# # @app.post("/chat")
# # async def chat_endpoint(
# #     prompt: str = Form(...),
# #     history: str = Form("[]"),
# #     member_info: str = Form("{}"),
# #     current_category: str = Form(""),
# #     member_key: str = Form(""),  # for external agents — skipped if member_info provided
# #     group_number: str = Form(
# #         ""
# #     ),  # for external agents — skipped if member_info provided
# # ):
# #     """
# #     Main benefit query endpoint.

# #     UI usage: sends member_info JSON directly (already resolved).
# #     External agent usage: sends member_key + group_number, server resolves member_info.
# #     """
# #     try:
# #         history_list = parse_history(history)

# #         try:
# #             member_info_dict = json.loads(member_info) if member_info else {}
# #         except json.JSONDecodeError:
# #             member_info_dict = {}

# #         # External agents pass member_key + group_number instead of full member_info
# #         if not member_info_dict and member_key:
# #             member_info_dict = get_member_info(
# #                 member_key=member_key, group_number=group_number
# #             )

# #         # Final fallback — demo member
# #         if not member_info_dict:
# #             member_info_dict = get_member_info()

# #         logger.info(
# #             f"[*] Received chat request with prompt: {prompt} and history: {history_list}"
# #         )

# #         # Reset token log for this request
# #         from utility.llm import reset_token_log, get_token_summary

# #         reset_token_log()

# #         result = await get_ai_response(
# #             prompt, history_list, member_info_dict, current_category
# #         )

# #         # Add token usage to response
# #         token_summary = get_token_summary()
# #         print(
# #             f"[TOKENS TOTAL] calls={token_summary['total_llm_calls']} input={token_summary['total_input_tokens']} output={token_summary['total_output_tokens']} total={token_summary['total_tokens']}"
# #         )

# #         if isinstance(result, dict):
# #             return {**result, "token_usage": token_summary}  # type: ignore[return-value]
# #         return {"answer": result, "token_usage": token_summary}  # type: ignore[return-value]

# #     except ValueError as e:
# #         raise HTTPException(status_code=400, detail=str(e))
# #     except Exception as e:
# #         logger.error(f"Error processing chat request: {str(e)}")
# #         raise HTTPException(status_code=500, detail=str(e))


# # # --- ENDPOINT 2: VISION SCANNER (Llama 3.2-Vision) ---
# # @app.post("/scan-card")
# # async def scan_card(file: UploadFile = File(...)):
# #     """
# #     Scan and analyze insurance card using Llama3.2-vision.
# #     """
# #     try:
# #         image_bytes = await file.read()
# #         logger.info(f"[*] Image received ({len(image_bytes)} bytes). Calling Llama...")

# #         # Call the Llama model — simple synchronous call as original
# #         response = ollama.chat(
# #             model="llama3.2-vision",
# #             messages=[
# #                 {
# #                     "role": "user",
# #                     "content": 'Read this insurance card. Return ONLY JSON: {"prefix":"","identification":"","suffix":"","group_number":""}',
# #                     "images": [image_bytes],
# #                 }
# #             ],
# #         )

# #         ai_text = response["message"]["content"]
# #         logger.info(f"[*] AI RESPONDED: {ai_text}")

# #         if not ai_text:
# #             return {"data": "⚠️ AI returned empty result."}

# #         try:
# #             ai_data = json.loads(ai_text)
# #             prefix = str(ai_data.get("prefix", "")).strip()
# #             identification = str(ai_data.get("identification", "")).strip()
# #             suffix = str(ai_data.get("suffix", "")).strip()
# #             group_number = str(ai_data.get("group_number", "")).strip()

# #             member_key = f"{prefix}{identification}{suffix}"
# #             logger.info(f"[*] MEMBER KEY: {member_key} | GROUP: {group_number}")

# #             member_info = get_member_info(
# #                 member_key=member_key, group_number=group_number
# #             )

# #             return {
# #                 "data": ai_data,
# #                 "member_key": member_key,
# #                 "group_number": group_number,
# #                 "member_info": member_info,
# #             }
# #         except json.JSONDecodeError:
# #             return {"data": "⚠️ AI returned invalid JSON response."}

# #     except Exception as e:
# #         logger.error(f"❌ Backend Error: {str(e)}")
# #         return {"data": f"Error: {str(e)}"}


# # # --- ENDPOINT 3: PDF GENERATOR (FPDF2) ---
# # @app.post("/download-pdf")
# # async def download_pdf(content: str = Form(...)):
# #     """
# #     Generate and return a downloadable PDF based on the provided content.
# #     """
# #     try:
# #         # Clean the input content and parse table data
# #         lines = [line.strip() for line in content.split("\n") if "|" in line]
# #         table_data = []
# #         for line in lines:
# #             if "---" in line or ":---" in line:  # Skip separators
# #                 continue
# #             row = [cell.strip() for cell in line.split("|") if cell.strip()]
# #             if row:
# #                 table_data.append(row)

# #         # Initialize the PDF document
# #         pdf = FPDF(orientation="L", unit="mm", format="A4")
# #         pdf.add_page()

# #         # Header
# #         pdf.set_font("helvetica", "B", 16)
# #         pdf.cell(
# #             0,
# #             10,
# #             "Insurance Benefit Comparison Report",
# #             align="C",
# #             new_x="LMARGIN",
# #             new_y="NEXT",
# #         )
# #         pdf.ln(5)

# #         # Calculate column widths dynamically based on the number of columns
# #         num_cols = len(table_data[0]) if table_data else 0
# #         font_size = 10 if num_cols <= 4 else (9 if num_cols <= 6 else 8)
# #         pdf.set_font("helvetica", size=font_size)

# #         col_widths = calculate_column_widths(num_cols)

# #         # Generate Table with automatic text wrapping
# #         with pdf.table(
# #             width=275, col_widths=col_widths, text_align="CENTER", line_height=6
# #         ) as table:
# #             for data_row in table_data:
# #                 row = table.row()
# #                 for cell_value in data_row:
# #                     row.cell(str(cell_value))

# #         # Output the PDF as bytes
# #         pdf_raw = pdf.output()
# #         pdf_bytes = bytes(pdf_raw)

# #         return Response(
# #             content=pdf_bytes,
# #             media_type="application/pdf",
# #             headers={
# #                 "Content-Disposition": "attachment; filename=Benefit_Comparison.pdf",
# #                 "Content-Length": str(len(pdf_bytes)),
# #             },
# #         )

# #     except Exception as e:
# #         logger.error(f"❌ PDF ERROR: {str(e)}")
# #         raise HTTPException(status_code=500, detail=str(e))


# # def calculate_column_widths(num_cols: int):
# #     """Helper function to calculate column widths based on the number of columns."""
# #     if num_cols > 1:
# #         benefit_col_w = 35
# #         remaining_space = 240  # Total usable width is ~275mm
# #         data_col_w = remaining_space / (num_cols - 1)
# #         return [benefit_col_w] + [data_col_w] * (num_cols - 1)
# #     else:
# #         return [270]


# # # Run the app with Uvicorn (only if the script is being executed directly)
# # if __name__ == "__main__":
# #     import uvicorn

# #     uvicorn.run(app, host="0.0.0.0", port=8000)
