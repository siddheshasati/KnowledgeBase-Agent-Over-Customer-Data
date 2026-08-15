import logging
import re
from dataclasses import dataclass

from app.config import Settings
from app.db.postgres_store import PostgresChatStore
from app.models import ChatRequest, ChatResponse, Contradiction, Evidence, RetrievalStep, SourceType
from app.retrieval.graphrag import GraphRAGRetriever, rerank_evidence
from app.retrieval.live_web import LiveFlytBaseRetriever
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


@dataclass
class AgentPlan:
    intent: str
    subqueries: list[str]
    sources: list[SourceType]


class KnowledgeAgent:
    def __init__(
        self,
        settings: Settings,
        graphrag: GraphRAGRetriever,
        live: LiveFlytBaseRetriever,
        llm: LLMService,
        chat_store: PostgresChatStore,
    ):
        self.settings = settings
        self.graphrag = graphrag
        self.live = live
        self.llm = llm
        self.chat_store = chat_store

    async def answer(self, request: ChatRequest) -> ChatResponse:
        conversation_id = request.conversation_id or await self.chat_store.create_conversation_id()
        memory = await self.chat_store.get_messages(conversation_id)
        plan = self._plan(request.message, memory)
        steps = [
            RetrievalStep(name="intent_detection", status="completed", detail=f"Intent: {plan.intent}"),
            RetrievalStep(name="query_decomposition", status="completed", detail="; ".join(plan.subqueries)),
            RetrievalStep(name="source_selection", status="completed", detail=", ".join(source.value for source in plan.sources)),
        ]

        evidence: list[Evidence] = []
        for source in plan.sources:
            source_evidence = await self._call_tool(source, plan.subqueries, steps)
            evidence.extend(source_evidence)

        evidence = rerank_evidence(evidence, request.message, self.settings.evidence_top_k * 2)
        contradictions = self._detect_contradictions(evidence)
        grounded = await self.llm.grounded_answer(request.message, evidence[: self.settings.evidence_top_k], contradictions)
        customer, product, release = self._split_evidence(evidence)
        confidence_score = self._confidence_score(evidence, contradictions, plan.sources)
        confidence = self._confidence(evidence, contradictions, plan.sources)
        warnings = self._warnings(evidence, plan.sources)
        links = sorted({item.url for item in evidence if item.url})

        concise_answer = grounded.get("concise_answer")
        if isinstance(concise_answer, list):
            concise_answer = " ".join(str(item) for item in concise_answer if item)
        if not isinstance(concise_answer, str) or not concise_answer.strip():
            concise_answer = "The available information is insufficient to answer this question."

        reasoning_summary = grounded.get("reasoning_summary")
        if not isinstance(reasoning_summary, str) or not reasoning_summary.strip():
            reasoning_summary = "The answer was generated only from retrieved evidence."

        follow_up_questions = grounded.get("follow_up_questions")
        if isinstance(follow_up_questions, str):
            follow_up_questions = [follow_up_questions]
        elif not isinstance(follow_up_questions, list):
            follow_up_questions = []

        response = ChatResponse(
            conversation_id=conversation_id,
            concise_answer=concise_answer,
            reasoning_summary=reasoning_summary,
            customer_evidence=customer[: self.settings.evidence_top_k],
            product_evidence=product[: self.settings.evidence_top_k],
            release_evidence=release[: self.settings.evidence_top_k],
            source_links=links,
            contradictions=contradictions,
            warnings=warnings,
            retrieval_steps=steps,
            confidence=confidence,
            confidence_score=confidence_score,
            follow_up_questions=[str(item) for item in follow_up_questions if item],
        )
        await self.chat_store.add_message(
            conversation_id,
            "user",
            request.message,
            {"plan": {"intent": plan.intent, "subqueries": plan.subqueries, "sources": [source.value for source in plan.sources]}},
        )
        await self.chat_store.add_message(conversation_id, "assistant", response.concise_answer, response.model_dump(mode="json"))
        return response

    async def _call_tool(self, source: SourceType, subqueries: list[str], steps: list[RetrievalStep]) -> list[Evidence]:
        evidence: list[Evidence] = []
        tool_name = source.value.lower()
        for subquery in subqueries:
            try:
                if source == SourceType.customer_graph:
                    result = await self.graphrag.retrieve(subquery)
                elif source == SourceType.live_product_docs:
                    result = await self.live.search_docs(subquery)
                elif source == SourceType.live_release_notes:
                    result = await self.live.search_releases(subquery)
                else:
                    result = []
                evidence.extend(result)
                steps.append(
                    RetrievalStep(
                        name=f"tool_call:{tool_name}",
                        status="completed",
                        detail=f"{subquery} -> {len(result)} evidence items",
                        source_type=source,
                    )
                )
            except Exception as exc:
                logger.exception("Tool call failed")
                steps.append(
                    RetrievalStep(
                        name=f"tool_call:{tool_name}",
                        status="failed",
                        detail=str(exc),
                        source_type=source,
                    )
                )
        return evidence

    def _plan(self, question: str, memory: list[dict]) -> AgentPlan:
        lowered = question.lower()
        subqueries = self._decompose(question)
        intent = "cross_source_reasoning" if any(word in lowered for word in ["customers", "accounts", "requested"]) and any(
            word in lowered for word in ["supported", "docs", "release", "shipped", "available", "plans"]
        ) else "knowledge_lookup"
        if any(word in lowered for word in ["most requested", "requests by", "affected by", "how many", "analytics", "frequently"]):
            intent = "analytics"

        sources: list[SourceType] = []
        customer_terms = [
            "customer",
            "customers",
            "account",
            "accounts",
            "issue",
            "issues",
            "task",
            "meeting",
            "feature request",
            "requested",
            "industry",
            "arr",
            "health",
        ]
        docs_terms = ["docs", "documentation", "how", "configure", "support", "supported", "plan", "plans", "api", "guide"]
        release_terms = ["release", "released", "shipped", "changelog", "already", "new version", "launch"]
        if any(term in lowered for term in customer_terms):
            sources.append(SourceType.customer_graph)
        if any(term in lowered for term in docs_terms):
            sources.append(SourceType.live_product_docs)
        if any(term in lowered for term in release_terms):
            sources.append(SourceType.live_release_notes)
        if intent == "cross_source_reasoning":
            for source in [SourceType.customer_graph, SourceType.live_product_docs, SourceType.live_release_notes]:
                if source not in sources:
                    sources.append(source)
        if not sources:
            sources = [SourceType.customer_graph, SourceType.live_product_docs]
        if memory and any(token in lowered for token in ["that", "those", "them", "it", "this"]):
            previous = " ".join(msg.get("content", "") for msg in memory[-4:] if msg.get("role") == "user")
            subqueries.append(f"{previous} {question}".strip())
        return AgentPlan(intent=intent, subqueries=subqueries, sources=sources)

    def _decompose(self, question: str) -> list[str]:
        parts = re.split(r"\s+(?:and|also|plus)\s+|\?\s*", question)
        subqueries = [part.strip(" ?.") for part in parts if len(part.strip()) > 3]
        return subqueries[:4] or [question]

    def _detect_contradictions(self, evidence: list[Evidence]) -> list[Contradiction]:
        contradictions: list[Contradiction] = []
        feature_items = [
            item
            for item in evidence
            if item.source_type == SourceType.customer_graph
            and (item.entity_type == "FeatureRequest" or "Status:" in item.snippet and "Accounts Requesting:" in item.snippet)
        ]
        release_text = " ".join(
            f"{item.title} {item.snippet}" for item in evidence if item.source_type == SourceType.live_release_notes
        ).lower()
        product_text = " ".join(f"{item.title} {item.snippet}" for item in evidence if item.source_type == SourceType.live_product_docs).lower()
        shipped_words = ["released", "available", "launched", "support", "supported", "introduced", "now supports"]
        for item in feature_items:
            status = _field(item.snippet, "Status").lower() or str(item.metadata.get("status", "")).lower()
            title_terms = [term for term in re.findall(r"[a-z0-9]{4,}", item.title.lower()) if term not in FEATURE_STOP_WORDS]
            overlap = sum(1 for term in title_terms if term in release_text or term in product_text)
            shipped_signal = any(word in release_text or word in product_text for word in shipped_words)
            if status in {"new", "in_progress", "declined"} and overlap >= max(1, min(3, len(title_terms))) and shipped_signal:
                contradictions.append(
                    Contradiction(
                        severity="warning",
                        message=f"{item.title} is recorded as customer-request status '{status}', but live product/release evidence appears to mention related shipped or supported capability.",
                        evidence_ids=[item.id],
                    )
                )
            if status == "completed" and not shipped_signal and (product_text or release_text):
                contradictions.append(
                    Contradiction(
                        severity="notice",
                        message=f"{item.title} is marked completed in customer data, but the retrieved live product/release evidence did not clearly confirm current availability.",
                        evidence_ids=[item.id],
                    )
                )
        return contradictions[:5]

    def _split_evidence(self, evidence: list[Evidence]) -> tuple[list[Evidence], list[Evidence], list[Evidence]]:
        customer = [item for item in evidence if item.source_type == SourceType.customer_graph]
        product = [item for item in evidence if item.source_type == SourceType.live_product_docs]
        release = [item for item in evidence if item.source_type == SourceType.live_release_notes]
        return customer, product, release

    def _confidence_score(self, evidence: list[Evidence], contradictions: list[Contradiction], sources: list[SourceType]) -> float:
        if not evidence:
            return 0.0
        base = min(1.0, len(evidence) / max(1, self.settings.max_evidence_items))
        source_coverage = len({item.source_type for item in evidence}) / max(1, len(sources))
        contradiction_penalty = 0.30 if contradictions else 0.0
        score = min(1.0, max(0.0, base * 0.6 + source_coverage * 0.4 - contradiction_penalty))
        return round(score, 3)

    def _confidence(self, evidence: list[Evidence], contradictions: list[Contradiction], sources: list[SourceType]) -> str:
        score = self._confidence_score(evidence, contradictions, sources)
        if not evidence:
            return "insufficient"
        if contradictions and score < self.settings.rag_min_confidence:
            return "mixed"
        if score >= 0.8:
            return "grounded"
        if score >= self.settings.rag_min_confidence:
            return "partial"
        return "insufficient"

    def _warnings(self, evidence: list[Evidence], sources: list[SourceType]) -> list[str]:
        present = {item.source_type for item in evidence}
        warnings = []
        for source in sources:
            if source not in present:
                warnings.append(f"No evidence was retrieved from {source.value}.")
        if any(item.metadata.get("mode") == "local_fallback" for item in evidence):
            warnings.append("Neo4j is not connected, so customer evidence came from a read-only local dataset fallback.")
        return warnings


def _field(text: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


FEATURE_STOP_WORDS = {
    "with",
    "from",
    "into",
    "bulk",
    "custom",
    "configurable",
    "support",
    "account",
    "site",
    "device",
    "devices",
}
