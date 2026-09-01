"""Shared Pydantic models: the invoice field schema used for LLM structured
extraction and as the canonical field list across the app.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Extracted fields, in display order. Kept in one place so the DB schema,
# the LLM extraction schema, and the Pohoda export all agree.
FIELDS = [
    "document_type",
    "vendor",
    "ico",
    "customer",
    "customer_ico",
    "invoice_number",
    "variable_symbol",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
    "bank_account",
]


class InvoiceFields(BaseModel):
    """The structured output we ask the LLM to produce from invoice text.

    Every field is optional — the model returns null for anything it cannot
    find. Descriptions double as extraction instructions for the LLM.
    """

    # LLMs often emit amounts / IČO / VS as JSON numbers; coerce to str so a
    # value like 50000 or 27182734 validates instead of erroring.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    @field_validator("*", mode="before")
    @classmethod
    def _unwrap(cls, v):
        """Tolerate small-model quirks in structured output.

        Local models sometimes wrap each field as ``{"value": X}`` (or return a
        nested object). Unwrap a single ``value`` key; drop anything still
        structured to None so extraction never hard-fails on one odd field.
        """
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (list, dict)):
            return None
        return v

    document_type: Optional[str] = Field(
        None,
        description=(
            "The kind of document: 'invoice' (faktura / daňový doklad) or "
            "'receipt' (paragon / účtenka / pokladní doklad / simplified tax "
            "document). Use 'invoice' if you are unsure."
        ),
    )
    vendor: Optional[str] = Field(
        None,
        description=(
            "The supplier/vendor company name — who ISSUED the invoice "
            "(Czech 'Dodavatel'). This is NOT the customer."
        ),
    )
    ico: Optional[str] = Field(
        None,
        description=(
            "The VENDOR's Czech company registration number (labelled 'IČO' or "
            "'IČ' next to the Dodavatel), as a string of digits."
        ),
    )
    customer: Optional[str] = Field(
        None,
        description=(
            "The customer/buyer company name — who the invoice is billed TO "
            "(Czech 'Odběratel' / 'Objednatel'). This is NOT the vendor."
        ),
    )
    customer_ico: Optional[str] = Field(
        None,
        description=(
            "The CUSTOMER's Czech company registration number (the 'IČO' next "
            "to the Odběratel/Objednatel), as a string of digits."
        ),
    )
    invoice_number: Optional[str] = Field(
        None, description="The invoice number / document number."
    )
    variable_symbol: Optional[str] = Field(
        None,
        description=(
            "Czech 'variabilní symbol' / 'VS', the numeric payment reference, "
            "as a string of digits; often equals the invoice number without "
            "separators."
        ),
    )
    invoice_date: Optional[str] = Field(
        None, description="The invoice issue date, as written in the invoice."
    )
    due_date: Optional[str] = Field(
        None, description="The payment due date, as written in the invoice."
    )
    currency: Optional[str] = Field(
        None, description="Currency code or symbol (e.g. USD, EUR, CZK, Kč, $)."
    )
    subtotal: Optional[str] = Field(
        None,
        description="Amount before tax, numeric only, no currency symbol.",
    )
    tax: Optional[str] = Field(
        None, description="Tax/VAT amount, numeric only, no currency symbol."
    )
    total: Optional[str] = Field(
        None,
        description="Grand total to pay, numeric only, no currency symbol.",
    )
    bank_account: Optional[str] = Field(
        None,
        description=(
            "Vendor's bank account number or IBAN for payment, as written "
            "(may include a bank code, e.g. '123456789/0100'). If both a "
            "domestic account number and an IBAN are shown, prefer the "
            "domestic account number; if only an IBAN is present, use it. "
            "This is ONLY the account identifier — a short token of digits, "
            "slashes and spaces. Do NOT put amounts, dates, addresses, "
            "IČO/DIČ, payment-method words ('Převodem'), or any other text "
            "here."
        ),
    )


# --- Bank statements -------------------------------------------------------

# Statement-level (header) fields, in display order. One bank statement = one
# document with many transactions; these are the scalar fields that describe
# the statement as a whole (the transactions live in ``transactions``).
BANK_STATEMENT_FIELDS = [
    "account_number",
    "statement_number",
    "currency",
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
]

# Per-transaction fields, in display order.
BANK_TRANSACTION_FIELDS = [
    "date",
    "counterparty_name",
    "counterparty_account",
    "variable_symbol",
    "description",
    "amount",
    "balance_after",
]


class BankTransaction(BaseModel):
    """One movement (line) on a bank statement.

    ``amount`` is signed: negative for money leaving the account (debit),
    positive for money arriving (credit). Every field is optional — the model
    returns null for anything a given statement doesn't show.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    @field_validator("*", mode="before")
    @classmethod
    def _unwrap(cls, v):
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (list, dict)):
            return None
        return v

    date: Optional[str] = Field(
        None,
        description=(
            "Transaction date as written (the booking date / 'datum "
            "zaúčtování' / 'datum uskutečnění')."
        ),
    )
    counterparty_name: Optional[str] = Field(
        None,
        description=(
            "The other party's name (the payer for an incoming payment, the "
            "payee for an outgoing one). Null if not shown."
        ),
    )
    counterparty_account: Optional[str] = Field(
        None,
        description=(
            "The other party's bank account number or IBAN, as written (may "
            "include a bank code, e.g. '670100-2219931431/6210'). This is ONLY "
            "the account identifier — never an amount, date or description."
        ),
    )
    variable_symbol: Optional[str] = Field(
        None,
        description=(
            "The payment's variable symbol ('variabilní symbol' / 'VS'), a "
            "numeric reference, if shown for this transaction."
        ),
    )
    description: Optional[str] = Field(
        None,
        description=(
            "The transaction description / narrative ('popis transakce' / "
            "'zpráva pro příjemce'), e.g. 'PŘÍCHOZÍ PLATBA — TRANSFER'."
        ),
    )
    amount: Optional[str] = Field(
        None,
        description=(
            "The transaction amount, numeric only, no currency symbol. SIGNED: "
            "negative when money LEAVES the account (a debit / outgoing / "
            "'odchozí' payment), positive when money ARRIVES (a credit / "
            "incoming / 'příchozí' payment). Preserve the sign shown."
        ),
    )
    balance_after: Optional[str] = Field(
        None,
        description=(
            "The account balance after this transaction ('účetní zůstatek po "
            "transakci'), numeric only, if shown."
        ),
    )


