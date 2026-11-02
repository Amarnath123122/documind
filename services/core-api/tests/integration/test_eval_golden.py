"""Golden-set evaluation: retrieval and answer quality as a regression gate.

Runs the real pipeline (upload -> worker -> hybrid search -> chat) on a synthetic corpus in a
fresh workspace. With the deterministic hash embedder + mock LLM these numbers measure the
*system*, not a production model: thresholds are the measured baseline, so a drop means a
regression. Set EVAL_REPORT=path to write a Markdown report.
"""

import json
import os
import pathlib
import re
import uuid
from datetime import date

import pytest

from documind.chat.providers import get_chat_provider
from documind.common.config import get_settings
from documind.dev_seed import ALPHA
from documind.ingestion.worker import process_one
from documind.retrieval.embeddings import get_embedder
from helpers import TXT, admin_sql, upload

pytestmark = pytest.mark.db
GOLDEN = json.loads((pathlib.Path(__file__).parents[4] / "tools" / "evals" / "golden.json")
                    .read_text(encoding="utf-8"))
K = 6

# Measured baseline (2026-11-02). Raise when quality improves; never lower to make CI green.
GATES = {"hit@1": 0.87, "hit@6": 1.0, "mrr": 0.91, "citation_correct": 0.93,
         "answer_contains": 0.87, "negative_abstain": 1.0}


async def drain():
    while await process_one():
        pass


def _norm(text: str) -> str:
    """Case- and thousands-separator-insensitive: "40,000" == "40000"."""
    return re.sub(r"(?<=\d),(?=\d{3})", "", text.lower())


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((fields["event"], json.loads(fields["data"])))
    return out


@pytest.fixture(scope="module")
async def eval_ws():
    from conftest import login

    alice = await login("alice@alpha.test")
    ws = uuid.uuid4()
    await admin_sql("INSERT INTO workspaces (id, tenant_id, name) VALUES (:w, :t, :n)",
                    {"w": ws, "t": ALPHA, "n": f"Eval {ws.hex[:6]}"})
    ids = {}
    for key, d in GOLDEN["documents"].items():
        ticket, done = await upload(alice, ws, d["filename"], "\f".join(d["pages"]).encode(), TXT)
        assert done.status_code == 202, done.text
        ids[ticket["document_id"]] = key
    await drain()
    yield alice, ws, ids
    await alice.aclose()


async def test_golden_set_quality(eval_ws):
    alice, ws, ids = eval_ws
    rows, ranks = [], []
    for item in GOLDEN["questions"]:
        hits = (await alice.get(f"/v1/workspaces/{ws}/search", params={"q": item["q"]})).json()
        ranked = [(ids.get(h["document_id"]), h["page_no"]) for h in hits[:K]]
        rank = next((i for i, x in enumerate(ranked, 1) if x == (item["doc"], item["page"])), None)
        ranks.append(rank)

        turn = (await alice.post("/v1/chat/turns", json={"workspace_id": str(ws),
                                                         "question": item["q"]})).json()
        events = parse_sse((await alice.get(turn["stream_url"])).text)
        done = next((d for e, d in events if e == "completed"),
                    {"answer": "", "grounding": "failed"})  # failed turn = miss, not crash
        cites = [(ids.get(d["document_id"]), d["page_start"]) for e, d in events if e == "citation"]
        rows.append({"q": item["q"], "rank": rank,
                     "cited_ok": (item["doc"], item["page"]) in cites,
                     "answer_ok": _norm(item["answer"]) in _norm(done["answer"])})

    abstained = []
    for item in GOLDEN["negatives"]:
        turn = (await alice.post("/v1/chat/turns", json={"workspace_id": str(ws),
                                                         "question": item["q"]})).json()
        done = next((d for e, d in parse_sse((await alice.get(turn["stream_url"])).text)
                     if e == "completed"), {"grounding": "failed"})
        abstained.append(done["grounding"] == "insufficient_evidence")

    n = len(rows)
    metrics = {
        "hit@1": sum(r == 1 for r in ranks) / n,
        f"hit@{K}": sum(r is not None for r in ranks) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
        "citation_correct": sum(r["cited_ok"] for r in rows) / n,
        "answer_contains": sum(r["answer_ok"] for r in rows) / n,
        "negative_abstain": sum(abstained) / len(abstained),
    }
    cfg = get_settings()
    setup = f"embeddings={get_embedder().name} llm={get_chat_provider().name}"
    if cfg.llm_provider == "ollama":
        setup += f":{cfg.ollama_model}"
    print("\nGOLDEN EVAL", setup, json.dumps({k: round(v, 3) for k, v in metrics.items()}))
    for r in rows:
        if not (r["rank"] == 1 and r["cited_ok"] and r["answer_ok"]):
            print("  miss:", r)

    if path := os.environ.get("EVAL_REPORT"):
        lines = [f"# Golden eval: {date.today()}", "",
                 f"Setup: `{setup}`. {n} positive, {len(abstained)} negative questions.", "",
                 "| Metric | Value | Gate |", "|---|---|---|"]
        lines += [f"| {k} | {v:.3f} | {GATES.get(k, '-')} |" for k, v in metrics.items()]
        lines += ["", "## Misses", ""] + [
            f"- `{r['q']}` rank={r['rank']} cited={r['cited_ok']} answer={r['answer_ok']}"
            for r in rows if not (r["rank"] == 1 and r["cited_ok"] and r["answer_ok"])]
        pathlib.Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Gates guard the deterministic CI setup. Real-model runs are report-only comparisons.
    if (cfg.embedding_provider, cfg.llm_provider) != ("hash", "mock"):
        return
    for name, floor in GATES.items():
        assert metrics[name] >= floor, f"{name} regressed: {metrics[name]:.3f} < {floor}"
