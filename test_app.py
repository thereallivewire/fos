"""Tests for Family OS - Quintalplan PDF to iCal converter.

Uses red/green TDD: all tests written first, then code fixed to pass.
"""

import io
import json
import multiprocessing
import os
import socket
import tempfile
import time
import urllib.parse
import urllib.request

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# Ensure MOCK_LLM is set for tests that hit the upload endpoint
os.environ["MOCK_LLM"] = "1"

from app import app, extract_text_from_pdf, extract_events_with_llm, generate_ics, ics_escape, MOCK_EVENTS

FIXTURE_PDF = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "sample_quintalplan.pdf")


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _run_server(port):
    """Run Flask server in a subprocess (for real HTTP tests)."""
    os.environ["MOCK_LLM"] = "1"
    app.run(port=port, debug=False, use_reloader=False)


@pytest.fixture
def live_server():
    """Start a real Flask HTTP server on a random port for true end-to-end tests."""
    port = _get_free_port()
    proc = multiprocessing.Process(target=_run_server, args=(port,))
    proc.start()
    # Wait for server to be ready
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.join(timeout=5)


def make_pdf(text: str) -> bytes:
    """Create a minimal PDF with the given text content."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 750
    for line in text.split("\n"):
        c.drawString(72, y, line)
        y -= 15
    c.save()
    return buf.getvalue()


SAMPLE_QUINTALPLAN_TEXT = """\
Quintalplan 2025/2026 - Grundschule am Park