class BankStatement(BaseModel):
    """A parsed bank statement: header fields plus a list of transactions.

    Descriptions double as extraction instructions for the LLM. Every field is
    optional; ``transactions`` is one entry per movement on the statement.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    @field_validator(
        "account_number", "statement_number", "currency", "period_start",
        "period_end", "opening_balance", "closing_balance", mode="before",
    )
    @classmethod
    def _unwrap(cls, v):
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (list, dict)):
            return None
        return v

    account_number: Optional[str] = Field(
        None,
        description=(
            "The statement's OWN account number or IBAN (the account this "
            "statement is for — 'číslo účtu'), as written."
        ),
    )
    statement_number: Optional[str] = Field(
        None,
        description=(
            "The statement's number/identifier ('číslo výpisu') if shown; "
            "otherwise null."
        ),
    )
    currency: Optional[str] = Field(
        None, description="Account currency code (e.g. CZK, EUR)."
    )
    period_start: Optional[str] = Field(
        None,
        description="Start of the statement period ('za období' from-date).",
    )
    period_end: Optional[str] = Field(
        None,
        description="End of the statement period ('za období' to-date).",
    )
    opening_balance: Optional[str] = Field(
        None,
        description=(
            "Opening balance ('počáteční zůstatek'), numeric only, no currency "
            "symbol. May be signed."
        ),
    )
    closing_balance: Optional[str] = Field(
        None,
        description=(
            "Closing balance ('konečný zůstatek'), numeric only, no currency "
            "symbol. May be signed."
        ),
    )
    transactions: List[BankTransaction] = Field(
        default_factory=list,
        description=(
            "One entry per movement/line on the statement, in the order they "
            "appear. Include every transaction row; do not include summary "
            "totals or the opening/closing balance lines as transactions."
        ),
    )


class DocumentKind(BaseModel):
    """Classifier output: which extraction path a document needs."""

    kind: str = Field(
        ...,
        description=(
            "The document kind: 'bank_statement' for a bank account statement "
            "(výpis z účtu — a list of transactions on one account with an "
            "opening/closing balance), or 'invoice' for anything else (a "
            "single invoice/faktura, receipt/účtenka, or bill)."
        ),
    )


class InvoiceExtraction(BaseModel):
    """Wrapper so one PDF can yield several invoices.

    A single PDF usually contains exactly one invoice, but occasionally it
    bundles several distinct invoices (e.g. a scanned batch). The model returns
    one ``InvoiceFields`` entry per distinct document.
    """

    invoices: List[InvoiceFields] = Field(
        default_factory=list,
        description=(
            "One entry per distinct invoice/receipt in the document. Return "
            "exactly one entry for a normal single-invoice document; return "
            "multiple entries ONLY when the document clearly contains several "
            "separate invoices (different invoice numbers / vendors / totals)."
        ),
    )
