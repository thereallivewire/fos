"""Tests for Family OS - Quintalplan PDF to iCal converter.

Uses red/green TDD: all tests written first, then code fixed to pass.
"""

import io
import json
import os
import tempfile

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# Ensure MOCK_LLM is set for tests that hit the upload endpoint
os.environ["MOCK_LLM"] = "1"

from app import app, extract_text_from_pdf, extract_events_with_llm, generate_ics, ics_escape


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


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


class TestUploadRoute:
    def test_post_without_file_returns_400(self, client):
        resp = client.post("/upload")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_post_with_non_pdf_returns_400(self, client):
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(b"not a pdf"), "test.txt"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_post_with_valid_pdf_returns_ics(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/calendar")
        assert b"BEGIN:VCALENDAR" in resp.data
        assert b"END:VCALENDAR" in resp.data
        assert b"BEGIN:VEVENT" in resp.data

    def test_ics_download_has_correct_filename(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert "quintalplan.ics" in resp.headers.get("Content-Disposition", "")

    def test_ics_contains_expected_mock_events(self, client):
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        ics_text = resp.data.decode("utf-8")
        # MOCK_EVENTS has 4 events
        assert ics_text.count("BEGIN:VEVENT") == 4
        assert "Elternabend" in ics_text
        assert "Herbstferien" in ics_text
        assert "Weihnachtsfeier" in ics_text

    def test_empty_pdf_returns_400(self, client):
        pdf_bytes = make_pdf("")
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "empty.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data


# ──────────────────────────────────────────────
# 6. End-to-end: full pipeline test
# ──────────────────────────────────────────────

class TestEndToEnd:
    def test_full_pipeline_pdf_to_importable_ics(self, client):
        """Simulate the complete user flow: upload PDF -> get .ics -> validate it."""
        pdf_bytes = make_pdf(SAMPLE_QUINTALPLAN_TEXT)

        # Step 1: Upload
        resp = client.post("/upload", data={
            "pdf": (io.BytesIO(pdf_bytes), "quintalplan.pdf"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200

        # Step 2: Parse the returned .ics
        ics_text = resp.data.decode("utf-8")

        # Step 3: Validate iCal structure
        assert ics_text.startswith("BEGIN:VCALENDAR\r\n")
        assert ics_text.strip().endswith("END:VCALENDAR")

        # Step 4: Verify required iCal properties
        assert "VERSION:2.0" in ics_text
        assert "PRODID:" in ics_text
        assert "CALSCALE:GREGORIAN" in ics_text

        # Step 5: Verify events are properly formed
        vevent_count = ics_text.count("BEGIN:VEVENT")
        assert vevent_count > 0
        assert ics_text.count("END:VEVENT") == vevent_count

        # Step 6: Every VEVENT must have UID, DTSTAMP, DTSTART, SUMMARY
        vevents = ics_text.split("BEGIN:VEVENT")[1:]  # skip preamble
        for vevent in vevents:
            assert "UID:" in vevent
            assert "DTSTAMP:" in vevent
            assert "DTSTART" in vevent
            assert "SUMMARY:" in vevent

        # Step 7: Verify timezone info
        assert "Europe/Berlin" in ics_text or "VALUE=DATE" in ics_text

        # Step 8: Verify CRLF line endings throughout
        lines = ics_text.split("\r\n")
        for line in lines[:-1]:  # last element may be empty
            assert "\n" not in line
