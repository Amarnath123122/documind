"""Ingestion rules: error classification and retry policy. Pure domain."""

import random


class PermanentError(Exception):
    """Do not retry: bad content, policy rejection, malware. `reason` is user-visible."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Abandoned(Exception):
    """The document moved on (deleted, newer version, someone else finished). Stop quietly."""


def backoff_s(attempt: int, *, base: float = 2.0, cap: float = 300.0,
              rng: random.Random | None = None) -> float:
    """Exponential backoff with 'equal jitter': [d/2, d], d = min(cap, base**attempt)."""
    d = min(cap, base ** attempt)
    return d / 2 + (rng or random).uniform(0, d / 2)
