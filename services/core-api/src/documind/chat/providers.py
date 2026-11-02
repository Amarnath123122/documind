"""LLM provider adapters behind one interface.

- MockChatProvider: deterministic, offline. Used in CI. Proves wiring, not answer quality.
- OllamaChatProvider: local models for learning (`LLM_PROVIDER=ollama`).
Azure OpenAI is added at deployment time behind the same Protocol.
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from documind.chat.domain import ANSWER_FORMAT, NOT_FOUND
from documind.common.config import get_settings
from documind.retrieval.domain import tokens


class ChatProvider(Protocol):
    name: str

    def stream_answer(self, *, system: str, question: str, context: str,
                      max_output_tokens: int) -> AsyncIterator[str]: ...


_BLOCK = re.compile(r"<<<EVIDENCE (E\d+) \|[^>]*>>>\n(.*?)\n<<<END>>>", re.DOTALL)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


class MockChatProvider:
    """Extractive: returns the evidence sentence with the most question-word overlap."""

    name = "mock"

    async def stream_answer(self, *, system: str, question: str, context: str,
                            max_output_tokens: int) -> AsyncIterator[str]:
        q = set(tokens(question))
        best, best_score = None, 0
        for label, body in _BLOCK.findall(context):
            for sent in _SENTENCE.split(body):
                score = len(q & set(tokens(sent)))
                if score > best_score:
                    best, best_score = (label, sent.strip()), score
        if best is None:
            answer = NOT_FOUND
        else:
            answer = f"According to the documents: {best[1]} [{best[0]}]"
        for word in answer.split(" "):
            yield word + " "


class OllamaChatProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, num_ctx: int = 4096, timeout_s: int = 90):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.num_ctx, self.timeout_s = num_ctx, timeout_s

    async def stream_answer(self, *, system: str, question: str, context: str,
                            max_output_tokens: int) -> AsyncIterator[str]:
        # num_ctx explicitly: Ollama's default per-request window can silently truncate the
        # *start* of the prompt (our instructions). keep_alive avoids reloading the model.
        body = {"model": self.model, "stream": True, "keep_alive": "30m",
                "options": {"num_predict": max_output_tokens, "temperature": 0,
                            "num_ctx": self.num_ctx},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": (f"{context}\n\nQuestion: {question}\n\n"
                                                          f"{ANSWER_FORMAT}")}]}
        timeout = httpx.Timeout(connect=5, read=self.timeout_s, write=10, pool=5)
        async with httpx.AsyncClient(timeout=timeout) as c, \
                c.stream("POST", f"{self.base_url}/api/chat", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    msg = json.loads(line)
                    if piece := msg.get("message", {}).get("content"):
                        yield piece


def get_chat_provider() -> ChatProvider:
    s = get_settings()
    if s.llm_provider == "ollama":
        return OllamaChatProvider(s.ollama_url, s.ollama_model, s.ollama_num_ctx, s.llm_timeout_s)
    return MockChatProvider()