15.09.2025  Elternabend Klasse 3a, 19:00 Uhr
20.10.-01.11.2025  Herbstferien
05.11.2025  Schulfotograf 08:00-12:00
19.12.2025  Weihnachtsfeier 10:00-12:00
"""


# ──────────────────────────────────────────────
# 1. Unit tests: ics_escape
# ──────────────────────────────────────────────

class TestIcsEscape:
    def test_escapes_commas(self):
        assert ics_escape("Hello, world") == "Hello\\, world"

    def test_escapes_semicolons(self):
        assert ics_escape("A;B") == "A\\;B"

    def test_escapes_backslashes(self):
        assert ics_escape("path\\to") == "path\\\\to"

    def test_escapes_newlines(self):
        assert ics_escape("line1\nline2") == "line1\\nline2"

    def test_plain_text_unchanged(self):
        assert ics_escape("Elternabend") == "Elternabend"

    def test_german_umlauts_preserved(self):
        assert ics_escape("Frühlingsfest") == "Frühlingsfest"


# ──────────────────────────────────────────────
# 2. Unit tests: extract_text_from_pdf
# ──────────────────────────────────────────────

class TestExtractTextFromPdf:
    def test_extracts_text_from_valid_pdf(self):
        pdf_bytes = make_pdf("Schuljahr 2025 Elternabend 15.09.2025")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            f.flush()
            text = extract_text_from_pdf(f.name)
        os.unlink(f.name)
        assert "Schuljahr" in text
        assert "2025" in text

    def test_returns_empty_string_for_blank_pdf(self):
        pdf_bytes = make_pdf("")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            f.flush()
            text = extract_text_from_pdf(f.name)
        os.unlink(f.name)
        assert text.strip() == ""


# ──────────────────────────────────────────────
# 3. Unit tests: extract_events_with_llm (mock mode)
# ──────────────────────────────────────────────

class TestExtractEventsWithLlm:
    def test_mock_mode_returns_events(self):
        events = extract_events_with_llm("any text")
        assert isinstance(events, list)
        assert len(events) == 4

    def test_mock_events_have_required_fields(self):
        events = extract_events_with_llm("any text")
        for event in events:
            assert "title" in event
            assert "date" in event
            # date should be YYYY-MM-DD format
            parts = event["date"].split("-")
            assert len(parts) == 3
            assert len(parts[0]) == 4

    def test_mock_events_have_english_translation_fields(self):
        """Each event should have title_en (English translation or null if already English)."""
        events = extract_events_with_llm("any text")
        for event in events:
            assert "title_en" in event
        # German events should have non-null title_en
        elternabend = [e for e in events if "Elternabend" in e["title"]][0]
        assert elternabend["title_en"] is not None
        assert "parent" in elternabend["title_en"].lower() or "evening" in elternabend["title_en"].lower()


# ──────────────────────────────────────────────
# 4. Unit tests: generate_ics
# ──────────────────────────────────────────────

class TestGenerateIcs:
    def test_generates_valid_vcalendar_wrapper(self):
        ics = generate_ics([])
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert "END:VCALENDAR\r\n" in ics
        assert "VERSION:2.0\r\n" in ics

    def test_all_day_event(self):
        events = [{
            "title": "Herbstferien",
            "date": "2025-10-20",
            "end_date": "2025-11-01",
            "time_start": None,
            "time_end": None,
            "description": None,
        }]
        ics = generate_ics(events)
        assert "BEGIN:VEVENT\r\n" in ics
        assert "SUMMARY:Herbstferien\r\n" in ics
        assert "DTSTART;VALUE=DATE:20251020\r\n" in ics
        # DTEND should be exclusive: 2025-11-01 + 1 day = 20251102
        assert "DTEND;VALUE=DATE:20251102\r\n" in ics

    def test_single_all_day_event_dtend_is_next_day(self):
        events = [{
            "title": "Schulfotograf",
            "date": "2025-11-05",
            "end_date": None,
            "time_start": None,
            "time_end": None,
            "description": None,
        }]
        ics = generate_ics(events)
        assert "DTSTART;VALUE=DATE:20251105\r\n" in ics
        assert "DTEND;VALUE=DATE:20251106\r\n" in ics

    def test_timed_event(self):
        events = [{
            "title": "Elternabend",
            "date": "2025-09-15",
            "end_date": None,
            "time_start": "19:00",
            "time_end": "21:00",
            "description": None,
        }]
        ics = generate_ics(events)
        assert "DTSTART;TZID=Europe/Berlin:20250915T190000\r\n" in ics
        assert "DTEND;TZID=Europe/Berlin:20250915T210000\r\n" in ics

    def test_timed_event_without_end_time(self):
        events = [{
            "title": "Meeting",
            "date": "2025-09-15",
            "end_date": None,
            "time_start": "14:00",
            "time_end": None,
            "description": None,
        }]
        ics = generate_ics(events)
        assert "DTSTART;TZID=Europe/Berlin:20250915T140000\r\n" in ics
        # Same time as start (no duration) - at least it shouldn't crash
        assert "DTEND;TZID=Europe/Berlin:20250915T140000\r\n" in ics

    def test_description_included_when_present(self):
        events = [{
            "title": "Test",
            "date": "2025-01-01",
            "end_date": None,
            "time_start": None,
            "time_end": None,
            "description": "Some details here",
        }]
        ics = generate_ics(events)
        assert "DESCRIPTION:Some details here\r\n" in ics

    def test_description_omitted_when_none(self):
        events = [{
            "title": "Test",
            "date": "2025-01-01",
            "end_date": None,
            "time_start": None,
            "time_end": None,
            "description": None,
        }]
        ics = generate_ics(events)
        assert "DESCRIPTION" not in ics

    def test_special_chars_escaped_in_summary(self):
        events = [{
            "title": "Fest, Feier; Spaß",
            "date": "2025-06-01",
            "end_date": None,
            "time_start": None,
            "time_end": None,
            "description": None,
        }]
        ics = generate_ics(events)
        assert "SUMMARY:Fest\\, Feier\\; Spaß\r\n" in ics

    def test_each_event_has_unique_uid(self):
        events = [
            {"title": "A", "date": "2025-01-01", "end_date": None,
             "time_start": None, "time_end": None, "description": None},
            {"title": "B", "date": "2025-01-02", "end_date": None,
             "time_start": None, "time_end": None, "description": None},
        ]
        ics = generate_ics(events)
        uids = [line for line in ics.split("\r\n") if line.startswith("UID:")]
        assert len(uids) == 2
        assert uids[0] != uids[1]

    def test_uses_crlf_line_endings(self):
        events = [{"title": "X", "date": "2025-01-01", "end_date": None,
                    "time_start": None, "time_end": None, "description": None}]
        ics = generate_ics(events)
        # Should not contain bare \n (every \n should be preceded by \r)
        lines = ics.split("\r\n")
        for line in lines:
            assert "\n" not in line, f"Bare \\n found in line: {line!r}"


# ──────────────────────────────────────────────
# 4b. Unit tests: is_valid_pdf
# ──────────────────────────────────────────────

from app import is_valid_pdf

class TestIsValidPdf:
    def test_valid_pdf_returns_true(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(make_pdf("Schuljahr 2025"))
            path = f.name
        try:
            assert is_valid_pdf(path) is True
        finally:
            os.unlink(path)

    def test_empty_file_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        try:
            assert is_valid_pdf(path) is False
        finally:
            os.unlink(path)

    def test_text_file_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"this is not a pdf at all")
            path = f.name
        try:
            assert is_valid_pdf(path) is False
        finally:
            os.unlink(path)

    def test_truncated_pdf_returns_false(self):
        pdf_bytes = make_pdf("Test")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes[:50])  # truncate to corrupt it
            path = f.name
        try:
            assert is_valid_pdf(path) is False
        finally:
            os.unlink(path)

    def test_wrong_extension_content_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"PK\x03\x04this is actually a zip file")
            path = f.name
        try:
            assert is_valid_pdf(path) is False
        finally:
            os.unlink(path)


    def test_mock_events_produce_valid_ics(self):
        """End-to-end: MOCK_EVENTS -> generate_ics -> valid .ics output."""
        from app import MOCK_EVENTS
        ics = generate_ics(MOCK_EVENTS)
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert ics.strip().endswith("END:VCALENDAR")
        assert ics.count("BEGIN:VEVENT") == 4
        assert ics.count("END:VEVENT") == 4


# ──────────────────────────────────────────────
# 5. Integration tests: Flask routes
# ──────────────────────────────────────────────

class TestIndexRoute:
    def test_get_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_get_returns_html(self, client):
        resp = client.get("/")
        assert b"Family OS" in resp.data
        assert b"Quintalplan" in resp.data

    def test_error_query_param_shown_in_page(self, client):
        """GET /?error=Something+went+wrong should display the error message."""
        resp = client.get("/?error=Something+went+wrong")
        assert resp.status_code == 200
        assert b"Something went wrong" in resp.data


class TestUploadRoute:
    def test_post_without_file_redirects_with_error(self, client):
        resp = client.post("/upload")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_post_with_non_pdf_redirects_with_error(self, client):
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(b"not a pdf"), "test.txt"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_post_with_valid_pdf_returns_preview_html(self, client):
        """Upload now returns a preview page, not a direct .ics download."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        assert b"Review Extracted Events" in resp.data

    def test_confirm_returns_ics_with_correct_filename(self, client):
        events = json.dumps(MOCK_EVENTS)
        resp = client.post("/confirm", data={"events": events})
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/calendar")
        assert "quintalplan.ics" in resp.headers.get("Content-Disposition", "")

    def test_confirm_ics_contains_expected_events(self, client):
        events = json.dumps(MOCK_EVENTS)
        resp = client.post("/confirm", data={"events": events})
        ics_text = resp.data.decode("utf-8")
        assert ics_text.count("BEGIN:VEVENT") == 4
        assert "Elternabend" in ics_text
        assert "Herbstferien" in ics_text
        assert "Weihnachtsfeier" in ics_text

    def test_empty_pdf_redirects_with_error(self, client):
        pdf_bytes = make_pdf("")
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "empty.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_corrupt_pdf_redirects_with_error(self, client):
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(b"not a pdf at all"), "corrupt.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]
        # Error message should mention invalid/corrupt
        from urllib.parse import urlparse, parse_qs
        loc = resp.headers["Location"]
        error_msg = parse_qs(urlparse(loc).query)["error"][0].lower()
        assert "invalid" in error_msg or "corrupt" in error_msg

    def test_truncated_pdf_redirects_with_error(self, client):
        truncated = make_pdf("Test")[:50]
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(truncated), "truncated.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]


