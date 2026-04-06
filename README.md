# Family OS

Upload your kid's school Quintalplan PDF and get an .ics calendar file you can import into Google Calendar, Apple Calendar, or Outlook.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-your-key-here
python app.py
```

Open http://localhost:5000 in your browser.

## How it works

1. Upload a Quintalplan PDF (German school term plan)
2. Text is extracted from the PDF using pdfplumber
3. Claude AI identifies all events, dates, holidays, and appointments
4. An .ics calendar file is generated and downloaded

## Mock mode

To test without using the Claude API:

```bash
export MOCK_LLM=1
python app.py
```

This returns sample events so you can verify the UI and iCal generation work correctly.
