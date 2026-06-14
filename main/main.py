from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json
import ollama
from clients.client import get_ai_response
from main.member_info_provider import get_member_info
from main.auth0middleware import Auth0Middleware
from fpdf import FPDF
import logging

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("[*] P-RAG API starting up")
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

##============================Previously working code before adding external API calls============================

# # from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
# # from fastapi.middleware.cors import CORSMiddleware
# # from fastapi.responses import StreamingResponse
# # from contextlib import asynccontextmanager
# # import json
# # import ollama
# # from clients.client import get_ai_response
# # from .member_info_provider import get_member_info, validate_dependent, close_client
# # from .auth0middleware import Auth0Middleware
# # from fpdf import FPDF
# # import logging

# # logging.basicConfig(level=logging.INFO)
# # logger = logging.getLogger(__name__)


# # @asynccontextmanager
# # async def lifespan(app: FastAPI):
# #     """Manage application lifespan — clean up resources on shutdown."""
# #     yield
# #     await close_client()
# #     logger.info("httpx client closed cleanly.")


# # app = FastAPI(title="Insurance RAG API", lifespan=lifespan)

# # # ── Middleware — order matters: Auth0 runs before CORS ────────────────────────
# # app.add_middleware(Auth0Middleware)

# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=["*"],
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# #     expose_headers=["Content-Disposition"],
# # )


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


# # # --- HEALTH CHECK (excluded from auth) ---
# # @app.get("/health")
# # async def health():
# #     """Health check — no auth required. Used by container orchestration."""
# #     return {"status": "ok"}


# # # --- ENDPOINT 0: WELCOME ---
# # @app.get("/welcome")
# # async def welcome_endpoint(member_key: str = "", group_number: str = ""):
# #     """Returns welcome message and member plan info on first load."""
# #     from utility.prompts import WELCOME_MESSAGE

# #     return {
# #         "answer": WELCOME_MESSAGE,
# #         "member_info": await get_member_info(
# #             member_key=member_key, group_number=group_number
# #         ),
# #     }


# # # --- ENDPOINT 0b: MEMBER INFO ---
# # @app.get("/member-info")
# # async def member_info_endpoint(member_key: str = "", group_number: str = ""):
# #     """Returns member plan details. Calls external API when configured."""
# #     return await get_member_info(member_key=member_key, group_number=group_number)


# # # --- ENDPOINT 1: CHAT ---
# # @app.post("/chat")
# # async def chat_endpoint(
# #     prompt: str = Form(...),
# #     history: str = Form("[]"),
# #     member_info: str = Form("{}"),
# #     current_category: str = Form(""),
# # ):
# #     """
# #     Chat endpoint that processes user prompts and history for AI response.
# #     member_info: JSON string of member plan details from /member-info.
# #     """
# #     try:
# #         history_list = parse_history(history)
# #         try:
# #             member_info_dict = json.loads(member_info) if member_info else {}
# #         except json.JSONDecodeError:
# #             member_info_dict = {}

# #         if not member_info_dict:
# #             member_info_dict = await get_member_info()

# #         logger.info(
# #             f"[*] Received chat request with prompt: {prompt} and history: {history_list}"
# #         )

# #         result = await get_ai_response(
# #             prompt, history_list, member_info_dict, current_category
# #         )
# #         if isinstance(result, dict):
# #             return result
# #         return {"answer": result}
# #     except ValueError as e:
# #         raise HTTPException(status_code=400, detail=str(e))
# #     except Exception as e:
# #         logger.error(f"Error processing chat request: {str(e)}")
# #         raise HTTPException(status_code=500, detail=str(e))


# # # --- ENDPOINT 1b: VALIDATE DEPENDENT ---
# # @app.post("/validate-dependent")
# # async def validate_dependent_endpoint(
# #     scanned_member_key: str = Form(...),
# #     group_number: str = Form(...),
# #     member_keys: str = Form("[]"),
# # ):
# #     """
# #     Validates whether a scanned card belongs to a dependent of the logged-in member.
# #     """
# #     try:
# #         keys_list = json.loads(member_keys)
# #     except json.JSONDecodeError:
# #         keys_list = []

# #     # Own card check
# #     if scanned_member_key in keys_list:
# #         return {
# #             "valid": False,
# #             "reason": "own_card",
# #             "message": "This looks like one of your own cards. Please scan a dependent's card if you'd like to check their benefits.",
# #         }

# #     # Dependent validation API call
# #     result = await validate_dependent(scanned_member_key, group_number)

# #     if not result:
# #         return {
# #             "valid": False,
# #             "reason": "not_found",
# #             "message": "This card doesn't appear to belong to your family plan. Please contact Premera if you need help.",
# #         }

