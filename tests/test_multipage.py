"""Multi-page PDF orchestration in extract_invoice.

These tests stub the LLM call (extract_documents_from_text) and the PDF text
step (extract_text_pages), so they're fast/deterministic and exercise only the
page-by-page merge/dedup/continuation logic — the part that makes "2 invoices
in one PDF" actually produce 2 rows.
"""
from app import extract
from app.schemas import FIELDS


def _doc(**vals):
    d = {f: None for f in FIELDS}
    d.update(vals)
    return d


def _run(monkeypatch, pages, per_page):
    monkeypatch.setattr(extract, "extract_text_pages", lambda _b: pages)
    monkeypatch.setattr(
        extract, "extract_documents_from_text", lambda text: per_page[text]
    )
    return extract.extract_invoice(b"%PDF-fake")


def test_two_invoices_on_two_pages_yield_two_rows(monkeypatch):
    pages = ["PAGE1", "PAGE2"]
    per_page = {
        "PAGE1": [_doc(vendor="Alfa s.r.o.", invoice_number="2026-001", total="12100")],
        "PAGE2": [_doc(vendor="Beta a.s.", invoice_number="2026-002", total="36300")],
    }
    rows = _run(monkeypatch, pages, per_page)
    assert [r["invoice_number"] for r in rows] == ["2026-001", "2026-002"]


def test_continuation_page_folds_total_into_previous(monkeypatch):
    pages = ["PAGE1", "PAGE2"]
    per_page = {
        "PAGE1": [_doc(vendor="Alfa s.r.o.", invoice_number="2026-001")],
        # page 2 has only a spilled total, no vendor/invoice_number
        "PAGE2": [_doc(subtotal="10000", tax="2100", total="12100")],
    }
    rows = _run(monkeypatch, pages, per_page)
    assert len(rows) == 1
    assert rows[0]["total"] == "12100"
    assert rows[0]["subtotal"] == "10000"


def test_same_invoice_repeated_on_both_pages_dedups(monkeypatch):
    pages = ["PAGE1", "PAGE2"]
    dup = _doc(vendor="Alfa s.r.o.", invoice_number="2026-001", total="12100")
    per_page = {"PAGE1": [dict(dup)], "PAGE2": [dict(dup)]}
    rows = _run(monkeypatch, pages, per_page)
    assert len(rows) == 1


def test_single_page_uses_model_list_directly(monkeypatch):
    # One page with two invoices -> the model's own list is returned as-is.
    pages = ["ONLYPAGE"]
    per_page = {
        "ONLYPAGE": [
            _doc(vendor="Alfa s.r.o.", invoice_number="2026-001"),
            _doc(vendor="Beta a.s.", invoice_number="2026-002"),
        ]
    }
    rows = _run(monkeypatch, pages, per_page)
    assert [r["vendor"] for r in rows] == ["Alfa s.r.o.", "Beta a.s."]
