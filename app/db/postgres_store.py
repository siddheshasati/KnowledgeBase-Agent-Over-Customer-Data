import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

try:
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional dependency when libpq is missing on the host.
    AsyncConnection = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]

from app.config import Settings

logger = logging.getLogger(__name__)


class PostgresChatStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn: AsyncConnection | None = None
        self.sqlite_conn: sqlite3.Connection | None = None
        self.available = False
        self.persistence_mode = "memory"
        self.memory: dict[str, list[dict[str, Any]]] = {}
        self.sqlite_path = Path.cwd() / "chat_history.sqlite3"

    async def connect(self) -> None:
        if AsyncConnection is not None and dict_row is not None and Jsonb is not None:
            try:
                self.conn = await AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row)
                await self.ensure_schema()
                self.available = True
                self.persistence_mode = "postgres"
                logger.info("Connected to Postgres")
                return
            except Exception as exc:
                logger.warning("Postgres unavailable; falling back to SQLite: %s", exc)

        self.persistence_mode = "sqlite"
        self.sqlite_conn = sqlite3.connect(self.sqlite_path)
        self.sqlite_conn.row_factory = sqlite3.Row
        await self.ensure_schema()
        self.available = True
        logger.warning("SQLite fallback active for chat history at %s", self.sqlite_path)

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
        if self.sqlite_conn is not None:
            self.sqlite_conn.close()
            self.sqlite_conn = None

    async def ensure_schema(self) -> None:
        if self.conn is not None:
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
            return

        if self.sqlite_conn is not None:
            self.sqlite_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self.sqlite_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                """
            )
            self.sqlite_conn.commit()

    async def create_conversation_id(self) -> str:
        return str(uuid.uuid4())

    async def add_message(self, conversation_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}

        if self.conn is not None and self.persistence_mode == "postgres":
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
            return

        if self.sqlite_conn is not None:
            self.sqlite_conn.execute(
                "INSERT OR IGNORE INTO conversations (id, created_at, updated_at) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (conversation_id,),
            )
            self.sqlite_conn.execute(
                "INSERT INTO messages (conversation_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (conversation_id, role, content, json.dumps(metadata, ensure_ascii=True)),
            )
            self.sqlite_conn.commit()
            return

        self.memory.setdefault(conversation_id, []).append({"role": role, "content": content, "metadata": metadata})

    async def get_messages(self, conversation_id: str, limit: int = 12) -> list[dict[str, Any]]:
        if self.conn is not None and self.persistence_mode == "postgres":
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

        if self.sqlite_conn is not None:
            rows = self.sqlite_conn.execute(
                """
                SELECT role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
            return [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        return self.memory.get(conversation_id, [])[-limit:]
