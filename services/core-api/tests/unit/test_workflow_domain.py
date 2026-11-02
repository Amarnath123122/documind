import uuid

from documind.workflows.domain import SourceChunk, draft_report, key_points

D = uuid.uuid4()


def chunk(text: str, page: int = 1) -> SourceChunk:
    return SourceChunk(D, 1, "policy.txt", page, text)


def test_key_points_dedupe_skip_short_and_limit():
    pts = key_points([chunk("Hi. Casual leave is 12 days. Casual leave is 12 days."),
                      chunk("Sick leave is 10 days. Maternity leave is 26 weeks.", 2),
                      chunk("Paternity leave is 15 working days.", 3)])
    assert pts == [("Casual leave is 12 days.", 1), ("Sick leave is 10 days.", 2),
                   ("Maternity leave is 26 weeks.", 2)]


def test_draft_has_title_sections_and_pages():
    out = draft_report("Leave", {D: [chunk("Casual leave is 12 days per year.", 4)]})
    assert out.startswith("# Leave\n") and "## policy.txt" in out
    assert "- Casual leave is 12 days per year. (p. 4)" in out
    assert "Requires approval" in out
