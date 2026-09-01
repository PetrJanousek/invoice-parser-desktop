"""Invoice extraction straight from the PDF, Anthropic-only.

Invoices have no deterministic parser, so this path replaces the whole
per-page text extraction whenever the provider is Anthropic: the PDF goes up
as a native document block and one forced tool call returns every invoice on
it. These tests drive /api/upload with the Anthropic call stubbed, and check
that every other provider still takes the text path.
"""
import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import extract, main
from app.auth import User, current_user
from app.schemas import FIELDS, InvoiceExtraction

USER_ID = "11111111-1111-1111-1111-111111111111"

INVOICE_TEXT = (
    "FAKTURA - danovy doklad c. 2026-042\n"
    "Dodavatel: Alfa s.r.o., ICO 27182734\n"
    "Odberatel: Beta a.s., ICO 45317054\n"
    "Celkem bez DPH 10000  DPH 2100  Celkem k uhrade 12100 Kc\n"
)

INVOICES_FROM_MODEL = {
    "invoices": [
        {
            "document_type": "invoice",
            "vendor": "Alfa s.r.o.",
            "ico": "27182734",
            "customer": "Beta a.s.",
            "customer_ico": "45317054",
            "invoice_number": "2026-042",
            "variable_symbol": "2026042",
            "invoice_date": "1.6.2026",
            "due_date": "15.6.2026",
            "currency": "Kc",
            "subtotal": "10000",
            "tax": "2100",
            "total": 12100,
            "bank_account": "123456789/0100",
        },
        {
            "vendor": "Gama s.r.o.",
            "invoice_number": "2026-043",
            "total": "36300",
            "currency": "  ",
        },
    ]
}


def _pdf_bytes() -> bytes:
    import fitz

    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), INVOICE_TEXT)
        return doc.tobytes()


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
        stored = {**row, "id": f"inv{len(rows)}", "user_id": user_id}
        rows.append(stored)
        return stored

    def _update(user_id, invoice_id, fields):
        for row in rows:
            if row["id"] == invoice_id:
                row.update(fields)
                return row
        return None

    monkeypatch.setattr(main.db, "insert_invoice", _insert)
    monkeypatch.setattr(main.db, "update_invoice", _update)
    monkeypatch.setattr(
        main.db, "upload_source_file", lambda *_a: f"{USER_ID}/file.pdf"
    )
    return rows


@pytest.fixture
def invoice_pdf(monkeypatch):
    """A one-page invoice PDF that reaches the invoice branch of /api/upload."""
    pdf = _pdf_bytes()
    monkeypatch.setattr(extract, "pdf_to_pages", lambda _b: [INVOICE_TEXT])
    monkeypatch.setattr(extract, "classify_document", lambda _t: "invoice")
    return pdf


def _upload(client, pdf: bytes):
    resp = client.post(
        "/api/upload", files={"file": ("invoice.pdf", pdf, "application/pdf")}
    )
    assert resp.status_code == 200
    return resp


def _fake_anthropic(tool_input: dict, captured: dict):
    """An anthropic.Anthropic stand-in whose forced tool call returns
    ``tool_input``, recording the request kwargs into ``captured``."""

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=tool_input)]
            )

    class _Client:
        def __init__(self, **_kwargs):
            self.messages = _Messages()

    return _Client


def test_document_call_sends_the_pdf_and_forces_the_schema(monkeypatch):
    import anthropic

    captured: dict = {}
    monkeypatch.setattr(
        anthropic, "Anthropic", _fake_anthropic(INVOICES_FROM_MODEL, captured)
    )
    pdf = _pdf_bytes()

    invoices = extract.extract_invoices_via_haiku_document(pdf)

    # The PDF itself goes up — no PyMuPDF text, no per-page calls.
    doc, text = captured["messages"][0]["content"]
    assert doc["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(doc["source"]["data"]) == pdf
    assert text["type"] == "text"
    assert captured["system"] == extract.SYSTEM_PROMPT
    # Structured output comes from a forced tool call typed by InvoiceExtraction.
    assert captured["tool_choice"] == {"type": "tool", "name": "record_invoices"}
    assert (
        captured["tools"][0]["input_schema"]
        == InvoiceExtraction.model_json_schema()
    )

    # Same shape the text path produces: exactly FIELDS, empty -> None,
    # currency canonicalized, numbers coerced to strings.
    assert len(invoices) == 2
    assert all(set(inv) == set(FIELDS) for inv in invoices)
    assert invoices[0]["vendor"] == "Alfa s.r.o."
    assert invoices[0]["customer_ico"] == "45317054"
    assert invoices[0]["variable_symbol"] == "2026042"
    assert invoices[0]["currency"] == "CZK"
    assert invoices[0]["total"] == "12100"
    assert invoices[1]["invoice_number"] == "2026-043"
    assert invoices[1]["currency"] is None
    assert invoices[1]["ico"] is None


def test_a_model_returning_no_invoices_still_yields_one_empty_row(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _fake_anthropic({}, {}))

    assert extract.extract_invoices_via_haiku_document(_pdf_bytes()) == [
        {f: None for f in FIELDS}
    ]


def test_upload_uses_the_document_path_for_anthropic(
    client, saved_rows, invoice_pdf, monkeypatch
):
    monkeypatch.setattr(main.settings, "llm_provider", "anthropic")
    calls = []

    def _fake_document_call(pdf_bytes):
        calls.append(pdf_bytes)
        return [
            extract._normalize(inv.model_dump())
            for inv in InvoiceExtraction.model_validate(
                INVOICES_FROM_MODEL
            ).invoices
        ]

    monkeypatch.setattr(
        extract, "extract_invoices_via_haiku_document", _fake_document_call
    )
    # The per-page text path must not run: this is what the document call replaces.
    monkeypatch.setattr(
        extract,
        "invoices_from_pages",
        lambda _p: pytest.fail("the per-page text path must not run for Anthropic"),
    )

    _upload(client, invoice_pdf)

    assert calls == [invoice_pdf]
    assert [r["invoice_number"] for r in saved_rows] == ["2026-042", "2026-043"]
    assert saved_rows[0]["error"] is None
    assert saved_rows[0]["filename"] == "invoice.pdf"


def test_upload_keeps_the_text_path_for_other_providers(
    client, saved_rows, invoice_pdf, monkeypatch
):
    monkeypatch.setattr(main.settings, "llm_provider", "ollama")
    monkeypatch.setattr(
        extract,
        "extract_invoices_via_haiku_document",
        lambda _b: pytest.fail("the document call is Anthropic-only"),
    )
    monkeypatch.setattr(
        extract,
        "invoices_from_pages",
        lambda pages: [{**{f: None for f in FIELDS}, "invoice_number": "2026-042"}],
    )

    _upload(client, invoice_pdf)

    assert [r["invoice_number"] for r in saved_rows] == ["2026-042"]


def test_a_failing_document_call_stores_an_error_row(
    client, saved_rows, invoice_pdf, monkeypatch
):
    monkeypatch.setattr(main.settings, "llm_provider", "anthropic")

    def _boom(_pdf):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(extract, "extract_invoices_via_haiku_document", _boom)

    _upload(client, invoice_pdf)

    assert len(saved_rows) == 1
    assert "overloaded" in saved_rows[0]["error"]
