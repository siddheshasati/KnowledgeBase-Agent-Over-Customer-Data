# FlytBase Knowledge Intelligence Agent

Enterprise-style Knowledge Base Agent using FastAPI, Neo4j for the knowledge graph, Qdrant for vector retrieval, live FlytBase documentation retrieval, Groq for grounded answer synthesis, Cohere embeddings, and Postgres chat history.

## What Is Built

- Markdown customer pipeline for accounts, issues, feature requests, tasks, and meeting notes.
- Idempotent incremental sync with stable record IDs, content hashes, version stamps, soft deletes, and upserted graph entities.
- Neo4j knowledge graph with `Account`, `Issue`, `FeatureRequest`, `Task`, `Meeting`, `Person`, `Plan`, and `ProductFeature` entities.
- Qdrant vector store for embedded customer chunks.
- GraphRAG retrieval that combines Neo4j graph expansion, Neo4j full-text, Qdrant vector retrieval, structured analytics, reranking, and evidence selection.
- Live query-time retrieval for `docs.flytbase.com` and `releases.flytbase.com`; pages are not copied into the customer corpus.
- Agent orchestration for intent detection, query decomposition, source selection, tool calls, evidence collection, contradiction detection, grounded generation, follow-up handling, and memory.
- Light-mode enterprise UI with answer, evidence cards, execution steps, contradictions, history, and animated graph/tree view.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your credentials:

- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`
- `QDRANT_URL`, usually `http://localhost:6333`
- `POSTGRES_DSN`
- `GROQ_API_KEY`
- `COHERE_API_KEY`

Groq works for the orchestrator answer-generation step. Cohere is used for embeddings. If Cohere is absent, the app uses deterministic local vectors for development, but Cohere should be used for the real demo.

For your local Docker services, use:

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
NEO4J_DATABASE=neo4j
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=kb_agent_chunks
POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/kb_agent
VECTOR_STORE=qdrant
```

If you want this repo to start Postgres, Neo4j, and Qdrant:

```powershell
docker compose up -d
```

## Run

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Ingest Customer Knowledge

Run once from the command line:

```powershell
python scripts/ingest_once.py
```

Or use the UI button, or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/ingest/sync-now
```

The sync process:

1. Parses Markdown tables and meeting-note blocks.
2. Creates stable source records such as `account:acct-001`, `issue:iss-0001`, and `feature-request:...`.
3. Hashes every record body.
4. Skips unchanged records.
5. Upserts entities and relationships into Neo4j.
6. Deletes and replaces Qdrant vectors only for changed records.
7. Soft-deletes records missing from the latest source scan and removes their Qdrant points.

## Demo Scenarios

Customer-only:

```text
Which accounts requested offline mission caching and what is the request status?
```

Product docs only:

```text
How do FlytBase docs describe API integrations or webhooks?
```

Cross-source:

```text
What customers requested custom geofence shapes and is that capability supported or released?
```

Incremental update:

```powershell
python scripts/demo_touch_record.py
python scripts/ingest_once.py
```

Then ask:

```text
Which customers requested autonomous low-battery return-to-dock policy?
```

Contradiction detection:

```text
Which requested features appear to be already shipped in release notes?
```

## API

- `GET /health`
- `POST /api/chat`
- `POST /api/ingest`
- `POST /api/ingest/sync-now`
- `GET /api/graph`
- `GET /api/history/{conversation_id}`

## Grounding Rules

The response format separates:

- concise answer
- reasoning summary
- customer evidence
- product evidence
- release evidence
- source links
- contradictions and warnings

The answer composer is instructed to use only retrieved evidence. If evidence is missing from a required source, the API returns a warning and the confidence becomes `partial` or `insufficient`.
