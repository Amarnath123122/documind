# Golden eval: 2026-11-02

Setup: `embeddings=ollama:nomic-embed-text llm=mock`. 16 positive, 4 negative questions.

| Metric | Value | Gate |
|---|---|---|
| hit@1 | 0.938 | 0.87 |
| hit@6 | 1.000 | 1.0 |
| mrr | 0.969 | 0.91 |
| citation_correct | 0.938 | 0.93 |
| answer_contains | 0.875 | 0.87 |
| negative_abstain | 1.000 | 1.0 |

## Misses

- `How long is paternity leave?` rank=2 cited=True answer=True
- `What is the minimum password length?` rank=1 cited=True answer=False
- `How do I paraphrase: what is the yearly allowance for training and certification?` rank=1 cited=False answer=False
