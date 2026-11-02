# ADR-0011: Real local models (Ollama): measured, not assumed

**Status:** Accepted (Phase 7)

## Decision
- **Embeddings sit behind an `Embedder` port.** `hash-v1-256` stays the CI default (deterministic, offline). `nomic-embed-text` (768 dimensions, via Ollama) is used with `EMBEDDING_PROVIDER=ollama`.
- **Each dimension has its own column** (`embedding`, `embedding_768`), and the model name is recorded per chunk. Search filters on `embedding_model = <active model>`, so vectors from different models are **never** ranked together. Switching models therefore means re-embedding documents.
- **Model calls happen outside database transactions,** so no connection is held during inference.
- **Chat:** `LLM_PROVIDER=ollama` and `OLLAMA_MODEL=qwen2.5:1.5b`. The prompt ends with an explicit answer-format instruction, because small models follow end-of-prompt instructions best.
- **Citations:** loose forms (`Evidence E1`, `(E1)`, bare `E1`) are normalized to `[E1]`, then **validated against the labels the server issued for that turn**. Invented labels are still dropped.
- **Refusals use an explicit `NOT_FOUND` token.** The server never infers "supported" from a refusal that happens to cite something; it replaces it with the standard "couldn't find this" answer and no citations.
- **Local:** `dev.ps1 ai` starts Ollama, pulls both models, and runs the golden set against them. It is report-only; the CI gates stay on the deterministic setup.

## Results on the golden set (16 positive / 4 negative questions, 2026-11-02, CPU only)

| Setup | hit@1 | MRR | citation correct | answer contains | correctly refuses |
|---|---|---|---|---|---|
| hash + mock (CI baseline) | 0.875 | 0.922 | 0.938 | 0.875 | 1.0 |
| **nomic** + mock | **0.938** | **0.969** | 0.938 | 0.875 | 1.0 |
| nomic + qwen2.5:1.5b, original prompt | 0.938 | 0.969 | 0.375 | 0.750 | 1.0 |
| … + format instruction + loose citations | 0.938 | 0.969 | 0.812 | 0.938 | **0.0** ✗ |
| … + `NOT_FOUND` rule placed first | 0.938 | 0.969 | 0.125 | 0.438 | 1.0 |
| **… + answer rule first, `NOT_FOUND` last (adopted)** | 0.938 | 0.969 | **0.688** | **0.812** | **1.0** |

## What we learned
1. **Real embeddings fix paraphrase.** The reworded question moved from rank 3 to rank 1. Retrieval improves on every metric.
2. **The mock flattered generation.** An extractive mock copies the evidence sentence and its label. A real 1.5B model paraphrases, cites inconsistently, and sometimes formats numbers differently (`40,000` versus `40000`; the eval now normalizes thousands separators).
3. **Prompt tweaks trade one failure for another.** One prompt made the model cite its refusals ("not in the documents [E1]"), which would have shown **false grounding** to users. Another made it refuse almost everything. Only the eval revealed both. Without it, either would have shipped.
4. A generation takes about 12 s per answer on this laptop's CPU (about 200 s for 20 questions). Plan for a GPU or a hosted model in production.

## Next levers (ranked)
A larger model (`qwen2.5:7b`, `llama3.1:8b`, or Azure OpenAI), compared on this same table. Constrained/JSON output (`{"answer", "citations": ["E1"]}`) instead of inline markers. More golden questions, especially near-miss negatives, before trusting any single-digit percentage change.
