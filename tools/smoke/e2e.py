"""End-to-end smoke test against the RUNNING app, through the web origin (browser path).

Usage: services/core-api/.venv/Scripts/python tools/smoke/e2e.py [base_url]
Creates a few small documents in Alpha HR, then deletes what it created.
"""

import asyncio
import json
import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5173"
WS = "a0000000-0000-4000-8000-0000000000a1"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")


async def login(email: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(base_url=BASE, timeout=300)
    r = await c.post("/v1/dev/login", json={"email": email})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = c.cookies["dm_csrf"]
    return c


async def upload(c: httpx.AsyncClient, name: str, body: bytes) -> str:
    t = (await c.post(f"/v1/workspaces/{WS}/uploads", json={
        "filename": name, "size_bytes": len(body), "mime": "text/plain"})).json()
    async with httpx.AsyncClient(base_url=BASE) as raw:
        assert (await raw.put(t["upload_url"], content=body,
                              headers=t["required_headers"])).status_code == 201
    assert (await c.post(f"/v1/uploads/{t['upload_id']}/complete")).status_code == 202
    for _ in range(120):
        d = (await c.get(f"/v1/documents/{t['document_id']}")).json()
        if d["status"] in ("ready", "failed"):
            break
        await asyncio.sleep(1)
    return t["document_id"] if d["status"] == "ready" else ""


async def ask(c: httpx.AsyncClient, question: str) -> dict:
    t = (await c.post("/v1/chat/turns", json={"workspace_id": WS, "question": question})).json()
    events, cur = [], None
    async with c.stream("GET", t["stream_url"]) as r:
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                cur = line[7:]
            elif line.startswith("data: "):
                events.append((cur, json.loads(line[6:])))
    done = next((d for e, d in events if e == "completed"), {})
    return {"answer": done.get("answer", ""), "grounding": done.get("grounding"),
            "cites": [d for e, d in events if e == "citation"], "kinds": [e for e, _ in events]}


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE) as anon:
        check("web page loads", (await anon.get("/")).status_code == 200)
        check("unauthenticated API is 401", (await anon.get("/v1/workspaces")).status_code == 401)
    alice, dave = await login("alice@alpha.test"), await login("dave@alpha.test")
    carol, bob = await login("carol@alpha.test"), await login("bob@beta.test")
    tag = uuid.uuid4().hex[:6]
    made: list[str] = []
    try:
        doc = await upload(alice, f"smoke-{tag}.txt",
                           f"Smoke policy {tag}. Night shift allowance is 450 rupees per night. "
                           "Contact ops@alpha.test for approvals.".encode())
        made.append(doc)
        check("upload -> scan -> index reaches ready", bool(doc))

        hits = (await alice.get(f"/v1/workspaces/{WS}/search",
                                params={"q": "night shift allowance"})).json()
        check("search finds the new document", any(h["document_id"] == doc for h in hits))
        check("search snippet is redacted",
              all("ops@alpha.test" not in h["snippet"] for h in hits))

        a = await ask(alice, "What is the night shift allowance?")
        check("chat streams and completes", a["kinds"][:1] == ["started"] and "completed" in a["kinds"],
              f"grounding={a['grounding']}")
        check("chat answer mentions 450", "450" in a["answer"], a["answer"][:90])
        check("chat cites the new document",
              any(ci["document_id"] == doc for ci in a["cites"]))
        n = await ask(alice, "What is the company's stock option vesting schedule?")
        check("unanswerable question is not shown as supported",
              n["grounding"] != "supported", n["grounding"] or "")

        check("Bob (other company) cannot read it",
              (await bob.get(f"/v1/documents/{doc}")).status_code == 404)
        check("Carol (viewer) cannot upload", (await carol.post(
            f"/v1/workspaces/{WS}/uploads",
            json={"filename": "x.txt", "size_bytes": 1, "mime": "text/plain"})).status_code == 403)

        await alice.patch(f"/v1/documents/{doc}", json={"visibility": "restricted"})
        check("restricted doc hidden from Carol",
              (await carol.get(f"/v1/documents/{doc}")).status_code == 404)
        await alice.post(f"/v1/documents/{doc}/grants", json={"email": "carol@alpha.test"})
        check("grant makes it visible to Carol",
              (await carol.get(f"/v1/documents/{doc}")).status_code == 200)
        await alice.patch(f"/v1/documents/{doc}", json={"visibility": "workspace"})

        run = (await dave.post("/v1/workflows", json={
            "workspace_id": WS, "title": f"Smoke report {tag}", "document_ids": [doc]})).json()
        check("Dave drafts a cited report", run.get("status") == "pending_approval"
              and "450" in run.get("draft", ""))
        check("Dave cannot approve his own report", (await dave.post(
            f"/v1/workflows/{run['id']}/approve", json={})).status_code == 403)
        check("Alice approves", (await alice.post(
            f"/v1/workflows/{run['id']}/approve", json={})).status_code == 200)
        exp = await dave.get(f"/v1/workflows/{run['id']}/export")
        check("approved report exports", exp.status_code == 200 and "450" in exp.text)

        usage = await alice.get("/v1/tenants/a0000000-0000-4000-8000-000000000001/usage")
        check("admin sees usage", usage.status_code == 200
              and usage.json()["chat_tokens"]["used"] > 0)
    finally:
        for d in made:
            if d:
                await alice.delete(f"/v1/documents/{d}")
        if made and made[0]:
            for _ in range(60):
                if (await alice.get(f"/v1/documents/{made[0]}")).status_code == 404:
                    break
                await asyncio.sleep(1)
            check("delete + cleanup removes it",
                  (await alice.get(f"/v1/documents/{made[0]}")).status_code == 404)
        for c in (alice, dave, carol, bob):
            await c.aclose()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
