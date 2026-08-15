import logging
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import Settings

logger = logging.getLogger(__name__)


class PostgresChatStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn: AsyncConnection | None = None
        self.available = False
        self.memory: dict[str, list[dict[str, Any]]] = {}

    async def connect(self) -> None:
        try:
            self.conn = await AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row)
            await self.ensure_schema()
            self.available = True
            logger.info("Connected to Postgres")
        except Exception as exc:
            self.available = False
            logger.warning("Postgres unavailable; chat history will use process memory: %s", exc)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def ensure_schema(self) -> None:
        if not self.conn:
            return
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            await self.conn.commit()

    async def create_conversation_id(self) -> str:
        return str(uuid.uuid4())

    async def add_message(self, conversation_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        if not self.available or not self.conn:
            self.memory.setdefault(conversation_id, []).append({"role": role, "content": content, "metadata": metadata})
            return
        async with self.conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO conversations (id) VALUES (%s) ON CONFLICT (id) DO UPDATE SET updated_at = now()",
                (conversation_id,),
            )
            await cur.execute(
                "INSERT INTO messages (conversation_id, role, content, metadata) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, content, Jsonb(metadata)),
            )
            await self.conn.commit()

    async def get_messages(self, conversation_id: str, limit: int = 12) -> list[dict[str, Any]]:
        if not self.available or not self.conn:
            return self.memory.get(conversation_id, [])[-limit:]
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (conversation_id, limit),
            )
            rows = await cur.fetchall()
            return list(reversed(rows))