# ──────────────────────────────────────────────
# 5b. Preview + confirm flow
# ──────────────────────────────────────────────

class TestPreviewFlow:
    def test_upload_returns_preview_page(self, client):
        """After upload, user sees a preview of events — not an immediate download."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert b"text/html" in resp.content_type.encode()
        html = resp.data.decode("utf-8")
        # Should show event titles from MOCK_EVENTS
        assert "Elternabend" in html
        assert "Herbstferien" in html
        assert "Weihnachtsfeier" in html

    def test_preview_shows_english_translations(self, client):
        """Preview should show English translations below German event titles."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        html = resp.data.decode("utf-8")
        # Should show both the German title and English translation
        assert "Elternabend" in html
        # The English translation should appear somewhere in the preview
        from app import MOCK_EVENTS
        elternabend = [e for e in MOCK_EVENTS if "Elternabend" in e["title"]][0]
        assert elternabend["title_en"] in html

    def test_preview_shows_event_count(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        html = resp.data.decode("utf-8")
        assert "4" in html  # 4 events in MOCK_EVENTS

    def test_preview_has_confirm_form_posting_to_confirm(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        html = resp.data.decode("utf-8").lower()
        assert 'action="/confirm"' in html
        assert 'method="post"' in html

    def test_preview_embeds_events_json(self, client):
        """Events JSON must be embedded in the form so /confirm can generate .ics."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        html = resp.data.decode("utf-8")
        assert 'name="events"' in html
        # The embedded value must be valid JSON containing our events
        import re
        match = re.search(r'name="events"[^>]*value="([^"]*)"', html)
        if not match:
            # try textarea
            match = re.search(r'name="events"[^>]*>(.*?)</', html, re.DOTALL)
        assert match, "events field not found in form"
        import html as html_module
        events = json.loads(html_module.unescape(match.group(1)))
        assert isinstance(events, list)
        assert len(events) == 4

    def test_preview_has_start_over_link(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        html = resp.data.decode("utf-8")
        assert 'href="/"' in html

    def test_confirm_returns_ics_download(self, client):
        """/confirm receives events JSON and returns the .ics file."""
        events = json.dumps([{
            "title": "Elternabend",
            "date": "2025-09-15",
            "end_date": None,
            "time_start": "19:00",
            "time_end": "21:00",
            "description": None,
        }])
        resp = client.post("/confirm", data={"events": events})
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/calendar")
        assert b"BEGIN:VCALENDAR" in resp.data
        assert b"Elternabend" in resp.data
        assert "quintalplan.ics" in resp.headers.get("Content-Disposition", "")

    def test_confirm_with_invalid_json_redirects_with_error(self, client):
        resp = client.post("/confirm", data={"events": "not valid json {"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_confirm_with_missing_events_redirects_with_error(self, client):
        resp = client.post("/confirm")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]


# ──────────────────────────────────────────────
# 6. Frontend HTML tests: catch download/upload bugs
# ──────────────────────────────────────────────

class TestFrontendHtml:
    """Tests that inspect the rendered HTML to catch structural issues."""

    def test_no_iframe_in_page(self, client):
        """An iframe target swallows file downloads instead of saving them."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert "<iframe" not in html.lower()

    def test_no_form_with_target_attribute(self, client):
        """A form with target= sends the response to a frame, not the browser."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        # No <form ... target=...> pattern
        assert 'target=' not in html.lower().split('<script')[0]

    def test_upload_form_uses_plain_post(self, client):
        """Upload must use a plain <form> POST, not XHR/fetch."""
        resp = client.get("/")
        html = resp.data.decode("utf-8").lower()
        assert 'action="/upload"' in html
        assert 'method="post"' in html
        assert 'enctype="multipart/form-data"' in html

    def test_no_xhr_or_fetch_for_upload(self, client):
        """Upload must NOT use XHR or fetch — plain form submit only."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        scripts = html.split("<script>")
        if len(scripts) > 1:
            script = scripts[1].split("</script>")[0]
            assert "XMLHttpRequest" not in script, "Must not use XHR for upload"
            assert "fetch(" not in script, "Must not use fetch for upload"

    def test_file_input_accepts_only_pdf(self, client):
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert 'accept=".pdf"' in html

    def test_file_input_has_name_pdf(self, client):
        """File input must have name='pdf' for the plain form POST."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert 'name="pdf"' in html

    def test_submit_button_starts_disabled(self, client):
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert "disabled" in html.split("submitBtn")[0].split("<button")[-1] or \
               "disabled" in html.split("id=\"submitBtn\"")[1].split(">")[0]

    def test_has_loading_spinner_css(self, client):
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert ".spinner" in html
        assert "@keyframes spin" in html

    def test_has_error_and_success_message_styles(self, client):
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert ".message.error" in html
        assert ".message.success" in html


# ──────────────────────────────────────────────
# 7. End-to-end with test client: full pipeline test
# ──────────────────────────────────────────────

def _extract_events_json_from_preview(html: str) -> str:
    """Parse the events JSON out of the hidden form field in the preview page."""
    import re, html as html_module
    match = re.search(r'name="events"\s+value="([^"]*)"', html)
    assert match, "events hidden field not found in preview HTML"
    return html_module.unescape(match.group(1))


class TestEndToEndTestClient:
    def test_full_pipeline_pdf_to_importable_ics(self, client):
        """Full two-step flow: upload → preview → confirm → .ics download."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)

        # Step 1: Upload → preview
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type

        # Step 2: Extract embedded events JSON from preview
        events_json = _extract_events_json_from_preview(resp.data.decode("utf-8"))

        # Step 3: Confirm → download .ics
        resp2 = client.post("/confirm", data={"events": events_json})
        assert resp2.status_code == 200
        ics_text = resp2.data.decode("utf-8")

        # Step 4: Validate iCal structure
        assert ics_text.startswith("BEGIN:VCALENDAR\r\n")
        assert ics_text.strip().endswith("END:VCALENDAR")
        assert "VERSION:2.0" in ics_text
        assert "PRODID:" in ics_text
        vevent_count = ics_text.count("BEGIN:VEVENT")
        assert vevent_count > 0
        assert ics_text.count("END:VEVENT") == vevent_count
        for vevent in ics_text.split("BEGIN:VEVENT")[1:]:
            assert "UID:" in vevent
            assert "DTSTART" in vevent
            assert "SUMMARY:" in vevent
        lines = ics_text.split("\r\n")
        for line in lines[:-1]:
            assert "\n" not in line

    def test_fixture_pdf_full_flow(self, client):
        """Real fixture PDF through the full upload → preview → confirm flow."""
        with open(FIXTURE_PDF, "rb") as f:
            resp = client.post("/upload", data={
                "pdf": (f, "sample_quintalplan.pdf"),
            }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        assert b"Elternabend" in resp.data  # events shown in preview

        events_json = _extract_events_json_from_preview(resp.data.decode("utf-8"))
        resp2 = client.post("/confirm", data={"events": events_json})
        assert resp2.status_code == 200
        assert resp2.content_type.startswith("text/calendar")
        assert resp2.data.count(b"BEGIN:VEVENT") == 4

    def test_fixture_pdf_download_to_file(self, client):
        """Full flow: fixture PDF → preview → confirm → save .ics to disk."""
        with open(FIXTURE_PDF, "rb") as f:
            resp = client.post("/upload", data={
                "pdf": (f, "sample_quintalplan.pdf"),
            }, content_type="multipart/form-data")
        events_json = _extract_events_json_from_preview(resp.data.decode("utf-8"))

        resp2 = client.post("/confirm", data={"events": events_json})
        assert resp2.status_code == 200

        with tempfile.NamedTemporaryFile(suffix=".ics", delete=False) as out:
            out.write(resp2.data)
            ics_path = out.name
        try:
            with open(ics_path, "rb") as f:
                raw = f.read()
            ics_text = raw.decode("utf-8")
            assert ics_text.startswith("BEGIN:VCALENDAR\r\n")
            assert ics_text.strip().endswith("END:VCALENDAR")
            assert b"\r\n" in raw
            assert os.path.getsize(ics_path) > 100
        finally:
            os.unlink(ics_path)


# ──────────────────────────────────────────────
# 8. Real HTTP end-to-end: live server + network upload
# ──────────────────────────────────────────────

def _build_multipart_body(filepath, field_name="pdf"):
    """Build a multipart/form-data body for urllib (no requests dependency)."""
    boundary = "----TestBoundary7MA4YWxkTrZu0gW"
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


class TestRealHttpEndToEnd:
    """Tests that start a real Flask HTTP server and make actual network requests.
    This catches issues that the Flask test client hides, like ClientDisconnected."""

    def _upload_and_confirm(self, live_server, filepath):
        """Helper: POST PDF to /upload, parse preview, POST to /confirm, return .ics bytes."""
        import re, html as html_module

        # Step 1: upload
        body, content_type = _build_multipart_body(filepath)
        req = urllib.request.Request(
            f"{live_server}/upload", data=body,
            headers={"Content-Type": content_type}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        assert resp.status == 200
        preview_html = resp.read().decode("utf-8")
        assert "Review Extracted Events" in preview_html

        # Step 2: extract events JSON from hidden field
        match = re.search(r'name="events"\s+value="([^"]*)"', preview_html)
        assert match, "events field not found in preview"
        events_json = html_module.unescape(match.group(1))

        # Step 3: confirm → get .ics
        confirm_body = urllib.parse.urlencode({"events": events_json}).encode()
        req2 = urllib.request.Request(
            f"{live_server}/confirm", data=confirm_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        resp2 = urllib.request.urlopen(req2, timeout=30)
        assert resp2.status == 200
        return resp2

    def test_real_http_upload_returns_ics(self, live_server):
        """Full two-step flow over real HTTP: upload → preview → confirm → .ics."""
        resp = self._upload_and_confirm(live_server, FIXTURE_PDF)
        assert "text/calendar" in resp.headers.get("Content-Type", "")
        assert "quintalplan.ics" in resp.headers.get("Content-Disposition", "")
        ics_text = resp.read().decode("utf-8")
        assert ics_text.startswith("BEGIN:VCALENDAR\r\n")
        assert ics_text.count("BEGIN:VEVENT") == 4

    def test_real_http_download_saves_valid_ics_file(self, live_server):
        """Full browser simulation over real HTTP: save .ics to disk and validate."""
        resp = self._upload_and_confirm(live_server, FIXTURE_PDF)
        raw_bytes = resp.read()

        with tempfile.NamedTemporaryFile(suffix=".ics", delete=False) as out:
            out.write(raw_bytes)
            ics_path = out.name
        try:
            with open(ics_path, "rb") as f:
                raw = f.read()
            ics_text = raw.decode("utf-8")
            assert ics_text.startswith("BEGIN:VCALENDAR\r\n")
            assert ics_text.strip().endswith("END:VCALENDAR")
            assert b"\r\n" in raw
            assert ics_text.count("BEGIN:VEVENT") == 4
            for vevent in ics_text.split("BEGIN:VEVENT")[1:]:
                assert "UID:" in vevent and "DTSTART" in vevent and "SUMMARY:" in vevent
        finally:
            os.unlink(ics_path)

    def test_real_http_error_for_non_pdf(self, live_server):
        """Upload a non-PDF file over real HTTP — must redirect with error."""
        boundary = "----TestBoundary7MA4YWxkTrZu0gW"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="pdf"; filename="test.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"this is not a pdf\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{live_server}/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        # Disable auto-redirect to check the 302 response
        import urllib.request as ur
        opener = ur.build_opener(ur.HTTPHandler)
        class NoRedirect(ur.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
        opener = ur.build_opener(NoRedirect)
        try:
            opener.open(req, timeout=10)
            assert False, "Should have been redirected"
        except urllib.error.HTTPError as e:
            assert e.code == 302
            assert "error=" in e.headers.get("Location", "")
