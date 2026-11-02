import io
import random
import zipfile

import pytest

from documind.documents.content import DOCX, MD, PDF, TXT, bytes_match_declared, sniff
from documind.ingestion.domain import PermanentError, backoff_s
from documind.ingestion.extract import extract_pages
from documind.ingestion.scanner import EicarScanner
from helpers import EICAR, make_pdf


@pytest.mark.parametrize(("head", "family"), [
    (b"%PDF-1.7\n...", "pdf"),
    (b"PK\x03\x04rest", "zip"),
    (b"hello world", "text"),
    ("héllo".encode(), "text"),
    (b"MZ\x90\x00\x03", None),          # Windows executable
    (b"\xff\xfe\xfd binary", None),
])
def test_sniff(head, family):
    assert sniff(head) == family


def test_sniff_tolerates_utf8_cut_at_boundary():
    assert sniff("abc€".encode()[:-1]) == "text"


@pytest.mark.parametrize(("mime", "head", "ok"), [
    (PDF, b"%PDF-1.4", True),
    (PDF, b"plain text", False),        # spoofed declared type
    (DOCX, b"PK\x03\x04", True),
    (TXT, b"%PDF-1.4", False),
    (MD, b"# Title", True),
])
def test_bytes_match_declared(mime, head, ok):
    assert bytes_match_declared(mime, head) is ok


def test_backoff_is_bounded_and_grows():
    rng = random.Random(7)
    delays = [backoff_s(a, rng=rng) for a in range(1, 12)]
    assert all(0 < d <= 300 for d in delays)
    assert delays[0] <= 2 and delays[-1] >= 150


def test_extract_pdf_pages():
    assert [p.strip() for p in extract_pages(PDF, make_pdf(["A1", "B2"]), 10)] == ["A1", "B2"]


def test_extract_rejects_too_many_pages():
    with pytest.raises(PermanentError, match="too_many_pages"):
        extract_pages(PDF, make_pdf(["x"] * 3), 2)


def test_extract_rejects_invalid_utf8():
    with pytest.raises(PermanentError, match="invalid_utf8"):
        extract_pages(TXT, b"\xff\xfe", 10)


def test_extract_strips_nul():
    assert extract_pages(TXT, b"a\x00b", 10) == ["ab"]


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_docx_zip_bomb_rejected():
    bomb = _zip({"word/document.xml": b"0" * (5 * 1024 * 1024)})  # ~1000:1 ratio
    with pytest.raises(PermanentError, match="archive_too_large"):
        extract_pages(DOCX, bomb, 10)


def test_zip_that_is_not_docx_rejected():
    with pytest.raises(PermanentError, match="not_a_docx"):
        extract_pages(DOCX, _zip({"readme.txt": b"hi"}), 10)


async def test_eicar_scanner():
    s = EicarScanner()
    assert await s.is_infected(b"prefix " + EICAR + b" suffix")
    assert not await s.is_infected(b"clean text")
