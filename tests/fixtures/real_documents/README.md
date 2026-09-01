# Real documents (not committed)

Drop real bank statement / invoice PDFs here to check classification and
extraction routing locally. Everything in this directory except this README is
gitignored — real documents contain personal data and must never be committed.

Compare the old blank-string heuristic against pdf-inspector's per-page
classification (offline, no API keys needed):

    PYTHONPATH=. .venv/bin/python scripts/inspect_pdf.py tests/fixtures/real_documents

Add `--extract` to also run the real extraction path, which calls the
configured vision model for every page routed to OCR.
