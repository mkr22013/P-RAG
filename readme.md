# BENJI

## Benefits Engine for Navigation & Intelligent Guidance

BENJI is a local-first AI-powered insurance benefits assistant designed to answer complex Medical, Dental, Vision, and SBC (Summary of Benefits & Coverage) questions using structured retrieval, MCP orchestration, and deterministic plan routing.

Unlike traditional vector-based RAG systems, BENJI uses explainable structured indexing and topic-driven retrieval to provide accurate, auditable, and enterprise-safe benefit responses without exposing confidential insurance documents to external cloud providers.

---

# 🚀 Key Features

## ✅ Multi-Plan & Multi-Year Benefit Intelligence

* Compare benefits across 2024, 2025, and 2026 plans
* Support for Medical, Dental, Vision, and SBC documents
* Dynamic plan routing using:

  * Group Number
  * Group Name
  * Plan Name
  * Plan Type
  * Variant
  * Network

---

## ✅ Local-First AI Architecture

* Runs fully on local infrastructure using Ollama
* No OpenAI or external cloud dependency required
* Insurance PDFs never leave the local environment
* No vector database required

---

## ✅ Structured Retrieval Engine

BENJI combines:

* Topic classification
* Keyword extraction
* Typo-tolerant matching
* Structured chunk ranking
* Deterministic plan filtering
* LLM fallback retrieval

This produces highly explainable and auditable AI responses.

---

## ✅ MCP (Model Context Protocol) Integration

BENJI uses an MCP-based retrieval architecture:

* Client orchestrates reasoning
* MCP server performs surgical retrieval
* Retrieval remains isolated from generation
* Easier debugging and compliance auditing

---

## ✅ Explainable Retrieval

Instead of fuzzy embeddings alone, BENJI retrieves:

* exact benefit sections
* structured cost tables
* network-specific values
* deductible/OOP breakdowns
* service-level coverage details

---

# 🏗️ System Architecture

```text
React UI
   ↓
FastAPI Backend (main.py)
   ↓
LLM Orchestrator (client.py)
   ↓
MCP Retrieval Server (server.py)
   ↓
SQLite + Structured JSON Indices
   ↓
Insurance PDFs
```

---

# 📂 Project Structure

```text
P-RAG/                                # Backend AI & Retrieval Engine
│
├── client.py                         # LLM orchestration layer
├── server.py                         # MCP retrieval server
├── main.py                           # FastAPI backend entry point
│
├── docs/                             # Source insurance PDFs
│   └── 2026/
│       ├── medical/
│       ├── dental/
│       ├── vision/
│       └── sbc/
│           └── 1000016/
│               └── Your Future HSA Qualified Agg NGF - SF/
│                   └── PPO/
│
├── indices/                          # Structured JSON indices
│
├── indexers/
│   ├── indexer.py                    # Master indexing pipeline
│   ├── medical_indexer.py
│   ├── dental_indexer.py
│   ├── vision_indexer.py
│   └── sbc_indexer.py
│
├── prompts/                          # LLM prompt templates
│
├── utils/                            # Shared helper utilities
│
├── p_insurance_index.db              # SQLite structured identity DB
│
├── requirements.txt
├── .env
└── README.md


insurance-frontend/                   # React Frontend Application
│
├── src/
│   ├── components/
│   ├── pages/
│   ├── services/                     # API integrations
│   ├── hooks/
│   └── App.jsx
│
├── public/
├── package.json
├── vite.config.js
└── README.md
```

