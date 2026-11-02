# Golden eval: 2026-11-02

Setup: `embeddings=ollama:nomic-embed-text llm=ollama:qwen2.5:1.5b`. 16 positive, 4 negative questions.

| Metric | Value | Gate |
|---|---|---|
| hit@1 | 0.938 | 0.87 |
| hit@6 | 1.000 | 1.0 |
| mrr | 0.969 | 0.91 |
| citation_correct | 0.688 | 0.93 |
| answer_contains | 0.812 | 0.87 |
| negative_abstain | 1.000 | 1.0 |

## Misses

- `How many casual leave days do employees get per year?` rank=1 cited=False answer=False
- `How many days of paid sick leave are there?` rank=1 cited=False answer=False
- `How long is paternity leave?` rank=2 cited=True answer=True
- `What is the minimum password length?` rank=1 cited=True answer=False
- `Is multi-factor authentication required for VPN?` rank=1 cited=False answer=True
- `Within how many days must expense claims be submitted?` rank=1 cited=False answer=True
- `How do I paraphrase: what is the yearly allowance for training and certification?` rank=1 cited=False answer=True