# #     # Verify primary holder is the logged-in member
# #     primary_key = result.get("primary_holder", {}).get("member_key", "")
# #     if primary_key not in keys_list:
# #         return {
# #             "valid": False,
# #             "reason": "not_your_dependent",
# #             "message": "This card doesn't appear to belong to your family plan. Please contact Premera if you need help.",
# #         }

# #     return {
# #         "valid": True,
# #         "member_info": result.get("dependent", {}),
# #         "member_key": scanned_member_key,
# #         "group_number": group_number,
# #     }


# # # --- ENDPOINT 2: VISION SCANNER ---
# # @app.post("/scan-card")
# # async def scan_card(file: UploadFile = File(...)):
# #     """Scan and analyze insurance card using Llama3.2-vision."""
# #     try:
# #         image_bytes = await file.read()
# #         logger.info(f"[*] Image received ({len(image_bytes)} bytes). Calling Llama...")

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

# #             member_info = await get_member_info(
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


# # # --- ENDPOINT 3: PDF GENERATOR ---
# # @app.post("/download-pdf")
# # async def download_pdf(content: str = Form(...)):
# #     """Generate and return a downloadable PDF based on the provided content."""
# #     try:
# #         # Sanitize — replace characters unsupported by helvetica font
# #         content = (
# #             content.replace("•", "-")
# #             .replace("\u2013", "-")
# #             .replace("\u2014", "-")
# #             .replace("\u2019", "'")
# #             .replace("\u2018", "'")
# #             .replace("\u201c", '"')
# #             .replace("\u201d", '"')
# #             .replace("\u2122", "")
# #             .replace("\u00ae", "")
# #         )

# #         lines = [line.strip() for line in content.split("\n") if "|" in line]
# #         table_data = []
# #         for line in lines:
# #             if "---" in line or ":---" in line:
# #                 continue
# #             row = [cell.strip() for cell in line.split("|") if cell.strip()]
# #             if row:
# #                 table_data.append(row)

# #         pdf = FPDF(orientation="L", unit="mm", format="A4")
# #         pdf.add_page()

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

# #         num_cols = len(table_data[0]) if table_data else 0
# #         font_size = 10 if num_cols <= 4 else (9 if num_cols <= 6 else 8)
# #         pdf.set_font("helvetica", size=font_size)

# #         col_widths = calculate_column_widths(num_cols)

# #         with pdf.table(
# #             width=275, col_widths=col_widths, text_align="CENTER", line_height=6
# #         ) as table:
# #             for data_row in table_data:
# #                 row = table.row()
# #                 for cell_value in data_row:
# #                     row.cell(str(cell_value))

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
# #     if num_cols > 1:
# #         benefit_col_w = 35
# #         remaining_space = 240
# #         data_col_w = remaining_space / (num_cols - 1)
# #         return [benefit_col_w] + [data_col_w] * (num_cols - 1)
# #     else:
# #         return [270]


# # if __name__ == "__main__":
# #     import uvicorn

# #     uvicorn.run(app, host="0.0.0.0", port=8000)

# =============================Previously working code before MFE refactor, kept here for reference==========================
# # from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
# # from fastapi.middleware.cors import CORSMiddleware
# # from fastapi.responses import StreamingResponse
# # import json
# # import ollama
# # from clients.client import get_ai_response
# # from member_info_provider import get_member_info  # Your existing RAG logic
# # from fpdf import FPDF
# # import logging

# # # Initialize FastAPI app
# # app = FastAPI(title="Insurance RAG API")

# # # CORS Middleware (Allow React to interact with the API)
# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=[
# #         "*"
# #     ],  # Use "*" temporarily, update with actual domains in production
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# #     expose_headers=["Content-Disposition"],  # For PDF downloads
# # )

# # # Set up basic logging
# # logging.basicConfig(level=logging.INFO)
# # logger = logging.getLogger(__name__)


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


# # # --- ENDPOINT 1: CHAT (Existing RAG) ---
# # @app.post("/chat")
# # async def chat_endpoint(
# #     prompt: str = Form(...),
# #     history: str = Form("[]"),
# #     member_info: str = Form("{}"),
# #     current_category: str = Form(""),
# # ):
# #     """
# #     Chat endpoint that processes user prompts and history for AI response.
# #     member_info: JSON string of member plan details from /member-info.
# #     """
# #     try:
# #         history_list = parse_history(history)
# #         try:
# #             member_info_dict = json.loads(member_info) if member_info else {}
# #         except json.JSONDecodeError:
# #             member_info_dict = {}

# #         if not member_info_dict:
# #             member_info_dict = get_member_info()

# #         logger.info(
# #             f"[*] Received chat request with prompt: {prompt} and history: {history_list}"
# #         )

# #         result = await get_ai_response(
# #             prompt, history_list, member_info_dict, current_category
# #         )
# #         if isinstance(result, dict):
# #             return result
# #         return {"answer": result}
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
