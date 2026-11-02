"""Synthetic fixtures and test-only helpers. No real customer data, ever."""

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.common.config import get_settings

PDF = "application/pdf"
TXT = "text/plain"

# Assembled at runtime so this file is not flagged by antivirus.
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode()


def make_pdf(pages: list[str]) -> bytes:
    """Minimal valid PDF with one line of Helvetica text per page."""
    n = len(pages)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(n))}] "
        f"/Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, t in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


async def admin_sql(sql: str, params: dict | None = None) -> list:
    """Superuser access for test setup/inspection only (bypasses RLS)."""
    engine = create_async_engine(get_settings().seed_database_url)
    try:
        async with engine.begin() as c:
            await c.execute(text("SET search_path = documind"))
            res = await c.execute(text(sql), params or {})
            return list(res) if res.returns_rows else []
    finally:
        await engine.dispose()


async def start_upload(client: httpx.AsyncClient, ws, name: str, data: bytes, mime: str,
                       *, declared_size: int | None = None, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    r = await client.post(f"/v1/workspaces/{ws}/uploads", headers=headers, json={
        "filename": name, "size_bytes": declared_size or len(data), "mime": mime})
    assert r.status_code == 201, r.text
    return r.json()


async def put_blob(ticket: dict, data: bytes) -> httpx.Response:
    async with httpx.AsyncClient() as raw:
        return await raw.put(ticket["upload_url"], content=data,
                             headers=ticket["required_headers"])


async def upload(client, ws, name: str, data: bytes, mime: str, **kw) -> tuple[dict, httpx.Response]:
    """Full browser flow: create session -> PUT bytes to blob -> complete."""
    ticket = await start_upload(client, ws, name, data, mime, **kw)
    put = await put_blob(ticket, data)
    assert put.status_code == 201, put.text
    done = await client.post(f"/v1/uploads/{ticket['upload_id']}/complete")
    return ticket, done
