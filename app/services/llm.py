import json
import logging
from typing import Any

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.models import Contradiction, Evidence

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncGroq(api_key=settings.groq_api_key) if settings.groq_api_key else None

    async def grounded_answer(self, question: str, evidence: list[Evidence], contradictions: list[Contradiction]) -> dict[str, Any]:
        if not evidence:
            return {
                "concise_answer": "The available information is insufficient to answer this question.",
                "reasoning_summary": "No customer, documentation, or release evidence was retrieved.",
                "follow_up_questions": ["Can you narrow the question to a customer, feature, issue, or product area?"],
            }
        if self.client:
            try:
                return await self._groq_grounded_answer(question, evidence, contradictions)
            except Exception as exc:
                logger.warning("Groq answer generation failed; using deterministic grounded fallback: %s", exc)
        top = evidence[:5]
        answer = "Based on the retrieved evidence, " + " ".join(e.snippet.splitlines()[0] for e in top[:3])
        return {
            "concise_answer": answer[:700],
            "reasoning_summary": "I combined the highest-scoring customer graph, documentation, and release-note evidence and avoided claims that were not present in those sources.",
            "follow_up_questions": [],
        }

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(2))
    async def _groq_grounded_answer(self, question: str, evidence: list[Evidence], contradictions: list[Contradiction]) -> dict[str, Any]:
        payload = {
            "question": question,
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "contradictions": [c.model_dump() for c in contradictions],
        }
        system = (
            "You are an enterprise GraphRAG answer composer. Use only the supplied evidence. "
            "Return JSON with concise_answer, reasoning_summary, and follow_up_questions. "
            "If evidence is insufficient, say so explicitly. Cite evidence IDs inline where helpful."
        )
        response = await self.client.chat.completions.create(
            model=self.settings.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
