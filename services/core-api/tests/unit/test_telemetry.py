import json
import logging

import pytest

from documind.common.telemetry import JsonFormatter, ScrubFilter, scrub


@pytest.mark.parametrize(("raw", "leak"), [
    ("PUT /quarantine/x?sv=2024&se=1&sp=cw&sig=AbC%2Bdef%3D&sr=b", "AbC%2Bdef%3D"),
    ("DefaultEndpointsProtocol=http;AccountKey=Eby8vdM02xNOcq==;BlobEndpoint=x", "Eby8vdM02xNOcq=="),
    ("cookie: dm_session=eyJ1aWQiOi.sig123; dm_csrf=x", "eyJ1aWQiOi.sig123"),
    ("postgresql+asyncpg://documind_app:dev-app-pw@db:5432/documind", "dev-app-pw"),
    ('headers={"X-CSRF-Token": "tok-123-abc"}', "tok-123-abc"),
])
def test_scrub_removes_bearer_material(raw, leak):
    out = scrub(raw)
    assert leak not in out and "[REDACTED]" in out


def test_scrub_leaves_ordinary_text():
    assert scrub("job 42 succeeded doc=abc") == "job 42 succeeded doc=abc"


def test_json_log_is_scrubbed_including_args_and_exceptions():
    logger = logging.getLogger("t")
    record = logger.makeRecord("t", logging.ERROR, __file__, 1, "upload url %s",
                               ("https://x/blob?sig=SECRET",), None)
    try:
        raise RuntimeError("failed with AccountKey=KEYVALUE;")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    ScrubFilter().filter(record)
    line = JsonFormatter("documind-test").format(record)
    data = json.loads(line)
    assert data["service"] == "documind-test" and data["level"] == "ERROR"
    assert "SECRET" not in line and "KEYVALUE" not in line
