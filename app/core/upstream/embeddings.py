from typing import List

import httpx
import numpy as np

from pydantic import BaseModel

from app.core.config import SEARCH_SETTINGS as CFG
from app.core.exceptions import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError

SERVICE = "embeddings"


class EmbeddingItem(BaseModel):
    index: int
    embedding: List[float]


class EmbeddingResponse(BaseModel):
    """OpenAI /embeddings answer"""
    data: List[EmbeddingItem]


class EmbeddingClient:
    """OpenAI-compatible embeddings; vectors come back L2-normalized"""

    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int) -> None:
        self.model = model
        self._dimensions = dimensions
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=CFG.EMBEDDING_TIMEOUT,
        )

    @property
    def signature(self) -> str:
        """Model and size: vectors of another signature are not comparable."""
        return f"{self.model}:{self._dimensions}"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed(self, texts: List[str]) -> np.ndarray:
        """Matrix (len(texts), dimensions) of unit vectors."""
        payload = {"model": self.model, "input": texts, "dimensions": self._dimensions}
        try:
            response = await self._http.post("/embeddings", json=payload)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"No answer in {CFG.EMBEDDING_TIMEOUT}s", service=SERVICE) from e
        except httpx.TransportError as e:
            raise UpstreamUnavailableError(f"Unreachable: {e}", service=SERVICE) from e

        if not response.is_success:
            raise UpstreamError(f"HTTP {response.status_code}: {response.text[:200]}", service=SERVICE)

        items = sorted(EmbeddingResponse.model_validate_json(response.content).data, key=lambda item: item.index)
        if len(items) != len(texts):
            raise UpstreamError(f"{len(items)} vectors for {len(texts)} texts", service=SERVICE)
        vectors = np.asarray([item.embedding for item in items], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)
