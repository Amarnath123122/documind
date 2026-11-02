"""Report drafting for compare-documents workflows. Pure domain.

Extractive and cited: every bullet is a sentence from an authorized, redacted chunk with
its page. A model-written summary can replace `draft_report` behind the same contract;
the human approval gate stays either way.
"""

import re
from dataclasses import dataclass
from uuid import UUID

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
MAX_POINTS_PER_DOC = 3


@dataclass(frozen=True)
class SourceChunk:
    document_id: UUID
    document_version: int
    filename: str
    page_no: int
    text: str


def key_points(chunks: list[SourceChunk],
               limit: int = MAX_POINTS_PER_DOC) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for c in chunks:
        for sentence in _SENTENCE.split(c.text):
            sentence = sentence.strip()
            if len(sentence) >= 12 and sentence.lower() not in seen:
                seen.add(sentence.lower())
                out.append((sentence, c.page_no))
                if len(out) == limit:
                    return out
    return out


def draft_report(title: str, by_doc: dict[UUID, list[SourceChunk]]) -> str:
    lines = [f"# {title}", "", ("_Draft generated from authorized, redacted sources. "
             "Requires approval before export._"), ""]
    for chunks in by_doc.values():
        if not chunks:
            continue
        lines += [f"## {chunks[0].filename}", ""]
        points = key_points(chunks)
        lines += [f"- {s} (p. {page})" for s, page in points] or ["- (no extractable text)"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
