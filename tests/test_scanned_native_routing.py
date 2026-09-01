"""What a scanned PDF costs before it reaches Claude's native document call.

With Anthropic configured, a PDF containing scans is read by the model itself
from the original bytes — so transcribing every page first buys nothing, and
the coordinate parsers in bank_parsers can't run on it anyway (a scan has no
embedded text layer to read coordinates from). These tests pin that: at most
ONE vision call, spent only on classifying page 1, and no deterministic parse
attempt. Text PDFs and every other provider must keep their old behavior.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import extract, main
from app.auth import User, current_user
from app.schemas import FIELDS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DOCS = FIXTURES / "real_documents"

# Page 1 carries embedded text, pages 2-3 are scans.
MIXED_PDF = FIXTURES / "mixed_text_and_scan_statement.pdf"
# mBank: fully text-native, and a layout the coordinate parser handles.
KNOWN_BANK_PDF = DOCS / "15872745_260601_260630.pdf"

USER_ID = "11111111-1111-1111-1111-111111111111"

STATEMENT_FROM_MODEL = {
    "account_number": "123456789/0800",
    "currency": "CZK",
    "transactions": [{"date": "5.12.2025", "amount": "-2500,00"}],
}


@pytest.fixture
def client():
    main.app.dependency_overrides[current_user] = lambda: User(
        id=USER_ID, email="tester@example.com"
    )
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def saved_rows(monkeypatch):
    """Capture the rows /api/upload would store, without touching Supabase."""
    rows = []

    def _insert(user_id, row):
        stored = {**row, "id": f"r{len(rows)}", "user_id": user_id}
        rows.append(stored)
        return stored

    def _update(user_id, row_id, fields):
        for row in rows:
            if row["id"] == row_id:
                row.update(fields)
                return row
        return None

    monkeypatch.setattr(main.db, "insert_invoice", _insert)
    monkeypatch.setattr(main.db, "update_invoice", _update)
    monkeypatch.setattr(main.db, "insert_bank_statement", _insert)
    monkeypatch.setattr(main.db, "update_bank_statement", _update)
    monkeypatch.setattr(
        main.db, "upload_source_file", lambda *_a: f"{USER_ID}/file.pdf"
    )
    return rows


@pytest.fixture
def anthropic(monkeypatch):
    monkeypatch.setattr(main.settings, "llm_provider", "anthropic")


@pytest.fixture
def ocr_calls(monkeypatch):
    """Count vision transcriptions; each one is a paid page-OCR call."""
    calls = []

    def _ocr(image):
        calls.append(image)
        return "Vypis z uctu\nOCR TRANSACTIONS"

    monkeypatch.setattr(extract, "transcribe_image", _ocr)
    return calls


@pytest.fixture
def no_deterministic_parse(monkeypatch):
    monkeypatch.setattr(
        main.bank_parsers,
        "parse_bank_statement",
        lambda *_a: pytest.fail("a scan has no text layer to parse by coordinates"),
    )


def _upload(client, path: Path):
    with path.open("rb") as fh:
        resp = client.post(
            "/api/upload", files={"file": (path.name, fh, "application/pdf")}
        )
    assert resp.status_code == 200
    return resp


def test_scanned_statement_goes_straight_to_the_document_call(
    client, saved_rows, anthropic, ocr_calls, no_deterministic_parse, monkeypatch
):
    """No page-OCR pass, no deterministic attempt — just the native call.

    Page 1 has embedded text, so classification costs nothing either: the whole
    document is one Claude call.
    """
    calls = []
    monkeypatch.setattr(extract, "classify_document", lambda _t: "bank_statement")
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_via_haiku_document",
        lambda pdf_bytes: calls.append(pdf_bytes) or STATEMENT_FROM_MODEL,
    )
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_from_text",
        lambda _t: pytest.fail("the chunked text path must not run for Anthropic"),
    )

    _upload(client, MIXED_PDF)

    assert calls == [MIXED_PDF.read_bytes()]
    assert ocr_calls == []
    assert saved_rows[0]["account_number"] == "123456789/0800"
    assert saved_rows[0]["matched_parser"] is None


def test_classifying_a_scanned_first_page_costs_one_ocr_call_at_most(
    client, saved_rows, anthropic, ocr_calls, no_deterministic_parse, monkeypatch
):
    """When page 1 is itself a scan, only page 1 is transcribed — not all three."""
    monkeypatch.setattr(extract, "_pages_needing_ocr", lambda _b: {0, 1, 2})
    monkeypatch.setattr(extract, "classify_document", lambda _t: "bank_statement")
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_via_haiku_document",
        lambda _b: STATEMENT_FROM_MODEL,
    )

    _upload(client, MIXED_PDF)

    assert len(ocr_calls) == 1
    assert saved_rows[0]["account_number"] == "123456789/0800"


def test_scanned_invoice_takes_the_document_call_without_ocring_pages(
    client, saved_rows, anthropic, ocr_calls, monkeypatch
):
    calls = []
    monkeypatch.setattr(extract, "classify_document", lambda _t: "invoice")
    monkeypatch.setattr(
        extract,
        "extract_invoices_via_haiku_document",
        lambda pdf_bytes: calls.append(pdf_bytes)
        or [{**{f: None for f in FIELDS}, "invoice_number": "2026-042"}],
    )
    monkeypatch.setattr(
        extract,
        "invoices_from_pages",
        lambda _p: pytest.fail("the per-page text path must not run for Anthropic"),
    )

    _upload(client, MIXED_PDF)

    assert calls == [MIXED_PDF.read_bytes()]
    assert ocr_calls == []
    assert [r["invoice_number"] for r in saved_rows] == ["2026-042"]


def test_text_pdf_still_takes_the_deterministic_path_under_anthropic(
    client, saved_rows, anthropic, ocr_calls, monkeypatch
):
    """A text-native statement is unaffected: parsed by coordinates, no LLM."""
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_via_haiku_document",
        lambda _b: pytest.fail("a known bank must not need an LLM"),
    )
    monkeypatch.setattr(
        extract,
        "classify_document",
        lambda _t: pytest.fail("a known statement must not need the classifier"),
    )

    _upload(client, KNOWN_BANK_PDF)

    assert ocr_calls == []
    assert saved_rows[0]["matched_parser"] == "mbank"
    assert len(saved_rows[0]["transactions"]) == 4


def test_other_providers_still_read_every_scanned_page(monkeypatch, ocr_calls):
    """Without a native document call there is nothing to save: OCR them all."""
    monkeypatch.setattr(main.settings, "llm_provider", "ollama")
    monkeypatch.setattr(extract, "classify_document", lambda _t: "bank_statement")

    plan = extract.plan_document(MIXED_PDF.read_bytes())

    assert len(ocr_calls) == 2
    assert plan.kind == "bank_statement"
    assert plan.pages is not None and len(plan.pages) == 3
