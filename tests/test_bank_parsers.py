"""Deterministic bank-statement parser tests over the real sample PDFs.

No LLM involved: these exercise the coordinate parsers directly. The strongest
check sums the parsed transactions and compares to each statement's own printed
credit/debit totals — if those match, every movement was captured with the
right amount and sign.
"""
import re
from pathlib import Path

import fitz
import pytest

from app import bank_parsers as bp
from app.pohoda import _amount

DATA = Path(__file__).resolve().parent.parent / "test_data"

CASES = {
    "mbank": {
        "file": "15872745_260601_260630.pdf",
        "txns": 4,
        "credits": r"Kreditní položky[\s\d]*?" + bp._BAL + r"\s*CZK",
        "debits": r"Debetní položky[\s\d]*?" + bp._BAL + r"\s*CZK",
    },
    "raiffeisen": {
        "file": "Vypis_5777223003_CZK_2026_006.pdf",
        "txns": 220,
        "credits": r"Příjmy celkem:\s*" + bp._BAL,
        "debits": r"Výdaje celkem:\s*" + bp._BAL,
    },
    "ceska_sporitelna": {
        "file": "Vypis_z_uctu_0-7062814319_z_20260630.pdf",
        "txns": 20,
        "credits": r"Celkem přišlo:\s*" + bp._BAL,
        "debits": r"Celkem odešlo:\s*" + bp._BAL,
    },
}


def _num(s):
    v = _amount(s)
    return float(v) if v else 0.0


def _load(case):
    path = DATA / case["file"]
    if not path.exists():
        pytest.skip(f"sample {case['file']} not present")
    pdf = path.read_bytes()
    text = "\n".join(p.get_text() for p in fitz.open(str(path)))
    return pdf, text


@pytest.mark.parametrize("bank,case", CASES.items())
def test_detection_and_classification(bank, case):
    _, text = _load(case)
    assert bp.looks_like_bank_statement(text) is True
    assert bp.detect_bank(text) == bank


@pytest.mark.parametrize("bank,case", CASES.items())
def test_transaction_count(bank, case):
    pdf, text = _load(case)
    st = bp.parse_bank_statement(pdf, text)
    assert st is not None
    assert len(st["transactions"]) == case["txns"]


@pytest.mark.parametrize("bank,case", CASES.items())
def test_totals_match_statement_summary(bank, case):
    pdf, text = _load(case)
    st = bp.parse_bank_statement(pdf, text)
    txns = st["transactions"]
    credits = sum(_num(t["amount"]) for t in txns
                  if not str(t["amount"]).strip().startswith("-"))
    debits = abs(sum(_num(t["amount"]) for t in txns
                     if str(t["amount"]).strip().startswith("-")))
    exp_credits = _num(re.search(case["credits"], text).group(1))
    exp_debits = abs(_num(re.search(case["debits"], text).group(1)))
    assert credits == pytest.approx(exp_credits, abs=0.05)
    assert debits == pytest.approx(exp_debits, abs=0.05)


@pytest.mark.parametrize("bank,case", CASES.items())
def test_header_fields(bank, case):
    pdf, text = _load(case)
    st = bp.parse_bank_statement(pdf, text)
    assert st["account_number"]           # every statement has its own account
    assert st["currency"] == "CZK"
    assert st["opening_balance"]
    assert st["closing_balance"]
    # every transaction has a date and an amount
    assert all(t["date"] and t["amount"] for t in st["transactions"])


def test_raiffeisen_variable_symbol_populated():
    """Regression: VS was dropped by the LLM path; the parser must capture it."""
    case = CASES["raiffeisen"]
    pdf, text = _load(case)
    st = bp.parse_bank_statement(pdf, text)
    with_vs = [t for t in st["transactions"] if t["variable_symbol"]]
    assert len(with_vs) > 100
    # VS is digits only, never a merged blob
    assert all(re.fullmatch(r"\d+", t["variable_symbol"]) for t in with_vs)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name):
    path = FIXTURES / name
    pdf = path.read_bytes()
    return pdf, "\n".join(p.get_text() for p in fitz.open(str(path)))


@pytest.mark.parametrize(
    "name",
    ["cs_layout_control.pdf", "cs_layout_shifted_40pt.pdf", "cs_layout_shifted_70pt.pdf"],
)
def test_shifted_column_layout_still_parses(name):
    """A table shifted sideways (scan skew / other template) must still parse.

    Regression: past a fixed offset every word fell out of its hardcoded column
    band and the parser returned nothing at all, with no signal to the caller.
    """
    pdf, text = _load_fixture(name)
    st = bp.parse_bank_statement(pdf, text)
    assert st is not None
    txns = st["transactions"]
    assert len(txns) == 4
    assert [t["date"] for t in txns] == [f"0{i}.06.2026" for i in range(1, 5)]
    assert all(t["amount"] == "-500,00" for t in txns)
    assert all(t["variable_symbol"] == "9988" for t in txns)
    assert all(t["counterparty_name"] == "DODAVATEL" for t in txns)


def test_drifting_row_stays_one_row():
    """Words on one visual line survive the vertical drift of a scanned page.

    Regression: each word sat only 2pt below the previous one, but the drift
    accumulated to 8pt across the line and split one transaction into three
    phantom rows.
    """
    doc = fitz.open()
    page = doc.new_page()
    words = ["01.06.2026", "Platba", "DODAVATEL", "9988", "-500,00"]
    for i, w in enumerate(words):
        page.insert_text((45 + i * 110, 200 + i * 2.0), w, fontsize=9, fontname="helv")
    pdf = doc.tobytes()

    rows = bp._page_rows(fitz.open(stream=pdf, filetype="pdf")[0])
    assert len(rows) == 1
    assert [w for _x, w in rows[0][1]] == words


def test_unknown_bank_returns_none():
    assert bp.parse_bank_statement(b"%PDF-1.4", "some random invoice text") is None
