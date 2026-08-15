import re
from collections import defaultdict

from app.config import Settings
from app.db.neo4j_store import Neo4jStore
from app.ingestion.parser import parse_dataset
from app.models import Evidence, GraphEdge, GraphNode, GraphSnapshot, SourceType
from app.services.embeddings import EmbeddingService


class GraphRAGRetriever:
    def __init__(self, settings: Settings, neo4j: Neo4jStore, embeddings: EmbeddingService):
        self.settings = settings
        self.neo4j = neo4j
        self.embeddings = embeddings

    async def retrieve(self, query: str) -> list[Evidence]:
        if not self.neo4j.available:
            return self._local_retrieve(query)
        query_embedding = await self.embeddings.embed(query, input_type="search_query")
        semantic = await self.neo4j.semantic_search(query_embedding, self.settings.vector_top_k)
        fulltext = await self.neo4j.fulltext_search(_lucene_query(query), self.settings.graph_top_k)
        expanded = await self.neo4j.graph_expand(_lucene_query(query), self.settings.graph_top_k)
        analytics = await self.neo4j.analytics(query)
        return rerank_evidence([*analytics, *expanded, *semantic, *fulltext], query, self.settings.evidence_top_k)

    async def graph_snapshot(self) -> GraphSnapshot:
        if self.neo4j.available:
            snapshot = await self.neo4j.snapshot()
            if snapshot.nodes:
                return snapshot
        return self._local_snapshot()

    def _local_retrieve(self, query: str) -> list[Evidence]:
        records = parse_dataset(self.settings.dataset_dir)
        terms = _terms(query)
        scored: list[Evidence] = []
        for record in records:
            haystack = f"{record.title} {record.text}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score == 0:
                continue
            scored.append(
                Evidence(
                    id=f"{record.id}:local",
                    source_type=SourceType.customer_graph,
                    title=record.title,
                    record_id=record.id,
                    entity_type=record.record_type,
                    snippet=record.text[:1200],
                    score=float(score),
                    metadata={"source_file": record.source_file, "mode": "local_fallback"},
                )
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)[: self.settings.evidence_top_k]

    def _local_snapshot(self) -> GraphSnapshot:
        records = parse_dataset(self.settings.dataset_dir)
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        for record in records[:120]:
            for entity in record.entities:
                nodes.setdefault(
                    entity["id"],
                    GraphNode(
                        id=entity["id"],
                        label=entity.get("name") or entity.get("title") or entity["id"],
                        type=entity.get("type", "Entity"),
                        metadata=entity,
                    ),
                )
            for source, target, rel, _ in record.relationships:
                edges.append(GraphEdge(source=source, target=target, label=rel))
        return GraphSnapshot(nodes=list(nodes.values())[:90], edges=edges[:160])


def rerank_evidence(evidence: list[Evidence], query: str, top_k: int) -> list[Evidence]:
    merged: dict[str, Evidence] = {}
    terms = _terms(query)
    for item in evidence:
        lexical = sum(0.08 for term in terms if term in f"{item.title} {item.snippet}".lower())
        item.score = float(item.score or 0) + lexical
        if item.id not in merged or item.score > merged[item.id].score:
            merged[item.id] = item
    by_record: dict[str, int] = defaultdict(int)
    selected: list[Evidence] = []
    for item in sorted(merged.values(), key=lambda e: e.score, reverse=True):
        key = item.record_id or item.id
        if by_record[key] >= 2:
            continue
        selected.append(item)
        by_record[key] += 1
        if len(selected) >= top_k:
            break
    return selected


def _terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9][a-z0-9-]{2,}", query.lower()) if term not in STOP_WORDS]


def _lucene_query(query: str) -> str:
    terms = _terms(query)
    return " OR ".join(f"{term}~" for term in terms[:12]) or query


STOP_WORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "what",
    "which",
    "who",
    "are",
    "was",
    "were",
    "their",
    "this",
    "from",
    "have",
    "has",
    "about",
}
