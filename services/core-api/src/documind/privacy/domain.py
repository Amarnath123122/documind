"""PII detection + permanent redaction. Pure domain.

Deterministic recognizers for high-precision identifiers. Behind a function boundary so a
Presidio/NER detector can replace it (ADR-0006). Only counts are kept: redacted values are
never stored, logged, indexed or sent to a model.
"""

import re
from collections import Counter
from dataclasses import dataclass


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass(frozen=True)
class Recognizer:
    label: str
    pattern: re.Pattern[str]
    validate: object = None  # callable(str) -> bool, optional

    def accepts(self, match: str) -> bool:
        return self.validate is None or self.validate(match)  # type: ignore[operator]


def _card_ok(m: str) -> bool:
    digits = re.sub(r"\D", "", m)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


def _phone_ok(m: str) -> bool:
    # 10-13 digits: national or international numbers, but not dates/years/amounts.
    return 10 <= len(re.sub(r"\D", "", m)) <= 13


# Order matters: longer/more specific identifiers first so they win overlaps.
RECOGNIZERS: tuple[Recognizer, ...] = (
    Recognizer("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Recognizer("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _card_ok),
    Recognizer("AADHAAR", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")),
    Recognizer("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    Recognizer("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Recognizer("PHONE", re.compile(r"(?<![\w+])\+?\(?\d[\d ()-]{8,16}\d(?!\w)"), _phone_ok),
)


@dataclass(frozen=True)
class Redaction:
    text: str
    counts: dict[str, int]


def redact(text: str) -> Redaction:
    spans: list[tuple[int, int, str]] = []
    for r in RECOGNIZERS:
        for m in r.pattern.finditer(text):
            if not r.accepts(m.group()):
                continue
            if any(m.start() < e and s < m.end() for s, e, _ in spans):
                continue  # already covered by a more specific recognizer
            spans.append((m.start(), m.end(), r.label))
    spans.sort()
    out, last, counts = [], 0, Counter()
    for s, e, label in spans:
        out += [text[last:s], f"[{label}_REDACTED]"]
        last = e
        counts[label] += 1
    out.append(text[last:])
    return Redaction("".join(out), dict(counts))
