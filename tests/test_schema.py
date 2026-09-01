"""Robustness of the InvoiceFields schema to messy LLM output."""
from app.schemas import InvoiceFields


def test_unwraps_value_dicts_from_small_models():
    # qwen2.5:7b sometimes emits {"value": X} per field instead of X.
    m = InvoiceFields(
        invoice_number={"value": "5 - Daňový doklad"},
        variable_symbol={"value": "23415"},
        subtotal={"value": 47311},          # nested + numeric
        currency={"value": "Kč"},
        bank_account={"value": "123456789/0800"},
    )
    assert m.invoice_number == "5 - Daňový doklad"
    assert m.variable_symbol == "23415"
    assert m.subtotal == "47311"            # coerced number -> str
    assert m.currency == "Kč"
    assert m.bank_account == "123456789/0800"


def test_coerces_plain_numbers_and_nulls():
    m = InvoiceFields(total=60500, tax=None, vendor="Novak s.r.o.")
    assert m.total == "60500"
    assert m.tax is None
    assert m.vendor == "Novak s.r.o."


def test_drops_unparseable_nested_structures():
    m = InvoiceFields(vendor={"unexpected": "shape"}, total=["10", "20"])
    assert m.vendor is None
    assert m.total is None
