"""Text-first / vision-fallback routing in extract_invoice.

These tests stub the LLM and PDF steps so they're fast and deterministic. They
verify the money-saving contract: a text PDF never touches the vision model,
and a scanned PDF (no extractable text) falls back to the vision path. The
vision path is now **OCR-first** — the vision model only transcribes each page
to text, then the same text extractor splits the fields (still multi-invoice +
continuation aware).
"""
from pathlib import Path

from app import extract
from app.extract import PdfTextError
from app.schemas import FIELDS

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _doc(**vals):
    d = {f: None for f in FIELDS}
    d.update(vals)
    return d


def _boom_vision(*_a, **_k):
    raise AssertionError("vision model must not be called when text exists")


def test_text_pdf_uses_text_model_not_vision(monkeypatch):
    monkeypatch.setattr(extract, "extract_text_pages", lambda _b: ["INVOICE TEXT"])
    monkeypatch.setattr(
        extract,
        "extract_documents_from_text",
        lambda _t: [_doc(vendor="Alfa s.r.o.", invoice_number="2026-001")],
    )
    # The vision path (transcribe_image) must never run for a text PDF.
    monkeypatch.setattr(extract, "transcribe_image", _boom_vision)

    rows = extract.extract_invoice(b"%PDF-text")
    assert [r["invoice_number"] for r in rows] == ["2026-001"]


def test_scanned_pdf_falls_back_to_vision(monkeypatch):
    # No extractable text -> extract_text_pages raises like a scanned PDF.
    def _raise(_b):
        raise PdfTextError("scanned")

    monkeypatch.setattr(extract, "extract_text_pages", _raise)
    monkeypatch.setattr(extract, "render_pages_to_images", lambda _b: [b"PNG1"])
    # Vision model just OCRs the page; the text extractor does the fields.
    monkeypatch.setattr(extract, "transcribe_image", lambda _img: "SCANNED TEXT")
    monkeypatch.setattr(
        extract,
        "extract_documents_from_text",
        lambda _t: [_doc(vendor="Scan Ltd", invoice_number="S-9", total="500")],
    )

    rows = extract.extract_invoice(b"%PDF-scan")
    assert [r["invoice_number"] for r in rows] == ["S-9"]


def test_scanned_multipage_vision_merges_pages(monkeypatch):
    def _raise(_b):
        raise PdfTextError("scanned")

    # Each page image transcribes to its own text, and each text extracts to
    # its own docs; page 2 is a continuation (only a spilled total).
    ocr = {b"PNG1": "PAGE1 TEXT", b"PNG2": "PAGE2 TEXT"}
    by_text = {
        "PAGE1 TEXT": [_doc(vendor="Alfa s.r.o.", invoice_number="2026-001")],
        "PAGE2 TEXT": [_doc(subtotal="10000", tax="2100", total="12100")],
    }
    monkeypatch.setattr(extract, "extract_text_pages", _raise)
    monkeypatch.setattr(extract, "render_pages_to_images", lambda _b: [b"PNG1", b"PNG2"])
    monkeypatch.setattr(extract, "transcribe_image", lambda img: ocr[img])
    monkeypatch.setattr(extract, "extract_documents_from_text", lambda t: by_text[t])

    rows = extract.extract_invoice(b"%PDF-scan2")
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "2026-001"
    assert rows[0]["total"] == "12100"


def test_mixed_text_and_scan_pdf_ocrs_only_the_empty_pages(monkeypatch):
    """A PDF whose page 1 has text and pages 2-3 are scans keeps all 3 pages.

    The scanned pages must be OCR'd individually instead of being silently
    dropped because the document as a whole had *some* extractable text.
    """
    pdf = (FIXTURES / "mixed_text_and_scan_statement.pdf").read_bytes()
    calls = []

    def _ocr(img):
        calls.append(img)
        return f"OCR PAGE {len(calls)}"

    monkeypatch.setattr(extract, "transcribe_image", _ocr)

    pages = extract.pdf_to_pages(pdf)
    assert len(pages) == 3
    assert "Vypis z uctu" in pages[0]          # embedded text kept as-is
    assert pages[1:] == ["OCR PAGE 1", "OCR PAGE 2"]
    assert len(calls) == 2                     # the text page was not rendered


