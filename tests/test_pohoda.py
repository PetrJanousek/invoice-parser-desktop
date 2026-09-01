"""Deterministic tests for the Pohoda XML export (no LLM involved)."""
from xml.dom import minidom

from app.pohoda import _amount, _iso_date, build_pohoda_xml


def test_amount_normalization():
    assert _amount("50 000 Kc") == "50000.00"
    assert _amount("1,234.56") == "1234.56"
    assert _amount("975,80") == "975.80"
    assert _amount(None) is None
    assert _amount("n/a") is None


def test_iso_date_variants():
    assert _iso_date("2026-05-30") == "2026-05-30"
    assert _iso_date("1.6.2026") == "2026-06-01"
    assert _iso_date("14/03/2026") == "2026-03-14"
    assert _iso_date("nonsense") is None


def test_build_pohoda_xml_contains_invoice():
    row = {
        "id": "abc",
        "vendor": "Novak s.r.o.",
        "ico": "27182734",
        "invoice_number": "2026-0117",
        "variable_symbol": "20260117",
        "invoice_date": "1.6.2026",
        "due_date": "15.6.2026",
        "subtotal": "50000",
        "tax": "10500",
        "total": "60500",
        "bank_account": "1234567890/0800",
    }
    xml = build_pohoda_xml([row])
    assert xml.startswith('<?xml')
    assert "<dat:dataPack" in xml and "</dat:dataPack>" in xml
    assert "receivedInvoice" in xml
    assert "<typ:company>Novak s.r.o.</typ:company>" in xml
    assert "<typ:ico>27182734</typ:ico>" in xml
    assert "<inv:symVar>20260117</inv:symVar>" in xml
    assert "2026-06-01" in xml  # invoice date normalized to ISO


def test_xml_escaping():
    xml = build_pohoda_xml([{"id": "1", "vendor": "A & B <Ltd>", "total": "10"}])
    assert "A &amp; B &lt;Ltd&gt;" in xml


def test_receipt_produces_voucher():
    row = {
        "id": "r1",
        "document_type": "receipt",
        "vendor": "Kavarna s.r.o.",
        "ico": "12345678",
        "invoice_date": "3.6.2026",
        "subtotal": "100",
        "tax": "21",
        "total": "121",
    }
    xml = build_pohoda_xml([row])
    assert "<vch:voucher" in xml
    assert "<vch:voucherType>expense</vch:voucherType>" in xml
    assert "<inv:invoice" not in xml
    assert "<typ:company>Kavarna s.r.o.</typ:company>" in xml
    assert "<vch:text>Receipt from Kavarna s.r.o.</vch:text>" in xml
    assert "2026-06-03" in xml  # date normalized to ISO
    # subtotal + tax present -> high-rate price block
    assert "<typ:priceHigh>100.00</typ:priceHigh>" in xml
    assert "<typ:priceHighVAT>21.00</typ:priceHighVAT>" in xml
    assert "<typ:priceHighSum>121.00</typ:priceHighSum>" in xml
    minidom.parseString(xml)  # well-formed


def test_receipt_price_none_without_vat_split():
    row = {
        "id": "r2",
        "document_type": "receipt",
        "vendor": "Shop",
        "total": "500",
    }
    xml = build_pohoda_xml([row])
    assert "<vch:voucher" in xml
    assert "<typ:priceNone>500.00</typ:priceNone>" in xml
    assert "priceHigh" not in xml
    minidom.parseString(xml)


def test_invoice_document_type_produces_invoice():
    row = {
        "id": "i1",
        "document_type": "invoice",
        "vendor": "Novak s.r.o.",
        "total": "60500",
    }
    xml = build_pohoda_xml([row])
    assert "<inv:invoice" in xml
    assert "receivedInvoice" in xml
    assert "<vch:voucher" not in xml
    minidom.parseString(xml)


def test_null_document_type_defaults_to_invoice():
    row = {"id": "i2", "document_type": None, "vendor": "X", "total": "10"}
    xml = build_pohoda_xml([row])
    assert "<inv:invoice" in xml
    assert "<vch:voucher" not in xml
    # a row with no document_type key at all also defaults to invoice
    xml2 = build_pohoda_xml([{"id": "i3", "vendor": "Y", "total": "5"}])
    assert "<inv:invoice" in xml2
    assert "<vch:voucher" not in xml2
    minidom.parseString(xml)


def test_ico_attr_always_present():
    # No pohoda_ico set anywhere -> emitted blank, not omitted, so Pohoda
    # rejects the import loudly instead of silently skipping the ico check.
    xml = build_pohoda_xml([{"id": "i1", "vendor": "X", "total": "10"}])
    assert 'ico=""' in xml


def test_ico_attr_from_row_field():
    xml = build_pohoda_xml([{"id": "i1", "vendor": "X", "total": "10", "pohoda_ico": "12345678"}])
    assert 'ico="12345678"' in xml


def test_ico_attr_explicit_param_overrides_row_field():
    xml = build_pohoda_xml(
        [{"id": "i1", "vendor": "X", "total": "10", "pohoda_ico": "12345678"}],
        ico="87654321",
    )
    assert 'ico="87654321"' in xml
    assert "12345678" not in xml


def test_mixed_list_produces_both_element_types():
    rows = [
        {"id": "i1", "document_type": "invoice", "vendor": "Faktura Ltd",
         "total": "1000"},
        {"id": "r1", "document_type": "receipt", "vendor": "Paragon Ltd",
         "total": "50"},
    ]
    xml = build_pohoda_xml(rows)
    assert "<inv:invoice" in xml
    assert "<vch:voucher" in xml
    assert 'xmlns:vch="http://www.stormware.cz/schema/version_2/voucher.xsd"' in xml
    minidom.parseString(xml)  # single dataPack, well-formed
