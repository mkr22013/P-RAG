# ── P-RAG: Insurance Policy Assistant ─────────────────────────────────────────
# FastAPI REST API (main.py) + plan_indexer library
# LLM is served externally by an ollama container (see docker-compose.yml).
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# System deps for pdfplumber (pdfminer) and docling (pypdfium2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.docker.txt .
RUN pip install --no-cache-dir -r requirements.docker.txt

# ── Application source ────────────────────────────────────────────────────────
# plan_indexer library (zero external deps beyond what's above)
COPY plan_indexer/ plan_indexer/

# App files
COPY app/ app/

# SQLite index DB — pre-built from docs/; mount a volume here to persist
# re-indexing runs done inside the container.
COPY p_insurance_index.db app/p_insurance_index.db

# Optional: copy source PDFs so re-indexing can run inside the container
COPY docs/ docs/

# ── Runtime config ────────────────────────────────────────────────────────────
# main.py (FastAPI + uvicorn) runs from the app/ directory so relative imports work.
WORKDIR /workspace/app

# OLLAMA_HOST is set at runtime via docker-compose (points to ollama service).
ENV OLLAMA_HOST=http://ollama:11434 \
    OLLAMA_MODEL=llama3.1

EXPOSE 8000

# plan_indexer must be on PYTHONPATH so `from plan_indexer import ...` works
ENV PYTHONPATH=/workspace

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