def test_watermarked_scan_page_is_ocrd_despite_having_text(monkeypatch):
    """A scan whose only embedded text is a footer watermark still gets OCR'd.

    PyMuPDF returns "Strana 2 / 2" for page 2, so the old blank-string check
    called it a text page and dropped the whole transaction body. pdf-inspector
    classifies it as image_based, which is what routes it to the vision path.
    """
    pdf = (FIXTURES / "watermarked_scan_page.pdf").read_bytes()
    calls = []

    def _ocr(img):
        calls.append(img)
        return "OCR TRANSACTIONS"

    monkeypatch.setattr(extract, "transcribe_image", _ocr)

    raw = extract.extract_text_pages(pdf)
    assert raw[1].strip()                       # not blank: the old check passed it
    assert extract._pages_needing_ocr(pdf) == {1}

    pages = extract.pdf_to_pages(pdf)
    assert len(pages) == 2
    assert "Vypis z uctu" in pages[0]           # embedded text kept as-is
    assert pages[1] == "OCR TRANSACTIONS"
    assert len(calls) == 1                      # only the scan page was transcribed


def test_low_confidence_classification_ocrs_every_page(monkeypatch):
    """An unsure verdict is treated as "OCR everything" rather than trusted."""
    class _Unsure:
        pdf_type = "mixed"
        page_count = 3
        pages_needing_ocr = []
        confidence = 0.1

    monkeypatch.setattr(
        extract.pdf_inspector, "classify_pdf_bytes", lambda _b: _Unsure()
    )
    assert extract._pages_needing_ocr(b"%PDF-unsure") == {0, 1, 2}


def test_classifier_failure_degrades_to_blank_check(monkeypatch):
    """If pdf-inspector can't read the PDF, routing must not get more expensive."""
    def _raise(_b):
        raise ValueError("not a pdf")

    monkeypatch.setattr(extract.pdf_inspector, "classify_pdf_bytes", _raise)
    monkeypatch.setattr(extract, "extract_text_pages", lambda _b: ["TEXT", ""])
    monkeypatch.setattr(extract, "render_pages_to_images", lambda _b: [b"P1", b"P2"])
    monkeypatch.setattr(extract, "transcribe_image", lambda _img: "OCR")

    assert extract._pages_needing_ocr(b"%PDF-broken") == set()
    assert extract.pdf_to_pages(b"%PDF-broken") == ["TEXT", "OCR"]


def test_transcribe_image_builds_data_uri_and_returns_text(monkeypatch):
    """transcribe_image sends a base64 data-URI image block and returns text."""
    captured = {}

    class _Result:
        content = "  transcribed page text  "

    class _FakeModel:
        def invoke(self, messages):
            captured["messages"] = messages
            return _Result()

    monkeypatch.setattr(extract, "get_vision_chat_model", lambda: _FakeModel())
    text = extract.transcribe_image(b"\x89PNG-bytes")

    assert text == "transcribed page text"  # stripped
    human = captured["messages"][1]
    blocks = human.content
    assert blocks[0]["type"] == "text"
    img = blocks[1]
    assert img["type"] == "image_url"
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_content_to_text_flattens_block_list():
    """A provider returning a list of content blocks is flattened to text."""
    blocks = [
        {"type": "text", "text": "line one"},
        {"type": "image_url", "image_url": {"url": "..."}},
        {"type": "text", "text": "line two"},
    ]
    assert extract._content_to_text(blocks) == "line one\nline two"
    assert extract._content_to_text("plain") == "plain"
