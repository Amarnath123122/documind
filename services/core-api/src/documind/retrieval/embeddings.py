"""Embedding providers behind one interface.

Each model writes to its own vector column (by dimension) and records its name per chunk;
search only compares vectors from the active model. Never mix models in one ranking.
"""

import asyncio
from typing import Protocol

import httpx

from documind.common.config import get_settings
from documind.retrieval.domain import EMBEDDING_DIM, HASH_MODEL, hash_embed

COLUMN_BY_DIM = {256: "embedding", 768: "embedding_768"}


class Embedder(Protocol):
    name: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Offline, deterministic, lexical only. Default for CI."""

    name, dim = HASH_MODEL, EMBEDDING_DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(lambda: [hash_embed(t) for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return hash_embed(text)


class OllamaEmbedder:
    """nomic-embed-text: 768 dims. It expects task prefixes for documents vs queries."""

    dim = 768
    batch = 32

    def __init__(self, base_url: str, model: str):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.name = f"ollama:{model}"

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as c:
            for i in range(0, len(inputs), self.batch):
                r = await c.post(f"{self.base_url}/api/embed",
                                 json={"model": self.model, "input": inputs[i:i + self.batch]})
                r.raise_for_status()
                out += r.json()["embeddings"]
        if any(len(v) != self.dim for v in out):
            raise ValueError(f"{self.name} returned unexpected dimensions")
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"search_document: {t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([f"search_query: {text}"]))[0]


def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "ollama":
        return OllamaEmbedder(s.ollama_url, s.ollama_embed_model)
    return HashEmbedder()
