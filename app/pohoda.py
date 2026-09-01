"""Pohoda (Stormware) XML export for received invoices (faktury přijaté).

Builds a Pohoda "dataPack" matching the Stormware XML import schema:
  https://api.stormware.cz/pohoda/xml-import-podporovana-data/faktury/faktury/

Ported from the original app. Rows are plain dicts carrying the extracted
fields; elements are emitted only when the row has a usable value.
"""
import datetime
import re
from xml.sax.saxutils import escape as xml_escape, quoteattr


def _ico_attr(ico: str | None) -> str:
    """dataPack ``ico`` attribute for the target accounting unit.

    Pohoda rejects a package whose ``ico`` does not match the open unit's IČO
    ("Tento balíček není určen pro tuto jednotku"). The IČO is set manually per
    row (see ``pohoda_ico`` on invoices/bank_statements) rather than defaulted,
    so a blank value is emitted as-is (``ico=""``) and Pohoda will reject the
    import — a loud signal that the user forgot to set it, rather than a
    silent unchecked import.
    """
    return f"ico={quoteattr((ico or '').strip())} "


def _iso_date(value) -> str | None:
    """Best-effort parse of a stored date string into ISO YYYY-MM-DD."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None
    fmts = [
        "%d.%m.%Y", "%d. %m. %Y", "%d/%m/%Y", "%m/%d/%Y",
        "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d",
        "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%d %b %Y",
    ]
    for fmt in fmts:
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amount(value) -> str | None:
    """Normalize a stored amount into a plain numeric string like '1234.56'."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return None


def _digits(value) -> str | None:
    if value is None:
        return None
    d = re.sub(r"\D", "", str(value))
    return d or None


