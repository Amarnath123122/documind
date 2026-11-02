# ADR-0010: Golden-set evaluation gate; report workflows with four-eyes approval

**Status:** Accepted (Phase 6)

## Evaluation
- `tools/evals/golden.json` holds a hand-written synthetic corpus (4 policies, 12 pages), **16 positive questions**, each with exactly one correct document, page and answer phrase (one deliberately paraphrased), and **4 negatives** that nothing in the corpus answers.
- `tests/integration/test_eval_golden.py` runs the **real** pipeline in a fresh workspace (upload → worker → hybrid search → SSE chat) and measures hit@1, hit@6, MRR, citation correctness, answer-contains and negative-abstain. It runs in CI with the rest of the suite. `dev.ps1 eval` also writes `docs/benchmarks/golden-eval-latest.md`.
- **Gates equal the measured baseline** (hit@1 0.875, hit@6 1.0, MRR 0.922, citations 0.938, answers 0.875, abstain 1.0). Raise them when quality improves; never lower them to make CI pass.
- **Determinism was a real bug.** Rank fusion and both SQL rankings broke ties on random chunk UUIDs, so identical corpora ranked differently between runs (hit@1 varied from 0.81 to 0.88). Ties now go to the keyword list, then to content position (filename, page, ordinal). Four consecutive runs gave identical metrics.
- **What these numbers mean:** they measure *this system* with the offline hash embedder and the extractive mock. The known misses are honest model limits: paraphrase (lexical embeddings) and sentence choice (mock). Switching to a real embedding model or LLM means re-running this set and recording a new baseline, compared side by side.

## Report workflows (human approval)
- `POST /v1/workflows` (`workflow:run`: editors and admins) builds a **cited, extractive draft** from up to 10 documents, using only what the author can see. RLS hides restricted documents, which appear as `404 DOCUMENT_NOT_FOUND`. The draft is built from redacted chunks only, and records the source document versions.
- **Approval:** `workflow:approve` (tenant admin), the approver must not be the author (**four-eyes**), and the status must still be pending. The four-eyes rule is enforced twice: in the use case (`403 FOUR_EYES`) **and** as a database `CHECK (decided_by <> created_by)`, which a test confirms holds even when the code is bypassed.
- **Sources are re-checked live** at approval and again at export. If any source was deleted, superseded or made invisible, the result is `409 SOURCES_CHANGED`. Deleted content can't leave the system through an old report.
- Export (`text/markdown`, as an attachment) exists only for approved runs. Every create, approve, reject and export is audited in the same transaction.
- Runs are visible only to their author and tenant admins (RLS).

**Future work:** model-written summaries behind `draft_report` (the approval gate stays); outward actions beyond download (email or ticket) through an allow-listed tool registry that requires an approved run id.
