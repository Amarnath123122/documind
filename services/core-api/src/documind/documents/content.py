"""Content-type policy from *actual bytes*, never from the declared MIME or filename.

Pure domain: no framework imports.
"""

PDF = "application/pdf"
TXT = "text/plain"
MD = "text/markdown"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ALLOWED_MIME = frozenset({PDF, TXT, MD, DOCX})
HEAD_BYTES = 4096


def sniff(head: bytes) -> str | None:
    """Best-effort family from the first bytes: 'pdf', 'zip', 'text' or None."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if b"\x00" in head:
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte char may be cut at the boundary; only the tail may be incomplete.
        if e.start < len(head) - 3:
            return None
    return "text"


_FAMILY = {PDF: "pdf", DOCX: "zip", TXT: "text", MD: "text"}


def bytes_match_declared(declared_mime: str, head: bytes) -> bool:
    return _FAMILY.get(declared_mime) == sniff(head)
