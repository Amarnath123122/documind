"""Safe text extraction from already-scanned bytes. Returns one string per page.

Every parse failure is a PermanentError: retrying the same bytes cannot succeed.
"""

import io
import zipfile

from documind.documents.content import DOCX, MD, PDF, TXT
from documind.ingestion.domain import PermanentError

MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024
MAX_ZIP_RATIO = 100


def extract_pages(mime: str, data: bytes, max_pages: int) -> list[str]:
    if mime == PDF:
        pages = _pdf(data, max_pages)
    elif mime == DOCX:
        pages = _docx(data)
    elif mime in (TXT, MD):
        pages = _text(data)
    else:
        raise PermanentError("unsupported_type")
    if len(pages) > max_pages:
        raise PermanentError("too_many_pages")
    # Postgres text cannot hold NUL; parsers occasionally emit it.
    return [p.replace("\x00", "") for p in pages] or [""]


def _pdf(data: bytes, max_pages: int) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PermanentError("encrypted_pdf")
        if len(reader.pages) > max_pages:
            raise PermanentError("too_many_pages")
        return [page.extract_text() or "" for page in reader.pages]
    except PermanentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise PermanentError("unparseable_pdf") from e


def _docx(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = sum(i.file_size for i in z.infolist())
            if total > MAX_ZIP_UNCOMPRESSED or total > MAX_ZIP_RATIO * max(len(data), 1):
                raise PermanentError("archive_too_large")  # zip-bomb guard
            if "word/document.xml" not in z.namelist():
                raise PermanentError("not_a_docx")
        import docx

        doc = docx.Document(io.BytesIO(data))
    except PermanentError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise PermanentError("unparseable_docx") from e
    # DOCX has no fixed pagination; the whole body is "page 1" (citations use offsets later).
    return ["\n".join(p.text for p in doc.paragraphs)]


def _text(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PermanentError("invalid_utf8") from e
    return text.split("\f")
