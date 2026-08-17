import json
import logging
import re
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

    def _extract_accounts(self, evidence: list[Evidence]) -> str:
        accounts = []
        for item in evidence:
            for chunk in [item.title, item.snippet, item.record_id or ""]:
                if not chunk:
                    continue
                matches = re.findall(r"([A-Z][A-Za-z0-9&'\-]+(?:\s+[A-Z][A-Za-z0-9&'\-]+)*)\s*\((?:Account|Customer)\)", chunk)
                if matches:
                    accounts.extend(matches)
                elif "Account:" in chunk and ":" in chunk:
                    parts = chunk.split("Account:")
                    if len(parts) > 1:
                        candidate = parts[1].split(" ")[:8]
                        accounts.append(" ".join(candidate).strip())
        seen = []
        ordered = []
        for account in accounts:
            cleaned = re.sub(r"\s+", " ", account).strip(" ;:-")
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
                ordered.append(cleaned)
        return ", ".join(ordered[:8]) if ordered else "Multiple customer accounts"

    def _extract_title(self, evidence: list[Evidence]) -> str:
        for item in evidence:
            if item.title and item.title.strip():
                return item.title.strip()
        return "Customer request summary"

    def _extract_status(self, evidence: list[Evidence]) -> str:
        text = " ".join((item.snippet or "") for item in evidence)
        for pattern in [r"Status:\s*([^;\n]+)", r"status:\s*([^;\n]+)", r"Status\s*:\s*([A-Za-z]+)"]:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return "In review"

    def _extract_product_area(self, evidence: list[Evidence]) -> str:
        text = " ".join((item.snippet or "") for item in evidence)
        match = re.search(r"Product Area\s*:\s*([^;\n]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return "Customer-facing product work"

    def _extract_revenue_impact(self, evidence: list[Evidence]) -> str:
        text = " ".join((item.snippet or "") for item in evidence)
        match = re.search(r"Est\.?\s*Revenue Impact\s*:\s*\$?([0-9,]+)", text, flags=re.IGNORECASE)
        if match:
            return f"${match.group(1)}"
        return "Not explicitly stated"

    def _normalize_to_structured_summary(self, answer: str, evidence: list[Evidence]) -> str:
        text = (answer or "").strip()
        if not text:
            text = "Customer request summary"

        parsed_title = re.search(r"Title\s*:\s*([^\n]+?)(?:\s+Product Area|\s+Status|\s+Accounts|$)", text, flags=re.IGNORECASE)
        parsed_product = re.search(r"Product Area\s*:\s*([^\n]+?)(?:\s+Status|\s+Accounts|$)", text, flags=re.IGNORECASE)
        parsed_status = re.search(r"Status\s*:\s*([^\n]+?)(?:\s+Accounts|\s+Mentions|$)", text, flags=re.IGNORECASE)
        parsed_accounts = re.search(r"Accounts Requesting\s*:\s*([^\n]+?)(?:\s+Mentions|\s+Est\.|$)", text, flags=re.IGNORECASE)
        parsed_revenue = re.search(r"Est\.?\s*Revenue Impact\s*:\s*([^\n]+?)(?:\s+Graph|$)", text, flags=re.IGNORECASE)

        title = parsed_title.group(1).strip() if parsed_title else self._extract_title(evidence)
        product_area = parsed_product.group(1).strip() if parsed_product else self._extract_product_area(evidence)
        status = parsed_status.group(1).strip() if parsed_status else self._extract_status(evidence)
        accounts = parsed_accounts.group(1).strip() if parsed_accounts else self._extract_accounts(evidence)
        revenue = parsed_revenue.group(1).strip() if parsed_revenue else self._extract_revenue_impact(evidence)

        clean_summary = re.sub(r"(?is)^\s*based on the retrieved evidence,\s*", "", text)
        clean_summary = re.sub(r"(?is)\bgraph context\b\s*:\s*.*?(?=(?:\btitle\b\s*:|\bproduct area\b\s*:|\bstatus\b\s*:|\baccounts\b\s*:|\bmentions\b\s*:|\best\.?\s*revenue impact\b\s*:|$))", "", clean_summary)
        clean_summary = re.sub(r"(?is)\b(?:title|product area|status|accounts requesting|accounts|mentions|est\.?\s*revenue impact)\b\s*:\s*", "", clean_summary)
        clean_summary = re.sub(r"\s+", " ", clean_summary).strip(" -:;")
        clean_summary = clean_summary.strip()

        clean_summary = (
            f"This request relates to {title}, a {product_area} initiative tracked as {status}. "
            f"It was requested by {accounts}, with an estimated revenue impact of {revenue}."
        )

        return (
            f"Title: {title}\n"
            f"Product Area: {product_area}\n"
            f"Status: {status}\n"
            f"Accounts Requesting: {accounts}\n"
            f"Est. Revenue Impact: {revenue}\n\n"
            f"Summary: {clean_summary}"
        )

    def _fallback_answer(self, evidence: list[Evidence]) -> dict[str, Any]:
        top = evidence[:5]
        base_text = " ".join((e.snippet or "").splitlines()[0] for e in top[:3] if (e.snippet or "").strip())
        answer = self._normalize_to_structured_summary(
            "Based on the retrieved evidence, " + (base_text or "The retrieved evidence supports a recent customer request."),
            evidence,
        )
        return {
            "concise_answer": answer[:1200],
            "reasoning_summary": "I combined the highest-scoring customer graph, documentation, and release-note evidence and avoided claims that were not present in those sources.",
            "follow_up_questions": [],
        }

    def _normalize_grounded_result(self, payload: Any, evidence: list[Evidence]) -> dict[str, Any]:
        if isinstance(payload, dict):
            result = payload
        elif isinstance(payload, list):
            if payload and isinstance(payload[0], dict):
                result = payload[0]
            else:
                result = {"concise_answer": " ".join(str(item) for item in payload if item), "reasoning_summary": "The model returned list-shaped output, so the answer was normalized from the supplied evidence.", "follow_up_questions": []}
        else:
            text = str(payload or "").strip()
            if not text:
                return self._fallback_answer(evidence)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"concise_answer": text, "reasoning_summary": "The model output was text, so it was normalized to a single answer string.", "follow_up_questions": []}
            if isinstance(parsed, dict):
                result = parsed
            else:
                result = {"concise_answer": str(parsed), "reasoning_summary": "The model output was not a JSON object, so it was normalized to a plain-text answer.", "follow_up_questions": []}

        concise = result.get("concise_answer")
        if isinstance(concise, list):
            concise = " ".join(str(item) for item in concise if item)
        if not isinstance(concise, str) or not concise.strip():
            concise = self._fallback_answer(evidence)["concise_answer"]
        else:
            concise = self._normalize_to_structured_summary(concise, evidence)

        reasoning = result.get("reasoning_summary")
        if not isinstance(reasoning, str) or not reasoning.strip():
            reasoning = "The answer was grounded in the retrieved evidence and surfaced only claims supported by those sources."

        follow_up = result.get("follow_up_questions")
        if isinstance(follow_up, str):
            follow_up = [follow_up]
        elif not isinstance(follow_up, list):
            follow_up = []

        return {
            "concise_answer": concise,
            "reasoning_summary": reasoning,
            "follow_up_questions": [str(item) for item in follow_up if item],
        }

    async def grounded_answer(self, question: str, evidence: list[Evidence], contradictions: list[Contradiction]) -> dict[str, Any]:
        if not evidence:
            return {
                "concise_answer": "I do not have enough verified customer or product evidence to answer this confidently.",
                "reasoning_summary": "No customer, documentation, or release evidence was retrieved for this query, so the answer is explicitly withheld to maintain high accuracy.",
                "follow_up_questions": ["Can you narrow the question to a specific account, feature, issue, or product area?"],
            }
        if self.client:
            try:
                return self._normalize_grounded_result(await self._groq_grounded_answer(question, evidence, contradictions), evidence)
            except Exception as exc:
                logger.warning("Groq answer generation failed; using deterministic grounded fallback: %s", exc)
        return self._fallback_answer(evidence)

    # Retry Attempt
    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(2))
    async def _groq_grounded_answer(self, question: str, evidence: list[Evidence], contradictions: list[Contradiction]) -> dict[str, Any]:
        payload = {
            "question": question,
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "contradictions": [c.model_dump() for c in contradictions],
        }
        system = (
            "You are a professional sales enablement assistant for a FieldOps and customer success team. "
            "Answer using only the supplied evidence and never invent facts. "
            "Be concise, business-focused, and credible. "
            "If the evidence is weak or incomplete, say so explicitly instead of guessing. "
            "Return strict JSON with keys concise_answer, reasoning_summary, and follow_up_questions. "
            "Format concise_answer as a clean executive summary with a short headline and clear subsections, not as a long raw sentence dump. "
            "Use a structure like: 'Summary: ...\nKey facts:\n- Title: ...\n- Product Area: ...\n- Status: ...\n- Accounts: ...\n- Impact: ...\n- Evidence note: ...' "
            "Always prefer readable bullet points, short table-like rows, and focused customer-facing language. "
            "Do not include raw scraping text or irrelevant browser noise. "
            "Keep answers brief, confident, and actionable for sales or customer conversations."
        )
        response = await self.client.chat.completions.create(
            model=self.settings.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ],
            temperature=self.settings.rag_temperature,
            top_p=self.settings.rag_top_p,
            top_k=self.settings.rag_top_k,
            max_tokens=self.settings.rag_max_tokens,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"concise_answer": " ".join(str(item) for item in parsed if item), "reasoning_summary": "The model answered with a list; it was normalized to a valid text answer.", "follow_up_questions": []}
        return {"concise_answer": str(parsed), "reasoning_summary": "The model responded with plain text that was normalized into the required schema.", "follow_up_questions": []}