def _el(tag: str, value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text == "":
        return ""
    return f"<{tag}>{xml_escape(text)}</{tag}>"


def _split_account(value) -> tuple[str | None, str | None]:
    """Split a Czech account string into (accountNo, bankCode).

    Czech accounts are written ``[prefix-]number/bankcode`` (e.g.
    ``19-2000145399/0800`` or ``500027832/0800``); the 4-digit bank code
    follows the slash. Returns ``(None, None)`` when no bank code is present
    (a bare number or an IBAN), because Pohoda's schema requires *both* parts
    inside ``paymentAccount`` — a lone ``accountNo`` fails validation.
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if "/" not in s:
        return None, None
    number, _, code = s.rpartition("/")
    number = number.strip()
    code = re.sub(r"\D", "", code)
    if not number or not code:
        return None, None
    return number, code


def _payment_account(wrapper: str, value) -> str:
    """Build ``<wrapper><typ:accountNo/><typ:bankCode/></wrapper>`` or ``""``.

    Pohoda's schema requires accountNo **and** bankCode together, so we emit
    the element only when both can be derived; otherwise we omit it entirely
    rather than produce a record Pohoda rejects.
    """
    number, code = _split_account(value)
    if not number or not code:
        return ""
    return (
        f"<{wrapper}>"
        f"{_el('typ:accountNo', number)}"
        f"{_el('typ:bankCode', code)}"
        f"</{wrapper}>"
    )


def _invoice_item(row: dict) -> str:
    invoice_number = row.get("invoice_number")
    vendor = row.get("vendor")
    ico = row.get("ico")
    bank_account = row.get("bank_account")

    sym_var = _digits(row.get("variable_symbol")) or _digits(invoice_number)
    date = _iso_date(row.get("invoice_date"))
    date_due = _iso_date(row.get("due_date"))

    header = ["<inv:invoiceHeader>"]
    header.append("<inv:invoiceType>receivedInvoice</inv:invoiceType>")
    if invoice_number is not None and str(invoice_number) != "":
        header.append(
            "<inv:number><typ:numberRequested>"
            f"{xml_escape(str(invoice_number))}"
            "</typ:numberRequested></inv:number>"
        )
    if sym_var:
        header.append(_el("inv:symVar", sym_var))
    if date:
        header.append(_el("inv:date", date))
    if date_due:
        header.append(_el("inv:dateDue", date_due))
    if vendor or ico:
        addr = ["<inv:partnerIdentity><typ:address>"]
        addr.append(_el("typ:company", vendor))
        addr.append(_el("typ:ico", ico))
        addr.append("</typ:address></inv:partnerIdentity>")
        header.append("".join(a for a in addr if a))
    if bank_account:
        header.append(_payment_account("inv:paymentAccount", bank_account))
    desc_num = invoice_number if invoice_number else "?"
    desc_vendor = vendor if vendor else "unknown vendor"
    header.append(_el("inv:text", f"Invoice {desc_num} from {desc_vendor}"))
    header.append("</inv:invoiceHeader>")

    subtotal = _amount(row.get("subtotal"))
    tax = _amount(row.get("tax"))
    total = _amount(row.get("total"))

    home = ["<inv:homeCurrency>"]
    if subtotal is not None and tax is not None:
        home.append(_el("typ:priceHigh", subtotal))
        home.append(_el("typ:priceHighVAT", tax))
        if total is not None:
            home.append(_el("typ:priceHighSum", total))
    elif total is not None:
        home.append(_el("typ:priceNone", total))
    home.append("</inv:homeCurrency>")

    summary = "<inv:invoiceSummary>" + "".join(home) + "</inv:invoiceSummary>"

    return (
        '<dat:dataPackItem version="2.0" '
        f'id="{xml_escape(str(row.get("id", "")))}">'
        '<inv:invoice version="2.0">'
        + "".join(h for h in header if h)
        + summary
        + "</inv:invoice></dat:dataPackItem>"
    )


def _voucher_item(row: dict) -> str:
    """Build a cash-voucher (Pokladna) dataPackItem for a received receipt.

    Pohoda's cash-register agenda uses the ``voucher.xsd`` schema (``vch:``),
    not ``inv:invoice``. A receipt we *received* (a paragon/účtenka from a
    shop = a purchase) is an expense cash voucher, so ``voucherType`` is
    always ``expense``. Mirrors ``_invoice_item`` conventions: ``vch:``
    header/summary wrapping ``typ:`` address and price elements.
    """
    vendor = row.get("vendor")
    ico = row.get("ico")

    date = _iso_date(row.get("invoice_date"))

    header = ["<vch:voucherHeader>"]
    header.append("<vch:voucherType>expense</vch:voucherType>")
    # NOTE: Pohoda's voucher schema expects a <vch:cashAccount> naming the
    # target cash register, but we don't know the user's register id, so we
    # omit it (like the empty POHODA_ICO above). On import the user may need
    # to pick/set the target cash register manually.
    if date:
        header.append(_el("vch:date", date))
    desc_vendor = vendor if vendor else "unknown vendor"
    header.append(_el("vch:text", f"Receipt from {desc_vendor}"))
    if vendor or ico:
        addr = ["<vch:partnerIdentity><typ:address>"]
        addr.append(_el("typ:company", vendor))
        addr.append(_el("typ:ico", ico))
        addr.append("</typ:address></vch:partnerIdentity>")
        header.append("".join(a for a in addr if a))
    header.append("</vch:voucherHeader>")

    subtotal = _amount(row.get("subtotal"))
    tax = _amount(row.get("tax"))
    total = _amount(row.get("total"))

    home = ["<vch:homeCurrency>"]
    if subtotal is not None and tax is not None:
        home.append(_el("typ:priceHigh", subtotal))
        home.append(_el("typ:priceHighVAT", tax))
        if total is not None:
            home.append(_el("typ:priceHighSum", total))
    elif total is not None:
        home.append(_el("typ:priceNone", total))
    home.append("</vch:homeCurrency>")

    summary = "<vch:voucherSummary>" + "".join(home) + "</vch:voucherSummary>"

    return (
        '<dat:dataPackItem version="2.0" '
        f'id="{xml_escape(str(row.get("id", "")))}">'
        '<vch:voucher version="2.0">'
        + "".join(h for h in header if h)
        + summary
        + "</vch:voucher></dat:dataPackItem>"
    )


def _signed_amount(value) -> tuple[str | None, bool]:
    """Normalize a signed transaction amount.

    Returns ``(abs_amount_str, is_expense)``: the magnitude as a plain numeric
    string and whether the original amount was negative (money leaving the
    account). ``(None, False)`` if the value can't be parsed.
    """
    is_expense = str(value).strip().startswith("-") if value is not None else False
    amt = _amount(value)
    if amt is None:
        return None, False
    # _amount preserves a leading '-'; strip it for the Pohoda magnitude.
    if amt.startswith("-"):
        is_expense = True
        amt = amt[1:]
    return amt, is_expense


def _bank_txn_item(statement: dict, txn: dict, index: int) -> str:
    """Build one <bnk:bank> dataPackItem for a single statement transaction.

    In Pohoda's Banka agenda each record is ONE movement (not a whole
    statement), classified as ``receipt`` (incoming) or ``expense`` (outgoing).
    We omit ``<bnk:account>`` because we don't know the user's Pohoda bank-account
    id — on import the user selects the target bank account.
    """
    amount, is_expense = _signed_amount(txn.get("amount"))
    bank_type = "expense" if is_expense else "receipt"

    sym_var = _digits(txn.get("variable_symbol"))
    date = _iso_date(txn.get("date"))
    counterparty = txn.get("counterparty_name")
    counterparty_acc = txn.get("counterparty_account")
    description = txn.get("description")

    header = ["<bnk:bankHeader>"]
    header.append(_el("bnk:bankType", bank_type))
    if statement.get("statement_number"):
        header.append(_el("bnk:statementNumber", statement.get("statement_number")))
    if date:
        header.append(_el("bnk:datePayment", date))
    if sym_var:
        header.append(_el("bnk:symVar", sym_var))
    if counterparty:
        addr = ["<bnk:partnerIdentity><typ:address>"]
        addr.append(_el("typ:company", counterparty))
        addr.append("</typ:address></bnk:partnerIdentity>")
        header.append("".join(a for a in addr if a))
    if counterparty_acc:
        header.append(_payment_account("bnk:paymentAccount", counterparty_acc))
    # Pohoda caps <bnk:text> at 96 chars.
    text = description or counterparty or "Bank transaction"
    header.append(_el("bnk:text", str(text)[:96]))
    header.append("</bnk:bankHeader>")

    summary = ""
    if amount is not None:
        summary = (
            "<bnk:bankSummary><bnk:homeCurrency>"
            f"{_el('typ:priceNone', amount)}"
            "</bnk:homeCurrency></bnk:bankSummary>"
        )

    item_id = f"{statement.get('id', '')}-{index}"
    return (
        f'<dat:dataPackItem version="2.0" id="{xml_escape(item_id)}">'
        '<bnk:bank version="2.0">'
        + "".join(h for h in header if h)
        + summary
        + "</bnk:bank></dat:dataPackItem>"
    )


def build_pohoda_bank_xml(statements, ico: str | None = None) -> str:
    """Build a Pohoda dataPack XML for one or more bank statements.

    Each statement's transactions are flattened into individual ``<bnk:bank>``
    records (Pohoda's Banka agenda is per-movement). Statements with no parsed
    transactions contribute nothing. ``ico`` is the target accounting unit's
    IČO for the whole package (a dataPack has exactly one); defaults to the
    first statement's own ``pohoda_ico`` when not given explicitly.
    """
    if isinstance(statements, dict):
        statements = [statements]
    if ico is None:
        ico = statements[0].get("pohoda_ico") if statements else ""
    parts = []
    for st in statements:
        for i, txn in enumerate(st.get("transactions") or []):
            parts.append(_bank_txn_item(st, txn, i))
    items = "".join(parts)
    header = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<dat:dataPack '
        'id="InvoiceParserBankExport" '
        f'{_ico_attr(ico)}'
        'application="InvoiceParser" '
        'version="2.0" '
        'note="Exported from Invoice Parser" '
        'xmlns:dat="http://www.stormware.cz/schema/version_2/data.xsd" '
        'xmlns:typ="http://www.stormware.cz/schema/version_2/type.xsd" '
        'xmlns:bnk="http://www.stormware.cz/schema/version_2/bank.xsd">'
    )
    return header + items + "</dat:dataPack>"


def build_pohoda_xml(rows, ico: str | None = None) -> str:
    """Build a Pohoda dataPack XML string for one or more invoice rows.

    ``ico`` is the target accounting unit's IČO for the whole package (a
    dataPack has exactly one); defaults to the first row's own ``pohoda_ico``
    when not given explicitly.
    """
    if isinstance(rows, dict):
        rows = [rows]
    if ico is None:
        ico = rows[0].get("pohoda_ico") if rows else ""
    # Route by document_type: receipts become cash vouchers (Pokladna),
    # everything else (invoices, or null/unknown) becomes received invoices.
    # A single dataPack may mix <inv:invoice> and <vch:voucher> items.
    items = "".join(
        _voucher_item(r) if r.get("document_type") == "receipt"
        else _invoice_item(r)
        for r in rows
    )
    header = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<dat:dataPack '
        'id="InvoiceParserExport" '
        f'{_ico_attr(ico)}'
        'application="InvoiceParser" '
        'version="2.0" '
        'note="Exported from Invoice Parser" '
        'xmlns:dat="http://www.stormware.cz/schema/version_2/data.xsd" '
        'xmlns:typ="http://www.stormware.cz/schema/version_2/type.xsd" '
        'xmlns:inv="http://www.stormware.cz/schema/version_2/invoice.xsd" '
        'xmlns:vch="http://www.stormware.cz/schema/version_2/voucher.xsd">'
    )
    return header + items + "</dat:dataPack>"