```

---

# 🧠 Retrieval Pipeline

## 1. User Query

Example:

```text
What is my urgent care copay for the 2026 PPO plan?
```

---

## 2. Category Detection

BENJI identifies:

* Medical
* Dental
* Vision
* SBC

using:

* rule-based routing
* LLM fallback classification

---

## 3. Topic Resolution

The system maps queries into structured insurance topics:

Examples:

| Query                 | Topic         |
| --------------------- | ------------- |
| urgent care cost      | urgent care   |
| blood work benefits   | diagnostic    |
| psychological testing | mental-health |
| braces coverage       | orthodontia   |

---

## 4. Keyword Extraction

LLM extracts retrieval keywords:

Example:

```json
{
  "topics": ["diagnostic"],
  "keywords": ["blood work"]
}
```

---

## 5. Structured Retrieval

The MCP server:

* routes to the correct plan index
* scores matching chunks
* ranks results
* groups related services
* returns structured benefit data

---

## 6. Response Synthesis

The local LLM generates a natural language response using only retrieved structured context.

---

# 📘 Insurance Document Classification

During indexing, BENJI extracts:

| Field        | Description           |
| ------------ | --------------------- |
| year         | Coverage year         |
| group_number | Employer group number |
| group_name   | Employer/group name   |
| plan         | Full plan name        |
| type         | PPO/HMO/EPO/HSA       |
| tier         | Gold/Silver/Bronze    |
| variant      | Standard/Retiree/etc  |
| network      | Network information   |

This creates deterministic plan identity routing.

---

# 🗂️ Indexing Workflow

## Step 1 — Place PDFs

```text
docs/2026/medical/
docs/2026/dental/
docs/2026/vision/
docs/2026/sbc/
```

---

## Step 2 — Build Indices

```bash
python indexers/indexer.py
```

This:

* extracts structured content
* classifies plans
* builds JSON chunk indices
* updates SQLite identity tables

---

# ⚙️ Setup Instructions

## 1. Install Python

Python 3.10+

---

## 2. Install Ollama

Download:
https://ollama.com

Pull model:

```bash
ollama pull llama3.1
```

---

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

Example dependencies:

```bash
fastapi
uvicorn
ollama
pdfplumber
docling
sqlite3
python-dotenv
rapidfuzz
react
```

---

# 🔐 Environment Variables

Create `.env`

```env
DOC_BASE_DIR=./docs
INDEX_OUTPUT_DIR=./indices
DB_PATH=./indexers/p_insurance_index.db
OLLAMA_MODEL=llama3.1
```

---

# ▶️ Running the System

## 1. Start Ollama

```bash
ollama serve
```

---

## 2. Start Backend

```bash
python main.py
```

---

## 3. Start React UI

```bash
npm run dev
```

Open:

```text
http://localhost:5173
```

---

# 🔒 Security & Compliance

## ✅ Local Processing

* No cloud inference required
* No external API dependency
* No PHI leaves local environment

---

## ✅ Deterministic Retrieval

BENJI avoids opaque vector-only retrieval by using:

* structured routing
* explicit plan identity
* explainable scoring

---

## ✅ Auditability

Every retrieval step can be logged:

* selected plan
* selected topic
* selected chunk
* ranking score

---

# 🧪 Example Queries

## Medical

* What is my deductible?
* Show me urgent care benefits
* Compare my 2024 and 2026 PCP copays
* What is my MRI cost?
* Do I need a referral to see a specialist?

---

## Dental

* Show me complete denture benefits
* What are my orthodontia benefits?
* Does my plan cover crowns?

---

## Vision

* What is my eye exam copay?
* Are contact lenses covered?

---

## Multi-Year Comparison

* Compare 2024 vs 2026 deductible
* Compare imaging costs across plans
* Show changes in specialist copays

---

# 🛠️ Troubleshooting

## ❌ Empty Responses

Ensure:

* indices were built successfully
* Ollama is running
* PDFs exist in docs folder

---

## ❌ MCP Errors

Check:

* server.py path
* backend startup logs
* malformed print statements

---

## ❌ Missing Benefits

Rebuild indices:

```bash
python indexers/indexer.py
```

---

# 🔮 Future Enhancements

* Conversation memory
* Multi-user authentication
* PDF highlighting
* Real-time benefit comparison UI
* Employer-specific personalization
* Analytics dashboard
* Azure/OpenShift deployment
* Hybrid semantic + structured retrieval

---

# 📌 Design Philosophy

BENJI was built around five principles:

1. Explainable AI
2. Local-first security
3. Deterministic retrieval
4. Structured insurance intelligence
5. Enterprise-safe architecture

---

# 📄 License

Internal enterprise prototype / demonstration system.
