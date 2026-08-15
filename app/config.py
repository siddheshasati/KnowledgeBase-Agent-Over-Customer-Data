from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "FlytBase Knowledge Intelligence"
    app_env: str = "development"
    log_level: str = "INFO"
    dataset_dir: Path = Path("se-dataset")

    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "Neo4jSecure2024!"
    neo4j_database: str = "neo4j"
    vector_store: str = "qdrant"
    neo4j_aura_client_id: str | None = None
    neo4j_aura_client_secret: str | None = None

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "kb_agent_chunks"

    postgres_dsn: str = "postgresql://postgres:postgres@localhost:5432/kb_agent"

    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    cohere_api_key: str | None = None
    cohere_embed_model: str = "embed-english-v3.0"
    embedding_dim: int = 1024

    live_docs_allowed_hosts: str = "docs.flytbase.com,releases.flytbase.com"
    live_fetch_timeout_seconds: float = 12
    live_max_pages: int = 6

    graph_top_k: int = 10
    vector_top_k: int = 8
    evidence_top_k: int = 8
    max_evidence_items: int = 8
    rag_temperature: float = 0.1
    rag_top_p: float = 0.9
    rag_top_k: int = 5
    rag_min_confidence: float = 0.55
    rag_max_tokens: int = 512

    @property
    def live_hosts(self) -> set[str]:
        return {host.strip().lower() for host in self.live_docs_allowed_hosts.split(",") if host.strip()}

    def configuration_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.vector_store.lower() != "qdrant":
            warnings.append("VECTOR_STORE is not 'qdrant'; this build expects Qdrant for semantic retrieval.")
        if self.neo4j_aura_client_id or self.neo4j_aura_client_secret:
            warnings.append(
                "NEO4J_AURA_CLIENT_ID/SECRET are Aura management credentials and cannot authenticate GraphRAG queries. "
                "Use NEO4J_URI, NEO4J_USER, and NEO4J_PASSWORD from your Aura database instance."
            )
        if self.neo4j_password in {"", "password", "change-me", "neo4j"}:
            warnings.append("NEO4J_PASSWORD is not configured with a real database password.")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
