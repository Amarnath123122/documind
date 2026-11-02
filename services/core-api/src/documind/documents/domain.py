"""Document lifecycle. Pure domain: no framework imports."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from uuid import UUID


class DocumentStatus(StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    QUARANTINED = "quarantined"
    SCANNING = "scanning"
    EXTRACTING = "extracting"
    REDACTING = "redacting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"
    NEEDS_ATTENTION = "needs_attention"


S = DocumentStatus
_PIPELINE = [S.CREATED, S.UPLOADING, S.QUARANTINED, S.SCANNING, S.EXTRACTING,
             S.REDACTING, S.CHUNKING, S.EMBEDDING, S.INDEXING, S.READY]

ALLOWED: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    s: frozenset({nxt, S.FAILED, S.DELETING}) for s, nxt in pairwise(_PIPELINE)
}
ALLOWED[S.READY] = frozenset({S.DELETING})
ALLOWED[S.FAILED] = frozenset({S.DELETING})
ALLOWED[S.DELETING] = frozenset({S.DELETED, S.NEEDS_ATTENTION})
ALLOWED[S.NEEDS_ATTENTION] = frozenset({S.DELETING})
ALLOWED[S.DELETED] = frozenset()

# Only this state may be opened, cited or retrieved (invariant 5).
READABLE_CONTENT = frozenset({S.READY})


class InvalidTransition(ValueError):
    pass


def transition(current: DocumentStatus, target: DocumentStatus) -> DocumentStatus:
    """A retried worker cannot move `deleting` back to `ready`: no such edge exists."""
    if target not in ALLOWED[current]:
        raise InvalidTransition(f"{current} -> {target} is not allowed")
    return target


@dataclass(frozen=True)
class Document:
    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    filename: str
    status: DocumentStatus
    version: int
    created_by: UUID
    created_at: datetime
    mime: str | None = None
    size_bytes: int | None = None
    page_count: int | None = None
    failure_reason: str | None = None
    visibility: str = "workspace"
