import asyncio

from app.config import Settings
from app.db.postgres_store import PostgresChatStore
from app.models import Evidence
from app.services.llm import LLMService


def test_rag_settings_are_configured_for_guardrailed_answers():
    settings = Settings()

    assert settings.rag_temperature == 0.1
    assert settings.rag_top_p == 0.9
    assert settings.rag_top_k == 5
    assert settings.rag_min_confidence == 0.55
    assert settings.max_evidence_items == 8


def test_structured_summary_removes_raw_evidence_noise():
    settings = Settings()
    service = LLMService(settings)
    evidence = [
        Evidence(
            id="feature-request:offline-mission-caching-for-low-connectivity-sites",
            source_type="CUSTOMER_GRAPH",
            title="Offline mission caching for low-connectivity sites",
            snippet="Title: Offline mission caching for low-connectivity sites\nProduct Area: missions\nStatus: completed\nAccounts Requesting: Thornfield Construction, Northgate Agriculture\nMentions: 10\nEst. Revenue Impact: $130,614\nGraph context: REQUESTED Thornfield Construction (Account)",
            score=1.0,
        )
    ]

    result = service._normalize_to_structured_summary(
        "Based on the retrieved evidence, Title: Offline mission caching for low-connectivity sites Product Area: missions Status: completed Accounts Requesting: Thornfield Construction, Northgate Agriculture Mentions: 10 Est. Revenue Impact: $130,614 Graph context: REQUESTED Thornfield Construction (Account)",
        evidence,
    )

    assert "Based on the retrieved evidence" not in result
    assert "Graph context" not in result
    assert "Product area: missions" in result
    assert "Status: completed" in result
    assert "Revenue impact: $130,614" in result


def test_chat_history_persists_in_sqlite_fallback(tmp_path):
    settings = Settings()
    settings.dataset_dir = tmp_path
    store = PostgresChatStore(settings)

    asyncio.run(store.connect())
    conversation_id = asyncio.run(store.create_conversation_id())
    asyncio.run(store.add_message(conversation_id, "user", "hello there"))
    messages = asyncio.run(store.get_messages(conversation_id))

    assert messages[-1]["content"] == "hello there"
    assert store.persistence_mode in {"postgres", "sqlite", "memory"}
