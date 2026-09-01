"""Deterministic tests for the Pohoda bank-statement XML export (no LLM)."""
from xml.dom import minidom

from app.pohoda import _signed_amount, build_pohoda_bank_xml


def test_signed_amount():
    assert _signed_amount("6 858,00") == ("6858.00", False)
    assert _signed_amount("-6 150,00") == ("6150.00", True)
    assert _signed_amount("250") == ("250.00", False)
    assert _signed_amount(None) == (None, False)
    assert _signed_amount("n/a") == (None, False)


def _sample_statement():
    return {
        "id": "st1",
        "account_number": "670100-2215872745/6210",
        "statement_number": "2026/06",
        "currency": "CZK",
        "transactions": [
            {
                "date": "19.06.2026",
                "counterparty_name": "PETR JANOUSEK",
                "counterparty_account": "670100-2219931431/6210",
                "variable_symbol": "12345",
                "description": "PRICHOZI PLATBA TRANSFER",
                "amount": "6 858,00",
                "balance_after": "7056,47",
            },
            {
                "date": "19.06.2026",
                "counterparty_name": "PAVLINA KOPALOVA",
                "counterparty_account": "670100-2223684576/6210",
                "variable_symbol": None,
                "description": "ODCHOZI PLATBA NA BYT",
                "amount": "-6 150,00",
                "balance_after": "906,47",
            },
        ],
    }


def test_build_bank_xml_structure():
    xml = build_pohoda_bank_xml([_sample_statement()])
    assert xml.startswith("<?xml")
    assert "<dat:dataPack" in xml and "</dat:dataPack>" in xml
    assert 'xmlns:bnk="http://www.stormware.cz/schema/version_2/bank.xsd"' in xml
    # one <bnk:bank> record per transaction
    assert xml.count("<bnk:bank ") == 2
    minidom.parseString(xml)  # well-formed


def test_bank_type_from_sign():
    xml = build_pohoda_bank_xml([_sample_statement()])
    assert "<bnk:bankType>receipt</bnk:bankType>" in xml   # +6858 incoming
    assert "<bnk:bankType>expense</bnk:bankType>" in xml   # -6150 outgoing
    # amounts are exported as positive magnitudes
    assert "<typ:priceNone>6858.00</typ:priceNone>" in xml
    assert "<typ:priceNone>6150.00</typ:priceNone>" in xml
    assert "-6150" not in xml


def test_bank_fields_mapped():
    xml = build_pohoda_bank_xml([_sample_statement()])
    # Pohoda's schema requires accountNo + bankCode split out of "number/code"
    assert "<typ:accountNo>670100-2219931431</typ:accountNo>" in xml
    assert "<typ:bankCode>6210</typ:bankCode>" in xml
    assert "<typ:company>PETR JANOUSEK</typ:company>" in xml
    assert "<bnk:symVar>12345</bnk:symVar>" in xml
    assert "2026-06-19" in xml  # date normalized to ISO
    assert "<bnk:text>PRICHOZI PLATBA TRANSFER</bnk:text>" in xml


def test_payment_account_omitted_without_bank_code():
    # An account with no "/bankcode" (e.g. an IBAN) can't satisfy Pohoda's
    # schema (which requires bankCode), so paymentAccount is omitted entirely
    # rather than emitted invalid.
    st = {
        "id": "s1",
        "transactions": [
            {"date": "01.06.2026", "amount": "100,00",
             "counterparty_account": "CZ6508000000192000145399"}
        ],
    }
    xml = build_pohoda_bank_xml([st])
    assert "paymentAccount" not in xml
    minidom.parseString(xml)  # still well-formed


def test_ico_attr_always_present():
    xml = build_pohoda_bank_xml([_sample_statement()])
    assert 'ico=""' in xml


def test_ico_attr_from_statement_field():
    st = {**_sample_statement(), "pohoda_ico": "12345678"}
    xml = build_pohoda_bank_xml([st])
    assert 'ico="12345678"' in xml


def test_empty_statement_yields_no_items():
    xml = build_pohoda_bank_xml([{"id": "x", "transactions": []}])
    assert "<bnk:bank " not in xml
    minidom.parseString(xml)


def test_accepts_single_dict():
    xml = build_pohoda_bank_xml(_sample_statement())
    assert xml.count("<bnk:bank ") == 2


def test_text_truncated_to_96_chars():
    st = {
        "id": "t",
        "transactions": [{"amount": "1", "description": "X" * 200}],
    }
    xml = build_pohoda_bank_xml(st)
    # the <bnk:text> content must be capped at 96 chars
    start = xml.index("<bnk:text>") + len("<bnk:text>")
    end = xml.index("</bnk:text>")
    assert end - start == 96
