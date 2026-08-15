import asyncio

from app.config import get_settings
from app.db.neo4j_store import Neo4jStore
from app.ingestion.sync import IngestionSynchronizer
from app.logging_config import configure_logging
from app.services.embeddings import EmbeddingService


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    neo4j = Neo4jStore(settings)
    await neo4j.connect()
    embeddings = EmbeddingService(settings)
    sync = IngestionSynchronizer(settings, neo4j, embeddings)
    run = await sync.run(sync.create_run())
    print(run.model_dump_json(indent=2))
    await neo4j.close()


if __name__ == "__main__":
    asyncio.run(main())
