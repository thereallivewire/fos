import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pdfplumber
from flask import Flask, Response, redirect, render_template, request

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
  "title_en": "English translation of title, or null if already in English",
  "date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD or null",
  "time_start": "HH:MM or null",
  "time_end": "HH:MM or null",
  "description": "Additional details or null",
  "description_en": "English translation of description, or null if already in English or description is null"
}"""

MOCK_EVENTS = [
    {
        "title": "Elternabend Klasse 3a",
        "title_en": "Parent evening class 3a",
        "date": "2025-09-15",
        "end_date": None,
        "time_start": "19:00",
        "time_end": "21:00",
        "description": "Elternabend im Klassenzimmer",
        "description_en": "Parent evening in the classroom",
    },
    {
        "title": "Herbstferien",
        "title_en": "Autumn holidays",
        "date": "2025-10-20",
        "end_date": "2025-11-01",
        "time_start": None,
        "time_end": None,
        "description": None,
        "description_en": None,
    },
    {
        "title": "Schulfotograf",
        "title_en": "School photographer",
        "date": "2025-11-05",
        "end_date": None,
        "time_start": "08:00",
        "time_end": "12:00",
        "description": "Bitte an ordentliche Kleidung denken",
        "description_en": "Please remember to wear neat clothing",
    },
    {
        "title": "Weihnachtsfeier",
        "title_en": "Christmas party",
        "date": "2025-12-19",
        "end_date": None,
        "time_start": "10:00",
        "time_end": "12:00",
        "description": None,
        "description_en": None,
    },
]


def is_valid_pdf(filepath: str) -> bool:
    """Return True only if the file is a readable, non-corrupt PDF."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            return False
        with pdfplumber.open(filepath) as pdf:
            _ = pdf.pages  # force parsing
        return True
    except Exception:
        return False


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


def _error_redirect(msg: str):
    """Redirect back to index with an error message in the query string."""
    from urllib.parse import quote
    return redirect(f"/?error={quote(msg)}")


@app.route("/")
def index():
    error = request.args.get("error")
    return render_template("index.html", error=error)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("pdf")
    if not file or not file.filename:
        return _error_redirect("Please upload a PDF file.")
    if not file.filename.lower().endswith(".pdf"):
        return _error_redirect("Please upload a PDF file.")

    log.info("File received: %s", file.filename)

    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        file.save(tmp.name)
        tmp.close()
        file_size = os.path.getsize(tmp.name)
        log.info("Saved to temp file: %s (%d bytes)", tmp.name, file_size)

        if not is_valid_pdf(tmp.name):
            return _error_redirect("Invalid or corrupt PDF file.")

        log.info("Extracting text from PDF...")
        text = extract_text_from_pdf(tmp.name)
        log.info("Extracted %d characters of text", len(text))
        if not text.strip():
            log.warning("No text extracted from PDF")
            return _error_redirect("Could not extract text from the PDF. The file may be image-based or empty.")

        log.info("Calling LLM for event extraction...")
        events = extract_events_with_llm(text)
        log.info("LLM returned %d events", len(events))
        if not events:
            return _error_redirect("No events could be extracted from the PDF.")

        log.info("Rendering preview page...")
        return render_template("preview.html", events=events, events_json=json.dumps(events))
    except json.JSONDecodeError as e:
        log.exception("JSON decode error: %s", e)
        return _error_redirect("Failed to parse the extracted events. Please try again.")
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        return _error_redirect(f"An error occurred: {str(e)}")
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


@app.route("/confirm", methods=["POST"])
def confirm():
    events_json = request.form.get("events")
    if not events_json:
        return _error_redirect("No events data received.")
    try:
        events = json.loads(events_json)
    except json.JSONDecodeError:
        return _error_redirect("Invalid events data.")

    ics_content = generate_ics(events)
    log.info("iCal generated (%d bytes). Returning download.", len(ics_content))
    return Response(
        ics_content,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=quintalplan.ics"},
    )


def _get_local_ip():
    """Get the LAN IP address of this machine."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        local_ip = _get_local_ip()
        print(f"\n  Network: http://{local_ip}:{port}\n")
    app.run(host="0.0.0.0", debug=True, port=port, threaded=True)
