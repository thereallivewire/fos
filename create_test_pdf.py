"""Generate a realistic Quintalplan test PDF fixture."""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

OUTPUT = "tests/fixtures/sample_quintalplan.pdf"


def create_quintalplan():
    c = canvas.Canvas(OUTPUT, pagesize=A4)
    width, height = A4

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, height - 2 * cm, "Quintalplan 2025/2026")
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - 2.8 * cm, "Grundschule am Park - Klasse 3a")

    # Events
    c.setFont("Helvetica-Bold", 11)
    c.drawString(2 * cm, height - 4.5 * cm, "1. Quartal (September - November 2025)")

    events = [
        ("Mo, 01.09.2025", "Erster Schultag"),
        ("Mi, 10.09.2025", "Klassenpflegschaft, 19:30 Uhr im Klassenraum"),
        ("Mo, 15.09.2025", "Elternabend Klasse 3a, 19:00-21:00 Uhr"),
        ("Fr, 26.09.2025", "Wandertag (ganztags)"),
        ("20.10.-01.11.2025", "Herbstferien"),
        ("Mi, 05.11.2025", "Schulfotograf, 08:00-12:00 Uhr"),
        ("Fr, 14.11.2025", "Bundesweiter Vorlesetag"),
        ("Do, 20.11.2025", "Elternsprechtag, 14:00-18:00 Uhr"),
    ]

    c.setFont("Helvetica", 10)
    y = height - 5.8 * cm
    for date, event in events:
        c.drawString(2 * cm, y, f"{date}    {event}")
        y -= 0.6 * cm

    # Second quarter
    c.setFont("Helvetica-Bold", 11)
    y -= 0.8 * cm
    c.drawString(2 * cm, y, "2. Quartal (Dezember 2025 - Februar 2026)")

    events2 = [
        ("Fr, 19.12.2025", "Weihnachtsfeier, 10:00-12:00 Uhr"),
        ("22.12.2025-06.01.2026", "Weihnachtsferien"),
        ("Di, 28.01.2026", "Zeugnisausgabe (Unterrichtsschluss 10:45 Uhr)"),
        ("Mo, 02.02.2026", "Pädagogischer Tag (unterrichtsfrei)"),
        ("16.02.-20.02.2026", "Winterferien / Karnevalsferien"),
        ("Mi, 25.02.2026", "Schulkonferenz, 18:00 Uhr"),
    ]

    c.setFont("Helvetica", 10)
    y -= 1.2 * cm
    for date, event in events2:
        c.drawString(2 * cm, y, f"{date}    {event}")
        y -= 0.6 * cm

    # Footer
    c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(
        width / 2, 1.5 * cm, "Änderungen vorbehalten. Stand: August 2025"
    )

    c.save()
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    create_quintalplan()
