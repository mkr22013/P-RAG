# BENJI Architecture Overview

## Benefits Engine for Navigation & Intelligent Guidance

BENJI is a local-first AI-powered insurance benefits intelligence platform designed to provide accurate, explainable, and deterministic benefit retrieval across Medical, Dental, Vision, and SBC documents.

Unlike traditional vector-only RAG systems, BENJI combines structured insurance indexing, topic-aware retrieval, and MCP orchestration to reduce hallucinations and improve retrieval precision.

---

# Why BENJI?

Insurance benefits are highly structured and identity-sensitive.

Traditional semantic search systems may retrieve:

* incorrect plan years
* wrong networks
* unrelated benefit sections
* semantically similar but incorrect answers

BENJI solves this using deterministic plan routing and structured retrieval.

---

# Core Differentiators

## 1. Deterministic Plan Identity Routing

Before retrieval begins, BENJI isolates documents using:

* Year
* Group Number
* Group Name
* Plan
* Plan Type
* Tier
* Variant
* Network

This prevents cross-plan contamination and improves answer reliability.

---

## 2. Structured Insurance Intelligence

Instead of indexing raw paragraphs, BENJI converts insurance documents into structured benefit objects.

Example:

```json
{
  "event": "Urgent Care",
  "service": "Freestanding urgent care centers",
  "in_network": "$35 copay",
  "out_of_network": "40% coinsurance"
}
```

This creates highly explainable and auditable retrieval.

---

## 3. Topic-Aware Retrieval

BENJI maps user intent into insurance-specific topics.

Examples:

| User Query            | Resolved Topic |
| --------------------- | -------------- |
| urgent care cost      | urgent care    |
| blood work benefits   | diagnostic     |
| psychological testing | mental-health  |
| braces coverage       | orthodontia    |

This improves retrieval accuracy significantly compared to generic semantic search.

---

## 4. Explainable Retrieval Pipeline

BENJI retrieval flow:

```text
User Query
→ Category Detection
→ Topic Resolution
→ Keyword Extraction
→ Plan Identity Routing
→ Structured Chunk Ranking
→ LLM Response Synthesis
```

Every retrieval decision can be audited and explained.

---

## 5. Local-First AI

BENJI runs fully locally using Ollama and does not require external cloud LLM providers.

Benefits:

* No PHI leaves the environment
* Reduced compliance concerns
* Lower operational cost
* Improved enterprise control

---

# Architecture

```text
React UI
   ↓
FastAPI Backend
   ↓
LLM Orchestrator
   ↓
MCP Retrieval Server
   ↓
SQLite + Structured JSON Indices
   ↓
Insurance PDFs
```

---

# Retrieval Strategy

BENJI combines:

* rule-based retrieval
* topic classification
* keyword extraction
* typo-tolerant matching
* structured ranking
* LLM fallback only when necessary

This hybrid approach improves precision while minimizing hallucinations.

---

# Enterprise Benefits

* Explainable AI
* Deterministic retrieval
* Local-first deployment
* Structured insurance intelligence
* Reduced hallucinations
* Audit-friendly architecture
* No vector database dependency
* MCP-compatible orchestration

---

# Summary

BENJI is not just a document search engine.

It is a structured insurance intelligence platform designed specifically for healthcare and benefits navigation use cases where accuracy, explainability, and deterministic retrieval are critical.
