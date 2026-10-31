"""
Hybrid search over the knowledge base.

FTS5 has no Russian stemmer, so a word is matched by its prefix with the ending cut:
"встречи" finds "встреча" and "встречу", "дача" finds "даче". Embeddings catch what shares no words with the
query ("созвон" - "звонок"). Both rankings are fused by reciprocal rank: no score
calibration between BM25 and cosine is needed.
"""

import re

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from loguru import logger

from app.core.config import SEARCH_SETTINGS as CFG
from app.core.entities import Event, ItemKind, Note, SearchHit
from app.core.exceptions import UpstreamError
from app.core.knowledge.repository import ItemKey, KnowledgeRepository
from app.core.upstream import EmbeddingClient

WORD = re.compile(r"\w+", re.UNICODE)


def build_match_query(query: str) -> Optional[str]:
    """FTS5 MATCH expression: any of the words, all but short ones by stem prefix; None for no words."""
    terms = []
    for word in WORD.findall(query.lower()):
        if len(word) < CFG.SHORT_WORD:
            terms.append(f'"{word}"')
        else:
            cut = 2 if len(word) >= CFG.LONG_WORD else 1
            terms.append(f'"{word[:-cut]}"*')
    return " OR ".join(dict.fromkeys(terms)) or None


def fuse_rankings(rankings: Sequence[Sequence[ItemKey]], k: int = CFG.RRF_K) -> List[Tuple[ItemKey, float]]:
    """Reciprocal rank fusion, best first."""
    scores: Dict[ItemKey, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


class KnowledgeSearch:
    """Searches notes and mirrored events; keeps their embeddings up to date"""

    def __init__(self, repository: KnowledgeRepository, embedder: Optional[EmbeddingClient]) -> None:
        self._repository = repository
        self._embedder = embedder

    async def search(self, query: str, kinds: Sequence[ItemKind], limit: int) -> List[SearchHit]:
        rankings: List[List[ItemKey]] = []

        # 1. Keyword ranking: names, numbers, exact words
        if match := build_match_query(query):
            rankings.append(await self._repository.full_text(match, kinds, CFG.CANDIDATES))

        # 2. Semantic ranking; search still works on keywords when the API fails
        if self._embedder is not None:
            try:
                rankings.append(await self._semantic(query, kinds))
            except UpstreamError as e:
                logger.warning("Semantic search skipped: {}", e)

        fused = fuse_rankings(rankings)[:limit]
        items = await self._repository.items([key for key, _ in fused])
        return [self._hit(items[key], score) for key, score in fused if key in items]

    async def index_pending(self) -> int:
        """Embeds the items left without a vector; returns how many got one."""
        if self._embedder is None:
            return 0

        indexed = 0
        while pending := await self._repository.pending_embeddings(self._embedder.signature, CFG.EMBEDDING_BATCH):
            keys = [key for key, _ in pending]
            vectors = await self._embedder.embed([text for _, text in pending])
            await self._repository.store_vectors(keys, vectors, self._embedder.signature)
            indexed += len(keys)

        if indexed:
            logger.info("Embeddings updated: {} items", indexed)
        return indexed

    async def _semantic(self, query: str, kinds: Sequence[ItemKind]) -> List[ItemKey]:
        keys, matrix = await self._repository.vectors(kinds, self._embedder.signature)
        if not keys:
            return []
        query_vector = (await self._embedder.embed([query]))[0]
        # Vectors are unit length: the dot product is the cosine
        scores = matrix @ query_vector
        order = np.argsort(-scores)[:CFG.CANDIDATES]
        return [keys[i] for i in order]

    @staticmethod
    def _hit(item: object, score: float) -> SearchHit:
        if isinstance(item, Note):
            return SearchHit(
                kind=ItemKind.NOTE,
                ref=str(item.id),
                title=item.title,
                snippet=_snippet(item.content),
                when=item.updated_at,
                score=round(score, 4),
            )
        if isinstance(item, Event):
            return SearchHit(
                kind=ItemKind.EVENT,
                ref=item.id,
                title=item.title,
                snippet=_snippet("\n".join(part for part in (item.description, item.location) if part)),
                when=item.start,
                score=round(score, 4),
            )
        raise TypeError(f"Unexpected knowledge base item {type(item).__name__}")


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= CFG.SNIPPET_CHARS else text[:CFG.SNIPPET_CHARS - 1] + "…"
