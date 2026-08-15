import logging
import uuid
from datetime import datetime

from app.config import Settings
from app.db.neo4j_store import Neo4jStore
from app.ingestion.parser import ParsedRecord, parse_dataset
from app.models import IngestionRun
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class IngestionSynchronizer:
    def __init__(self, settings: Settings, neo4j: Neo4jStore, embeddings: EmbeddingService):
        self.settings = settings
        self.neo4j = neo4j
        self.embeddings = embeddings

    def create_run(self) -> IngestionRun:
        return IngestionRun(run_id=str(uuid.uuid4()), status="queued", started_at=datetime.utcnow())

    async def run(self, run: IngestionRun) -> IngestionRun:
        run.status = "running"
        try:
            records = parse_dataset(self.settings.dataset_dir)
            known_hashes = await self.neo4j.get_record_hashes()
            active_ids = []
            for record in records:
                active_ids.append(record.id)
                if known_hashes.get(record.id) == record.content_hash:
                    run.skipped_records += 1
                    continue
                await self._upsert_record(record)
                run.upserted_records += 1
            run.deleted_records = await self.neo4j.mark_records_deleted(active_ids)
            run.status = "completed"
        except Exception as exc:
            logger.exception("Ingestion run failed")
            run.status = "failed"
            run.errors.append(str(exc))
        finally:
            run.finished_at = datetime.utcnow()
        return run

    async def _upsert_record(self, record: ParsedRecord) -> None:
        await self.neo4j.upsert_source_record(record.source_record())
        for entity in record.entities:
            await self.neo4j.upsert_entity(entity, record.id)
        for source_id, target_id, rel_type, props in record.relationships:
            await self.neo4j.upsert_relationship(source_id, target_id, rel_type, props)
        chunks = await self._chunks_for_record(record)
        await self.neo4j.replace_chunks(record.id, chunks)

    async def _chunks_for_record(self, record: ParsedRecord) -> list[dict]:
        text = record.text.strip()
        windows = _chunk_text(text, max_chars=1400, overlap=180)
        embeddings = await self.embeddings.embed_many(windows, input_type="search_document")
        return [
            {
                "id": f"{record.id}:chunk:{idx}",
                "text": chunk,
                "embedding": embeddings[idx],
                "source_file": record.source_file,
                "title": record.title,
                "entity_type": record.record_type,
                "content_hash": record.content_hash,
            }
            for idx, chunk in enumerate(windows)
        ]


def _chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(0, end - overlap)
    return chunks
