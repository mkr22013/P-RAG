"""
drug_intelligence_api.py — Drug Intelligence API

Standalone FastAPI service that provides READ-ONLY access to drug and
condition intelligence data from the Rx formulary index.

Data is populated and maintained by the BENJI team using:
    build_rxclass_lookup.py  — fills illnesses[] in drug_words.json from RxNorm RRF files
    rx_classifier.py         — generates condition_synonyms.json

No LLM calls are made at query time — all data is pre-computed.

Endpoints:
    GET  /health
    GET  /api/v1/condition/drugs?condition=diabetes
    POST /api/v1/condition/resolve
    POST /api/v1/condition/expand
    GET  /api/v1/drug/conditions?drug=metformin
    GET  /api/v1/drug/exists?name=vivjoa
    GET  /api/v1/drug/search?name=metformin&plan=E4&group=1000016
    POST /api/v1/admin/cache/invalidate

Run locally:
    python -m uvicorn drug_intelligence_api:app --port 8001 --reload

Data files (read-only at runtime):
    indices/drug_words.json           — drug word → entry_type + full_names + illnesses
    indices/condition_synonyms.json   — condition → patient-friendly synonyms
    indices/2026_rx_*.json            — full formulary drug chunks
"""

import os
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from main.auth0middleware import Auth0Middleware
from utility.condition_resolver import (
    _load_drug_illness,
    _load_condition_synonyms,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRUG_WORDS_FILE = os.path.join(_BASE_DIR, "indices", "drug_words.json")
CONDITION_SYNONYMS_FILE = os.path.join(_BASE_DIR, "indices", "condition_synonyms.json")
INDICES_DIR = os.path.join(_BASE_DIR, "indices")


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup — pre-load data files into condition_resolver cache."""
    logger.info("[*] Drug Intelligence API starting up")

    try:
        drug_data = _load_drug_illness()
        synonym_data = _load_condition_synonyms()
        logger.info(
            f"[*] Loaded {len(drug_data)} drug words, "
            f"{len(synonym_data)} conditions with synonyms"
        )
    except Exception as e:
        logger.warning(f"[*] Cache pre-warm skipped: {e}")

    yield
    logger.info("[*] Drug Intelligence API shutting down")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Drug Intelligence API",
    description="Drug and condition intelligence from the Rx formulary index.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(Auth0Middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────
class ResolveRequest(BaseModel):
    query: str


class ExpandRequest(BaseModel):
    term: str


class ConditionDrugsResponse(BaseModel):
    condition: str
    canonical: str | None
    synonyms: list[str]
    drugs: list[str]
    drug_count: int


class DrugConditionsResponse(BaseModel):
    drug: str
    conditions: list[str]
    condition_count: int


class DrugExistsResponse(BaseModel):
    drug: str
    exists: bool


class DrugSearchResponse(BaseModel):
    drug: str
    plan: str
    group: str
    entries: list[dict]
    entry_count: int


class CacheInvalidateResponse(BaseModel):
    status: str
    message: str


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Health check — excluded from auth."""
    return {
        "status": "ok",
        "drug_words_file": os.path.exists(DRUG_WORDS_FILE),
        "condition_synonyms_file": os.path.exists(CONDITION_SYNONYMS_FILE),
    }


# ── Condition endpoints ───────────────────────────────────────────────────────


@app.get("/api/v1/condition/drugs", response_model=ConditionDrugsResponse)
async def get_drugs_for_condition(
    condition: str = Query(
        ..., description="Condition name e.g. 'diabetes' or 'blood pressure'"
    )
):
    """
    Returns all drugs that treat the given condition.
    Accepts both canonical names and synonyms.

    Examples:
        ?condition=diabetes       → metformin, glipizide, ozempic...
        ?condition=blood pressure → lisinopril, amlodipine, losartan...
        ?condition=high bp        → same as blood pressure
    """
    from utility.condition_resolver import (
        find_canonical_condition,
        get_drugs_for_condition,
        expand_condition,
    )

    canonical = find_canonical_condition(condition)
    synonyms = expand_condition(condition) if canonical else [condition]
    drugs = get_drugs_for_condition(condition)

    return ConditionDrugsResponse(
        condition=condition,
        canonical=canonical,
        synonyms=synonyms,
        drugs=drugs,
        drug_count=len(drugs),
    )


@app.post("/api/v1/condition/resolve", response_model=ConditionDrugsResponse)
async def resolve_condition(body: ResolveRequest):
    """
    Resolves a free-text query to a condition and matching drugs.
    Uses bigram/trigram extraction + synonym lookup + LLM fallback.

    Examples:
        {"query": "I want to know about my blood pressure medication"}
        {"query": "what drugs are available for diabetes?"}
        {"query": "my sugar is high what can I take"}
    """
    from utility.condition_resolver import (
        resolve_query_to_drugs,
        extract_condition_terms,
        find_canonical_condition,
        expand_condition,
    )

    if not body.query or not body.query.strip():
        raise HTTPException(status_code=400, detail="query cannot be empty")

    candidates = extract_condition_terms(body.query)
    canonical = None
    matched_term = None

    for term in candidates:
        canonical = find_canonical_condition(term)
        if canonical:
            matched_term = term
            break

    drugs = resolve_query_to_drugs(body.query, use_llm_fallback=False)
    synonyms = expand_condition(canonical) if canonical else []

    return ConditionDrugsResponse(
        condition=matched_term or body.query,
        canonical=canonical,
        synonyms=synonyms,
        drugs=drugs,
        drug_count=len(drugs),
    )


@app.post("/api/v1/condition/expand")
async def expand_condition_endpoint(body: ExpandRequest):
    """
    Expands a condition term to its canonical name and all synonyms.

    Examples:
        {"term": "blood pressure"} → {canonical: "hypertension", synonyms: [...]}
        {"term": "sugar"}          → {canonical: "diabetes", synonyms: [...]}
    """
    from utility.condition_resolver import find_canonical_condition, expand_condition

    if not body.term or not body.term.strip():
        raise HTTPException(status_code=400, detail="term cannot be empty")

    canonical = find_canonical_condition(body.term)
    synonyms = expand_condition(body.term)

    return {
        "term": body.term,
        "canonical": canonical,
        "synonyms": synonyms,
        "found": canonical is not None,
    }


# ── Drug endpoints ────────────────────────────────────────────────────────────


@app.get("/api/v1/drug/conditions", response_model=DrugConditionsResponse)
async def get_conditions_for_drug(
    drug: str = Query(..., description="Drug name e.g. 'metformin' or 'lisinopril'")
):
    """
    Returns the conditions a drug treats.

    Examples:
        ?drug=metformin  → ["Diabetes Mellitus, Type 2", "Insulin Resistance"]
        ?drug=lisinopril → ["Hypertension", "Heart Failure"]
    """
    from utility.condition_resolver import get_conditions_for_drug

    conditions = get_conditions_for_drug(drug)

    return DrugConditionsResponse(
        drug=drug,
        conditions=conditions,
        condition_count=len(conditions),
    )


@app.get("/api/v1/drug/exists", response_model=DrugExistsResponse)
async def drug_exists(
    name: str = Query(..., description="Drug name to check e.g. 'vivjoa'")
):
    """
    Checks if a drug name exists in the formulary index.
    Useful for category detection and spelling validation.

    Examples:
        ?name=vivjoa     → {exists: true}
        ?name=randomword → {exists: false}
    """
    drug_data = _load_drug_illness()
    exists = name.lower().strip() in drug_data

    return DrugExistsResponse(drug=name, exists=exists)


@app.get("/api/v1/drug/search", response_model=DrugSearchResponse)
async def search_drug(
    name: str = Query(..., description="Drug name e.g. 'metformin'"),
    plan: str = Query("E4", description="Plan variant e.g. E4, A2"),
    group: str = Query("1000016", description="Group number"),
):
    """
    Returns full formulary details for a drug — tier, cost, requirements.
    Searches the rx index JSON files for matching entries.

    Examples:
        ?name=metformin&plan=E4&group=1000016
        ?name=vivjoa&plan=E4&group=1000016
    """
    name_lower = name.lower().strip()
    entries = []

    try:
        for filename in os.listdir(INDICES_DIR):
            if not filename.endswith(".json"):
                continue
            if "rx" not in filename.lower():
                continue
            if group not in filename and plan.lower() not in filename.lower():
                continue

            filepath = os.path.join(INDICES_DIR, filename)
            with open(filepath, encoding="utf-8") as f:
                chunks = json.load(f)

            for chunk in chunks:
                content = chunk.get("content", {})
                drug_name = content.get("drug_name", "")
                if name_lower in drug_name.lower():
                    entries.append(
                        {
                            "drug_name": drug_name,
                            "tier": content.get("tier", ""),
                            "tier_label": content.get("tier_label", ""),
                            "requirements": content.get("requirements", ""),
                            "requirements_text": content.get("requirements_text", ""),
                            "drug_category": content.get("drug_category", ""),
                            "drug_subcategory": content.get("drug_subcategory", ""),
                            "page_number": chunk.get("page_number", 0),
                        }
                    )

    except Exception as e:
        logger.error(f"[!] drug search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

    if not entries:
        raise HTTPException(
            status_code=404,
            detail=f"No formulary entries found for drug '{name}' in plan {plan}",
        )

    return DrugSearchResponse(
        drug=name,
        plan=plan,
        group=group,
        entries=entries,
        entry_count=len(entries),
    )


# ── Admin endpoints ───────────────────────────────────────────────────────────


@app.post("/api/v1/admin/cache/invalidate", response_model=CacheInvalidateResponse)
async def invalidate_cache():
    """
    Clears the in-memory cache for drug_words.json and condition_synonyms.json.
    Call this after running build_rxclass_lookup.py or rx_classifier --synonyms
    to pick up new data without restarting the server.
    """
    try:
        import utility.condition_resolver as cr

        cr._drug_illness_loaded_at = None
        cr._condition_synonyms_loaded_at = None
        cr._drug_illness_data = {}
        cr._condition_synonyms_data = {}

        logger.info("[*] Drug Intelligence API: cache invalidated")
        return CacheInvalidateResponse(
            status="ok",
            message="Cache cleared — next request will reload from disk",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Cache invalidation failed: {str(e)}"
        )
