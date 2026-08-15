import logging
from collections.abc import Iterable
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app.config import Settings
from app.models import Evidence, GraphEdge, GraphNode, GraphSnapshot, SourceType

logger = logging.getLogger(__name__)


ENTITY_LABELS = {
    "Account",
    "Issue",
    "FeatureRequest",
    "Task",
    "Meeting",
    "Person",
    "Plan",
    "ProductFeature",
    "Chunk",
    "SourceRecord",
}


class Neo4jStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.driver: AsyncDriver | None = None
        self.available = False

    async def connect(self) -> None:
        try:
            self.driver = AsyncGraphDatabase.driver(
                self.settings.neo4j_uri,
                auth=(self.settings.neo4j_user, self.settings.neo4j_password),
            )
            await self.driver.verify_connectivity()
            self.available = True
            await self.ensure_schema()
            logger.info("Connected to Neo4j")
        except Exception as exc:
            self.available = False
            logger.warning("Neo4j unavailable; graph operations will return empty results: %s", exc)

    async def close(self) -> None:
        if self.driver:
            await self.driver.close()

    async def execute(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if not self.available or not self.driver:
            return []
        try:
            async with self.driver.session(database=self.settings.neo4j_database) as session:
                result = await session.run(cypher, **params)
                return [record.data() async for record in result]
        except (Neo4jError, ServiceUnavailable) as exc:
            logger.error("Neo4j query failed: %s", exc)
            return []

    async def ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT source_record_id IF NOT EXISTS FOR (r:SourceRecord) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
            "CREATE FULLTEXT INDEX entity_text IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.title, e.text]",
            "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]",
            (
                "CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS FOR (c:Chunk) ON (c.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {self.settings.embedding_dim}, "
                "`vector.similarity_function`: 'cosine'}}}"
            ),
        ]
        for statement in statements:
            await self.execute(statement)

    async def upsert_source_record(self, record: dict[str, Any]) -> None:
        await self.execute(
            """
            MERGE (r:SourceRecord {id: $id})
            SET r += $props,
                r.deleted_at = null,
                r.last_seen_at = datetime()
            """,
            id=record["id"],
            props=record,
        )

    async def mark_records_deleted(self, active_ids: Iterable[str]) -> int:
        rows = await self.execute(
            """
            MATCH (r:SourceRecord)
            WHERE NOT r.id IN $active_ids AND r.deleted_at IS NULL
            SET r.deleted_at = datetime()
            WITH r
            OPTIONAL MATCH (r)-[:HAS_CHUNK]->(c:Chunk)
            SET c.deleted_at = datetime()
            RETURN count(DISTINCT r) AS deleted
            """,
            active_ids=list(active_ids),
        )
        return rows[0]["deleted"] if rows else 0

    async def upsert_entity(self, entity: dict[str, Any], source_record_id: str) -> None:
        label = entity.get("type", "Entity")
        extra_label = f":{label}" if label in ENTITY_LABELS else ""
        await self.execute(
            f"""
            MERGE (e:Entity{extra_label} {{id: $id}})
            SET e += $props,
                e.updated_at = datetime()
            WITH e
            MATCH (r:SourceRecord {{id: $source_record_id}})
            MERGE (r)-[:MENTIONS]->(e)
            """,
            id=entity["id"],
            props=entity,
            source_record_id=source_record_id,
        )

    async def upsert_relationship(self, source_id: str, target_id: str, rel_type: str, props: dict[str, Any] | None = None) -> None:
        safe_type = "".join(ch for ch in rel_type.upper() if ch.isalnum() or ch == "_") or "RELATED_TO"
        await self.execute(
            f"""
            MATCH (a:Entity {{id: $source_id}})
            MATCH (b:Entity {{id: $target_id}})
            MERGE (a)-[r:{safe_type}]->(b)
            SET r += $props,
                r.updated_at = datetime()
            """,
            source_id=source_id,
            target_id=target_id,
            props=props or {},
        )

    async def replace_chunks(self, record_id: str, chunks: list[dict[str, Any]]) -> None:
        await self.execute(
            """
            MATCH (r:SourceRecord {id: $record_id})-[rel:HAS_CHUNK]->(c:Chunk)
            DETACH DELETE c
            """,
            record_id=record_id,
        )
        for chunk in chunks:
            await self.execute(
                """
                MATCH (r:SourceRecord {id: $record_id})
                CREATE (c:Chunk {
                    id: $id,
                    text: $text,
                    embedding: $embedding,
                    source_file: $source_file,
                    record_id: $record_id,
                    title: $title,
                    entity_type: $entity_type,
                    content_hash: $content_hash,
                    created_at: datetime()
                })
                MERGE (r)-[:HAS_CHUNK]->(c)
                """,
                record_id=record_id,
                **chunk,
            )

    async def get_record_hashes(self) -> dict[str, str]:
        rows = await self.execute("MATCH (r:SourceRecord) RETURN r.id AS id, r.content_hash AS hash")
        return {row["id"]: row["hash"] for row in rows}

    async def semantic_search(self, embedding: list[float], top_k: int) -> list[Evidence]:
        rows = await self.execute(
            """
            CALL db.index.vector.queryNodes('chunk_embedding', $top_k, $embedding)
            YIELD node, score
            MATCH (r:SourceRecord)-[:HAS_CHUNK]->(node)
            WHERE r.deleted_at IS NULL
            RETURN node.id AS id, node.text AS snippet, node.title AS title,
                   node.record_id AS record_id, node.entity_type AS entity_type,
                   node.source_file AS source_file, score
            ORDER BY score DESC
            """,
            embedding=embedding,
            top_k=top_k,
        )
        return [
            Evidence(
                id=row["id"],
                source_type=SourceType.customer_graph,
                title=row.get("title") or row["record_id"],
                record_id=row["record_id"],
                entity_type=row.get("entity_type"),
                snippet=row["snippet"],
                score=float(row.get("score") or 0),
                metadata={"source_file": row.get("source_file")},
            )
            for row in rows
        ]

    async def fulltext_search(self, query: str, top_k: int) -> list[Evidence]:
        rows = await self.execute(
            """
            CALL db.index.fulltext.queryNodes('chunk_text', $query, {limit: $top_k})
            YIELD node, score
            MATCH (r:SourceRecord)-[:HAS_CHUNK]->(node)
            WHERE r.deleted_at IS NULL
            RETURN node.id AS id, node.text AS snippet, node.title AS title,
                   node.record_id AS record_id, node.entity_type AS entity_type,
                   node.source_file AS source_file, score
            ORDER BY score DESC
            """,
            query=query,
            top_k=top_k,
        )
        return [
            Evidence(
                id=row["id"],
                source_type=SourceType.customer_graph,
                title=row.get("title") or row["record_id"],
                record_id=row["record_id"],
                entity_type=row.get("entity_type"),
                snippet=row["snippet"],
                score=float(row.get("score") or 0),
                metadata={"source_file": row.get("source_file")},
            )
            for row in rows
        ]

    async def graph_expand(self, query: str, top_k: int) -> list[Evidence]:
        rows = await self.execute(
            """
            CALL db.index.fulltext.queryNodes('entity_text', $query, {limit: $top_k})
            YIELD node, score
            OPTIONAL MATCH path=(node)-[rel]-(neighbor:Entity)
            WITH node, score, collect(DISTINCT {
                rel: type(rel),
                id: neighbor.id,
                name: coalesce(neighbor.name, neighbor.title, neighbor.id),
                type: neighbor.type
            })[0..8] AS neighbors
            OPTIONAL MATCH (r:SourceRecord)-[:MENTIONS]->(node)
            RETURN node.id AS id, coalesce(node.name, node.title, node.id) AS title,
                   node.type AS entity_type, coalesce(node.text, node.title, node.name) AS snippet,
                   collect(DISTINCT r.id)[0] AS record_id, neighbors, score
            ORDER BY score DESC
            """,
            query=query,
            top_k=top_k,
        )
        evidence = []
        for row in rows:
            relations = "; ".join(
                f"{n.get('rel')} {n.get('name')} ({n.get('type')})" for n in row.get("neighbors", []) if n.get("name")
            )
            snippet = row.get("snippet") or row["title"]
            if relations:
                snippet = f"{snippet}\nGraph context: {relations}"
            evidence.append(
                Evidence(
                    id=row["id"],
                    source_type=SourceType.customer_graph,
                    title=row["title"],
                    record_id=row.get("record_id"),
                    entity_type=row.get("entity_type"),
                    snippet=snippet,
                    score=float(row.get("score") or 0),
                    metadata={"neighbors": row.get("neighbors", [])},
                )
            )
        return evidence

    async def analytics(self, query: str) -> list[Evidence]:
        lowered = query.lower()
        if "most requested" in lowered or "top requested" in lowered:
            rows = await self.execute(
                """
                MATCH (fr:Entity:FeatureRequest)
                RETURN fr.id AS id, fr.title AS title, fr.mentions AS mentions,
                       fr.status AS status, fr.product_area AS area, fr.revenue_impact AS impact,
                       fr.accounts AS accounts
                ORDER BY coalesce(fr.mentions, 0) DESC
                LIMIT 10
                """
            )
            return [
                Evidence(
                    id=row["id"],
                    source_type=SourceType.customer_graph,
                    title=row["title"],
                    record_id=row["id"],
                    entity_type="FeatureRequest",
                    snippet=(
                        f"{row['title']} has {row.get('mentions')} mentions, status {row.get('status')}, "
                        f"area {row.get('area')}, estimated impact {row.get('impact')}. "
                        f"Accounts: {row.get('accounts')}."
                    ),
                    score=float(row.get("mentions") or 0),
                )
                for row in rows
            ]
        if "affected by" in lowered or "accounts affected" in lowered:
            rows = await self.execute(
                """
                MATCH (a:Entity:Account)<-[:AFFECTS]-(i:Entity:Issue)
                RETURN a.id AS account_id, a.name AS account, collect(i.title)[0..5] AS issues, count(i) AS issue_count
                ORDER BY issue_count DESC
                LIMIT 10
                """
            )
            return [
                Evidence(
                    id=row["account_id"],
                    source_type=SourceType.customer_graph,
                    title=row["account"],
                    record_id=row["account_id"],
                    entity_type="Account",
                    snippet=f"{row['account']} has {row['issue_count']} linked issues: {', '.join(row['issues'])}.",
                    score=float(row["issue_count"]),
                )
                for row in rows
            ]
        return []

    async def snapshot(self) -> GraphSnapshot:
        rows = await self.execute(
            """
            MATCH (n:Entity)
            WITH n LIMIT 90
            OPTIONAL MATCH (n)-[r]->(m:Entity)
            WHERE m IS NOT NULL
            RETURN collect(DISTINCT {
                id: n.id,
                label: coalesce(n.name, n.title, n.id),
                type: n.type,
                metadata: properties(n)
            }) AS nodes,
            collect(DISTINCT {source: n.id, target: m.id, label: type(r)})[0..160] AS edges
            """
        )
        if not rows:
            return GraphSnapshot(nodes=[], edges=[])
        nodes = [GraphNode(**node) for node in rows[0].get("nodes", []) if node.get("id")]
        edges = [GraphEdge(**edge) for edge in rows[0].get("edges", []) if edge.get("target")]
        return GraphSnapshot(nodes=nodes, edges=edges)
