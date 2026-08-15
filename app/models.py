from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    customer_graph = "CUSTOMER_GRAPH"
    live_product_docs = "LIVE_PRODUCT_DOCS"
    live_release_notes = "LIVE_RELEASE_NOTES"


class Evidence(BaseModel):
    id: str
    source_type: SourceType
    title: str
    url: str | None = None
    record_id: str | None = None
    entity_type: str | None = None
    snippet: str
    score: float = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalStep(BaseModel):
    name: str
    status: str
    detail: str
    source_type: SourceType | None = None


class Contradiction(BaseModel):
    severity: str = "warning"
    message: str
    evidence_ids: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    concise_answer: str
    reasoning_summary: str
    customer_evidence: list[Evidence] = Field(default_factory=list)
    product_evidence: list[Evidence] = Field(default_factory=list)
    release_evidence: list[Evidence] = Field(default_factory=list)
    source_links: list[str] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    retrieval_steps: list[RetrievalStep] = Field(default_factory=list)
    confidence: str = "insufficient"
    follow_up_questions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IngestionRun(BaseModel):
    run_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    upserted_records: int = 0
    deleted_records: int = 0
    skipped_records: int = 0
    errors: list[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str


class GraphSnapshot(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
