import hashlib
import logging
import math

import cohere
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = cohere.AsyncClient(settings.cohere_api_key) if settings.cohere_api_key else None

    async def embed(self, text: str, input_type: str = "search_document") -> list[float]:
        vectors = await self.embed_many([text], input_type=input_type)
        return vectors[0]

    async def embed_many(self, texts: list[str], input_type: str = "search_document") -> list[list[float]]:
        if not texts:
            return []
        if self.client:
            try:
                return await self._embed_cohere(texts, input_type)
            except Exception as exc:
                logger.warning("Cohere embeddings failed; falling back to deterministic local vectors: %s", exc)
        return [self._local_embedding(text) for text in texts]

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
    async def _embed_cohere(self, texts: list[str], input_type: str) -> list[list[float]]:
        response = await self.client.embed(
            texts=texts,
            model=self.settings.cohere_embed_model,
            input_type=input_type,
            embedding_types=["float"],
        )
        return [list(vec) for vec in response.embeddings.float]

    def _local_embedding(self, text: str) -> list[float]:
        dim = self.settings.embedding_dim
        vector = [0.0] * dim
        tokens = [token.strip(".,:;!?()[]{}\"'").lower() for token in text.split()]
        for token in tokens:
            if not token:
                continue
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dim
            sign = 1 if digest[4] % 2 == 0 else -1
            vector[idx] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]
