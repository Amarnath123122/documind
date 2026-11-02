"""Chunking, deterministic embeddings and rank fusion. Pure domain."""

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

EMBEDDING_DIM = 256
HASH_MODEL = "hash-v1-256"  # part of the index version: never mix models in one search
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(["a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were", "be", "by", "with", "at", "as", "it", "this", "that", "from", "per", "how", "what", "which", "who", "many", "much", "do", "does", "can", "i", "you"])


@dataclass(frozen=True)
class Chunk:
    page_no: int
    ordinal: int
    char_start: int  # offsets into the *redacted* page text
    char_end: int
    text: str


def chunk_pages(pages: Sequence[str], size: int = 800, overlap: int = 120) -> list[Chunk]:
    """Page-aware: a chunk never spans pages, so every citation has one exact page."""
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    chunks, ordinal = [], 0
    for page_no, page in enumerate(pages, start=1):
        start = 0
        while start < len(page):
            end = min(start + size, len(page))
            if end < len(page):  # back off to a word boundary
                space = page.rfind(" ", start + size // 2, end)
                end = space if space > start else end
            piece = page[start:end]
            if piece.strip():
                chunks.append(Chunk(page_no, ordinal, start, end, piece))
                ordinal += 1
            if end >= len(page):
                break
            start = max(end - overlap, start + 1)
            nxt = page.find(" ", start, end)  # don't start mid-word
            start = nxt + 1 if nxt != -1 else start
    return chunks


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def hash_embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Feature-hashed unigrams + bigrams, L2-normalised. Deterministic, offline, free.

    Captures lexical overlap only. It proves integration correctness, NOT semantic quality;
    real embeddings are evaluated separately (ADR-0006).
    """
    toks = tokens(text)
    vec = [0.0] * dim
    for feat in [*toks, *(f"{a}_{b}" for a, b in pairwise(toks))]:
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def to_pgvector(vec: Iterable[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def rrf(*rankings: Sequence[str], k: int = 60) -> list[str]:
    """Reciprocal-rank fusion: deterministic merge of keyword and vector rankings.

    Ties (common: #1 in one list vs #1 in the other) go to the earlier ranking list,
    i.e. pass keyword results first. Never break ties on ids: ids are random, so
    results would change between otherwise identical runs.
    """
    score: dict[str, float] = {}
    positions: dict[str, list[int]] = {}
    for n, ranking in enumerate(rankings):
        for rank, item in enumerate(ranking, start=1):
            score[item] = score.get(item, 0.0) + 1.0 / (k + rank)
            positions.setdefault(item, [10**9] * len(rankings))[n] = rank
    return sorted(score, key=lambda i: (-score[i], positions[i], i))
