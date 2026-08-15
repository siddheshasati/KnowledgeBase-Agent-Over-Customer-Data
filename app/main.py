from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.orchestrator import KnowledgeAgent
from app.config import get_settings
from app.db.neo4j_store import Neo4jStore
from app.db.postgres_store import PostgresChatStore
from app.db.qdrant_store import QdrantVectorStore
from app.ingestion.sync import IngestionSynchronizer
from app.logging_config import configure_logging
from app.models import ChatRequest, ChatResponse, GraphSnapshot, IngestionRun
from app.retrieval.graphrag import GraphRAGRetriever
from app.retrieval.live_web import LiveFlytBaseRetriever
from app.services.embeddings import EmbeddingService
from app.services.llm import LLMService

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.neo4j = Neo4jStore(settings)
    app.state.chat_store = PostgresChatStore(settings)
    app.state.embeddings = EmbeddingService(settings)
    app.state.vectors = QdrantVectorStore(settings)
    app.state.live = LiveFlytBaseRetriever(settings)
    app.state.graphrag = GraphRAGRetriever(settings, app.state.neo4j, app.state.embeddings, app.state.vectors)
    app.state.llm = LLMService(settings)
    app.state.agent = KnowledgeAgent(settings, app.state.graphrag, app.state.live, app.state.llm, app.state.chat_store)
    app.state.ingestor = IngestionSynchronizer(settings, app.state.neo4j, app.state.embeddings, app.state.vectors)
    await app.state.chat_store.connect()
    await app.state.neo4j.connect()
    await app.state.vectors.connect()
    try:
        snapshot = await app.state.graphrag.graph_snapshot()
        if not snapshot.nodes:
            run = app.state.ingestor.create_run()
            await app.state.ingestor.run(run)
    except Exception:
        pass
    yield
    await app.state.live.close()
    await app.state.vectors.close()
    await app.state.neo4j.close()
    await app.state.chat_store.close()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Enterprise knowledge base agent for customer records, product docs, and release notes.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("frontend/index.html")


@app.get("/health", tags=["System"], summary="Check backend health", description="Returns the connectivity status for Neo4j, Qdrant, and Postgres and whether the app is operating in local fallback mode.")
async def health() -> dict[str, str]:
    neo4j_status = "connected" if app.state.neo4j.available else "not_connected"
    postgres_status = app.state.chat_store.persistence_mode if app.state.chat_store.available else "memory_fallback"
    qdrant_status = "connected" if app.state.vectors.available else "not_connected"
    warnings = settings.configuration_warnings()
    status = "ok" if neo4j_status == "connected" and qdrant_status == "connected" and not warnings else "degraded"
    return {
        "status": status,
        "app": settings.app_name,
        "neo4j": neo4j_status,
        "vector_store": settings.vector_store,
        "qdrant": qdrant_status,
        "postgres": postgres_status,
        "warnings": "; ".join(warnings),
    }


@app.get("/api/capabilities", tags=["Agent Catalog"], summary="List agent capabilities", description="Describes the available agent features across chat, retrieval, graph, and ingestion so they can be tested from Swagger or the frontend.")
async def capabilities() -> dict[str, list[str] | dict[str, str]]:
    return {
        "features": [
            "customer_graph_retrieval",
            "live_docs_search",
            "release_note_search",
            "graph_snapshot_generation",
            "grounded_answering",
            "contradiction_detection",
            "conversation_history",
            "incremental_ingestion",
        ],
        "endpoints": {
            "chat": "/api/chat",
            "graph": "/api/graph",
            "history": "/api/history/{conversation_id}",
            "ingest": "/api/ingest/sync-now",
            "health": "/health",
        },
    }


@app.post("/api/chat", response_model=ChatResponse, tags=["Chat"], summary="Ask the grounded knowledge agent", description="Send a natural-language question to the agent for retrieval across customer graphs, product docs, and release notes.")
async def chat(request: ChatRequest) -> ChatResponse:
    return await app.state.agent.answer(request)


@app.post("/api/ingest", response_model=IngestionRun, tags=["Ingestion"], summary="Queue ingestion", description="Starts an asynchronous background ingestion run for the markdown dataset and dependency graph updates.")
async def ingest(background_tasks: BackgroundTasks) -> IngestionRun:
    run = app.state.ingestor.create_run()
    background_tasks.add_task(app.state.ingestor.run, run)
    return run


@app.post("/api/ingest/sync-now", response_model=IngestionRun, tags=["Ingestion"], summary="Run ingestion immediately", description="Synchronizes the customer dataset now without waiting for a background task.")
async def ingest_sync_now() -> IngestionRun:
    run = app.state.ingestor.create_run()
    return await app.state.ingestor.run(run)


@app.get("/api/graph", response_model=GraphSnapshot, tags=["Graph"], summary="Fetch the knowledge graph snapshot", description="Returns the current entity graph built from Neo4j data or the local dataset fallback when external services are unavailable.")
async def graph() -> GraphSnapshot:
    return await app.state.graphrag.graph_snapshot()


@app.get("/api/history/{conversation_id}", tags=["Memory"], summary="Fetch conversation history", description="Returns recent messages for a conversation so the frontend or API clients can reconstruct the chat context.")
async def history(conversation_id: str) -> list[dict]:
    return await app.state.chat_store.get_messages(conversation_id)
