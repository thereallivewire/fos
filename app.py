import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pdfplumber
from flask import Flask, Response, render_template, request

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

EXTRACTION_SYSTEM_PROMPT = """\
You are an expert at extracting calendar events from German school term plans \
(Quintalplan / Quartalsplan / Schuljahresplan).

Given the raw text extracted from a PDF, identify ALL events, dates, and appointments.

Rules:
- German date formats: "12.05.2025", "12. Mai 2025", "12.05.", "Mo, 12.05."
- If only a month/day is given without a year, infer the year from context \
(school year, surrounding dates, headers)
- Multi-day events (e.g. "12.-16.05.2025", "Herbstferien 21.10.-01.11.") \
should have both date and end_date
- Holidays and vacation periods are events too
- If a time is mentioned (e.g. "19:00 Uhr", "14.30-16.00"), include it
- For all-day events, leave time_start and time_end as null

Return ONLY a JSON array. No markdown fences, no explanation. Each object:
{
  "title": "Event name in original language",
  "date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD or null",
  "time_start": "HH:MM or null",
  "time_end": "HH:MM or null",
  "description": "Additional details or null"
}"""

MOCK_EVENTS = [
    {
        "title": "Elternabend Klasse 3a",
        "date": "2025-09-15",
        "end_date": None,
        "time_start": "19:00",
        "time_end": "21:00",
        "description": "Elternabend im Klassenzimmer",
    },
    {
        "title": "Herbstferien",
        "date": "2025-10-20",
        "end_date": "2025-11-01",
        "time_start": None,
        "time_end": None,
        "description": None,
    },
    {
        "title": "Schulfotograf",
        "date": "2025-11-05",
        "end_date": None,
        "time_start": "08:00",
        "time_end": "12:00",
        "description": "Bitte an ordentliche Kleidung denken",
    },
    {
        "title": "Weihnachtsfeier",
        "date": "2025-12-19",
        "end_date": None,
        "time_start": "10:00",
        "time_end": "12:00",
        "description": None,
    },
]


def extract_text_from_pdf(filepath: str) -> str:
    """Extract text and table data from all pages of a PDF."""
    parts = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
            for table in page.extract_tables():
                for row in table:
                    cells = [c or "" for c in row]
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_events_with_llm(text: str) -> list[dict]:
    """Send extracted PDF text to Claude and get structured event data back."""
    if os.environ.get("MOCK_LLM"):
        return MOCK_EVENTS

    # Truncate to ~50k chars to avoid slow/failed API calls on huge PDFs
    if len(text) > 50000:
        text = text[:50000] + "\n\n[... truncated ...]"

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw)


def ics_escape(text: str) -> str:
    """Escape special characters per RFC 5545."""
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def generate_ics(events: list[dict]) -> str:
    """Generate an RFC 5545 iCalendar string from a list of event dicts."""
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyOS//Quintalplan//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Quintalplan",
        "X-WR-TIMEZONE:Europe/Berlin",
    ]

    for event in events:
        date = event["date"].replace("-", "")
        end_date = event.get("end_date")
        time_start = event.get("time_start")
        time_end = event.get("time_end")
        title = event.get("title", "Unnamed Event")
        description = event.get("description")

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uuid.uuid4()}@familyos")
        lines.append(f"DTSTAMP:{now}")

        if time_start:
            ts = time_start.replace(":", "") + "00"
            lines.append(f"DTSTART;TZID=Europe/Berlin:{date}T{ts}")
            if time_end:
                te = time_end.replace(":", "") + "00"
                end_d = (end_date or event["date"]).replace("-", "")
                lines.append(f"DTEND;TZID=Europe/Berlin:{end_d}T{te}")
            else:
                # Default: 1 hour duration
                lines.append(f"DTEND;TZID=Europe/Berlin:{date}T{ts}")
        else:
            # All-day event
            lines.append(f"DTSTART;VALUE=DATE:{date}")
            if end_date:
                # DTEND is exclusive, so add one day
                ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                lines.append(f"DTEND;VALUE=DATE:{ed.strftime('%Y%m%d')}")
            else:
                # Single all-day: DTEND = next day
                ed = datetime.strptime(event["date"], "%Y-%m-%d") + timedelta(days=1)
                lines.append(f"DTEND;VALUE=DATE:{ed.strftime('%Y%m%d')}")

        lines.append(f"SUMMARY:{ics_escape(title)}")
        if description:
            lines.append(f"DESCRIPTION:{ics_escape(description)}")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    print(f"[UPLOAD] POST /upload received", flush=True)
    print(f"[UPLOAD] content-length: {request.content_length}", flush=True)
    print(f"[UPLOAD] content-type: {request.content_type}", flush=True)
    try:
        files_keys = list(request.files.keys())
        print(f"[UPLOAD] files keys: {files_keys}", flush=True)
    except Exception as e:
        print(f"[UPLOAD] ERROR accessing request.files: {e}", flush=True)
        import traceback; traceback.print_exc()

    file = request.files.get("pdf")
    print(f"[UPLOAD] file object: {file}", flush=True)
    print(f"[UPLOAD] filename: '{file.filename}' (len={len(file.filename)})" if file else "[UPLOAD] file is None", flush=True)

    if not file or not file.filename:
        print(f"[UPLOAD] FAIL: no file or empty filename", flush=True)
        return {"error": "Please upload a PDF file."}, 400
    if not file.filename.lower().endswith(".pdf"):
        print(f"[UPLOAD] FAIL: not a PDF: '{file.filename}'", flush=True)
        return {"error": "Please upload a PDF file."}, 400

    log.info("File received: %s", file.filename)

    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        file.save(tmp.name)
        tmp.close()
        file_size = os.path.getsize(tmp.name)
        log.info("Saved to temp file: %s (%d bytes)", tmp.name, file_size)

        log.info("Extracting text from PDF...")
        text = extract_text_from_pdf(tmp.name)
        log.info("Extracted %d characters of text", len(text))
        if not text.strip():
            log.warning("No text extracted from PDF")
            return {"error": "Could not extract text from the PDF. The file may be image-based or empty."}, 400

        log.info("Calling LLM for event extraction...")
        events = extract_events_with_llm(text)
        log.info("LLM returned %d events", len(events))
        if not events:
            return {"error": "No events could be extracted from the PDF."}, 400

        log.info("Generating iCal file...")
        ics_content = generate_ics(events)
        log.info("iCal generated (%d bytes). Returning download.", len(ics_content))

        return Response(
            ics_content,
            mimetype="text/calendar; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=quintalplan.ics"},
        )
    except json.JSONDecodeError as e:
        log.exception("JSON decode error: %s", e)
        return {"error": "Failed to parse the extracted events. Please try again."}, 500
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        return {"error": f"An error occurred: {str(e)}"}, 500
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


if __name__ == "__main__":
    app.run(debug=True, port=5000)
