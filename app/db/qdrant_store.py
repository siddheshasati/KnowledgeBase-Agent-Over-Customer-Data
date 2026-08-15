import hashlib
import logging
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, FieldCondition, Filter, MatchAny, PointStruct, VectorParams

from app.config import Settings
from app.models import Evidence, SourceType

logger = logging.getLogger(__name__)


class QdrantVectorStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: AsyncQdrantClient | None = None
        self.available = False

    async def connect(self) -> None:
        try:
            self.client = AsyncQdrantClient(url=self.settings.qdrant_url, api_key=self.settings.qdrant_api_key or None)
            await self.client.get_collections()
            await self.ensure_collection()
            self.available = True
            logger.info("Connected to Qdrant")
        except Exception as exc:
            self.available = False
            logger.warning("Qdrant unavailable; semantic retrieval will use local fallback: %s", exc)

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    async def ensure_collection(self) -> None:
        if not self.client:
            return
        try:
            await self.client.get_collection(self.settings.qdrant_collection)
        except (UnexpectedResponse, ValueError):
            await self.client.create_collection(
                collection_name=self.settings.qdrant_collection,
                vectors_config=VectorParams(size=self.settings.embedding_dim, distance=Distance.COSINE),
            )

    async def upsert_chunks(self, chunks: list[dict[str, Any]]) -> None:
        if not self.available or not self.client or not chunks:
            return
        points = [
            PointStruct(
                id=_point_id(chunk["id"]),
                vector=chunk["embedding"],
                payload={
                    "chunk_id": chunk["id"],
                    "record_id": chunk["record_id"],
                    "source_file": chunk["source_file"],
                    "title": chunk["title"],
                    "entity_type": chunk["entity_type"],
                    "content_hash": chunk["content_hash"],
                    "text": chunk["text"],
                },
            )
            for chunk in chunks
        ]
        await self.client.upsert(collection_name=self.settings.qdrant_collection, points=points, wait=True)

    async def delete_record_chunks(self, record_ids: list[str]) -> None:
        if not self.available or not self.client or not record_ids:
            return
        await self.client.delete(
            collection_name=self.settings.qdrant_collection,
            points_selector=Filter(
                must=[FieldCondition(key="record_id", match=MatchAny(any=record_ids))]
            ),
            wait=True,
        )

    async def search(self, embedding: list[float], top_k: int) -> list[Evidence]:
        if not self.available or not self.client:
            return []
        results = await self.client.search(
            collection_name=self.settings.qdrant_collection,
            query_vector=embedding,
            limit=top_k,
            with_payload=True,
        )
        evidence: list[Evidence] = []
        for result in results:
            payload = result.payload or {}
            evidence.append(
                Evidence(
                    id=str(payload.get("chunk_id") or result.id),
                    source_type=SourceType.customer_graph,
                    title=str(payload.get("title") or payload.get("record_id") or result.id),
                    record_id=payload.get("record_id"),
                    entity_type=payload.get("entity_type"),
                    snippet=str(payload.get("text") or ""),
                    score=float(result.score or 0),
                    metadata={
                        "source_file": payload.get("source_file"),
                        "content_hash": payload.get("content_hash"),
                        "vector_store": "qdrant",
                    },
                )
            )
        return evidence


def _point_id(value: str) -> str:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
