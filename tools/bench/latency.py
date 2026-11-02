"""Measure API latency against the local stack (not a capacity test).

Usage: services/core-api/.venv/Scripts/python tools/bench/latency.py [requests] [concurrency]
Writes nothing; paste results into docs/benchmarks with the date and machine.
"""

import asyncio
import statistics
import sys
import time

import httpx

BASE = "http://localhost:8000"
WS = "a0000000-0000-4000-8000-0000000000a1"


async def client() -> httpx.AsyncClient:
    c = httpx.AsyncClient(base_url=BASE, timeout=30)
    r = await c.post("/v1/dev/login", json={"email": "alice@alpha.test"})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = c.cookies["dm_csrf"]
    return c


async def timed(fn) -> float:
    t = time.perf_counter()
    await fn()
    return (time.perf_counter() - t) * 1000


def pct(xs: list[float], p: float) -> float:
    return statistics.quantiles(xs, n=100)[int(p) - 1] if len(xs) > 1 else xs[0]


async def run(name: str, make, n: int, conc: int) -> None:
    sem = asyncio.Semaphore(conc)

    async def one():
        async with sem:
            return await timed(make)
    xs = await asyncio.gather(*[one() for _ in range(n)])
    print(f"{name:<28} n={n:<4} c={conc:<3} p50={pct(xs, 50):7.1f}ms "
          f"p95={pct(xs, 95):7.1f}ms max={max(xs):7.1f}ms")


async def main(n: int, conc: int) -> None:
    c = await client()

    async def list_docs():
        (await c.get(f"/v1/workspaces/{WS}/documents?limit=25")).raise_for_status()

    async def search():
        (await c.get(f"/v1/workspaces/{WS}/search", params={"q": "casual leave"})).raise_for_status()

    async def chat():
        r = await c.post("/v1/chat/turns", json={"workspace_id": WS, "question": "casual leave"})
        r.raise_for_status()
        body = (await c.get(r.json()["stream_url"])).text
        assert "event: completed" in body, body[-200:]

    await run("GET documents (list)", list_docs, n, conc)
    await run("GET search (hybrid)", search, n, conc)
    await run("chat turn (mock LLM, SSE)", chat, max(n // 4, 5), max(conc // 2, 1))
    await c.aclose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 200,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 10))
