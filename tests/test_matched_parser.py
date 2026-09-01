"""Which engine parsed a bank statement, and what gets recorded about it.

`matched_parser` is the self-improvement signal: it holds the bank key when a
deterministic coordinate parser handled the statement, and stays None whenever
an LLM had to step in — so unknown bank layouts can be found later and turned
into new parsers. These tests drive the real /api/upload flow over the real
sample PDFs, with only the Anthropic call stubbed.
"""
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import extract, main
from app.auth import User, current_user
from app.schemas import BANK_TRANSACTION_FIELDS, BankStatement

DOCS = Path(__file__).resolve().parent / "fixtures" / "real_documents"

# A genuine statement from a bank bank_parsers doesn't know (a ČSOB-style MCZB
# export): looks_like_bank_statement says yes, detect_bank says None.
UNKNOWN_BANK_PDF = DOCS / "319666998_20251231_12_MCZB.pdf.pdf"
# mBank, which the coordinate parser does handle.
KNOWN_BANK_PDF = DOCS / "15872745_260601_260630.pdf"

USER_ID = "11111111-1111-1111-1111-111111111111"

STATEMENT_FROM_MODEL = {
    "account_number": "319666998/0300",
    "currency": "CZK",
    "period_start": "1.12.2025",
    "period_end": "31.12.2025",
    "opening_balance": "12000,00",
    "closing_balance": "9500,00",
    "transactions": [
        {"date": "5.12.2025", "counterparty_name": "ČEZ Prodej",
         "variable_symbol": "778899", "amount": "-2500,00"},
        {"date": "18.12.2025", "counterparty_name": "PETR JANOUSEK",
         "description": "PRICHOZI PLATBA", "amount": "1000,00"},
        {"date": "20.12.2025", "counterparty_name": "NAJEM",
         "amount": "-1000,00"},
    ],
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
        stored = {**row, "id": f"st{len(rows)}", "user_id": user_id}
        rows.append(stored)
        return stored

    def _update(user_id, statement_id, fields):
        for row in rows:
            if row["id"] == statement_id:
                row.update(fields)
                return row
        return None

    monkeypatch.setattr(main.db, "insert_bank_statement", _insert)
    monkeypatch.setattr(main.db, "update_bank_statement", _update)
    monkeypatch.setattr(
        main.db, "upload_source_file", lambda *_a: f"{USER_ID}/file.pdf"
    )
    return rows


def _upload(client, path: Path):
    with path.open("rb") as fh:
        resp = client.post(
            "/api/upload", files={"file": (path.name, fh, "application/pdf")}
        )
    assert resp.status_code == 200
    return resp


def _fake_anthropic(tool_input: dict, captured: dict):
    """An anthropic.Anthropic stand-in whose forced tool call returns
    ``tool_input``, recording the request kwargs into ``captured``."""

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=tool_input)]
            )

    class _Messages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _Stream()

    class _Client:
        def __init__(self, **_kwargs):
            self.messages = _Messages()

    return _Client


def test_document_call_sends_the_pdf_and_forces_the_schema(monkeypatch):
    import anthropic

    captured: dict = {}
    monkeypatch.setattr(
        anthropic, "Anthropic", _fake_anthropic(STATEMENT_FROM_MODEL, captured)
    )
    pdf_bytes = UNKNOWN_BANK_PDF.read_bytes()

    statement = extract.extract_bank_statement_via_haiku_document(pdf_bytes)

    # The PDF itself goes up — no PyMuPDF text, no per-page chunking.
    doc, text = captured["messages"][0]["content"]
    assert doc["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(doc["source"]["data"]) == pdf_bytes
    assert text["type"] == "text"
    # Structured output comes from a forced tool call typed by BankStatement.
    assert captured["tool_choice"] == {
        "type": "tool", "name": "record_bank_statement"
    }
    assert captured["tools"][0]["input_schema"] == BankStatement.model_json_schema()

    # Same shape the text path produces.
    assert statement["account_number"] == "319666998/0300"
    assert statement["closing_balance"] == "9500,00"
    assert len(statement["transactions"]) == 3
    assert set(statement["transactions"][0]) == set(BANK_TRANSACTION_FIELDS)
    assert [t["amount"] for t in statement["transactions"]] == [
        "-2500,00", "1000,00", "-1000,00"
    ]
    assert statement["transactions"][0]["variable_symbol"] == "778899"


def test_unknown_bank_uses_the_document_path_and_records_no_parser(
    client, saved_rows, monkeypatch
):
    monkeypatch.setattr(main.settings, "llm_provider", "anthropic")
    calls = []

    def _fake_document_call(pdf_bytes):
        calls.append(pdf_bytes)
        return extract._normalize_bank(
            BankStatement.model_validate(STATEMENT_FROM_MODEL)
        )

    monkeypatch.setattr(
        extract, "extract_bank_statement_via_haiku_document", _fake_document_call
    )
    # The chunked text path must not run: this is what the document call replaces.
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_from_text",
        lambda _t: pytest.fail("the chunked text path must not run for Anthropic"),
    )

    _upload(client, UNKNOWN_BANK_PDF)

    assert calls == [UNKNOWN_BANK_PDF.read_bytes()]
    row = saved_rows[0]
    assert row["matched_parser"] is None
    assert row["error"] is None
    assert row["account_number"] == "319666998/0300"
    assert [t["amount"] for t in row["transactions"]] == [
        "-2500,00", "1000,00", "-1000,00"
    ]


def test_unknown_bank_keeps_the_text_path_for_other_providers(
    client, saved_rows, monkeypatch
):
    monkeypatch.setattr(main.settings, "llm_provider", "ollama")
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_via_haiku_document",
        lambda _b: pytest.fail("the document call is Anthropic-only"),
    )
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_from_text",
        lambda _t: extract._normalize_bank(
            BankStatement.model_validate(STATEMENT_FROM_MODEL)
        ),
    )

    _upload(client, UNKNOWN_BANK_PDF)

    assert saved_rows[0]["matched_parser"] is None
    assert saved_rows[0]["transactions"]


def test_known_bank_records_its_parser_key(client, saved_rows, monkeypatch):
    # A recognized layout never reaches an LLM at all.
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_via_haiku_document",
        lambda _b: pytest.fail("a known bank must not need an LLM"),
    )
    monkeypatch.setattr(
        extract,
        "extract_bank_statement_from_text",
        lambda _t: pytest.fail("a known bank must not need an LLM"),
    )

    _upload(client, KNOWN_BANK_PDF)

    row = saved_rows[0]
    assert row["matched_parser"] == "mbank"
    assert len(row["transactions"]) == 4
