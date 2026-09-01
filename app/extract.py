"""Invoice extraction pipeline: PDF bytes -> plain text -> structured fields.

The text step uses PyMuPDF (no OCR — scanned/image PDFs yield no text). The
field step asks the configured LLM for structured output matching
``InvoiceFields``. Evals and the app both call ``extract_fields_from_text`` so
they exercise the exact same prompt + model path.
"""
import base64
import re
from dataclasses import dataclass

import fitz  # PyMuPDF
import pdf_inspector
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate

from . import bank_parsers
from .config import settings
from .llm import active_model_name, get_chat_model, get_vision_chat_model
from .schemas import (
    BANK_STATEMENT_FIELDS,
    BANK_TRANSACTION_FIELDS,
    FIELDS,
    BankStatement,
    DocumentKind,
    InvoiceExtraction,
)

# Resolution for rasterizing scanned/image PDF pages before sending them to the
# vision model. 200 DPI keeps small print legible (~1650px on an A4 long edge)
# without producing needlessly huge images. The long edge is additionally
# capped at _MAX_EDGE_PX so a large page can't blow up the token count.
_RENDER_DPI = 200
_MAX_EDGE_PX = 2200

# Canonicalize currency to a single ISO code so "Kč"/"Kc"/"CZK" don't appear as
# three different currencies for the same thing.
_CURRENCY_MAP = {
    "kč": "CZK", "kc": "CZK", "kčs": "CZK", "kcs": "CZK", "czk": "CZK",
    "korun": "CZK", "kč.": "CZK",
    "€": "EUR", "eur": "EUR",
    "$": "USD", "usd": "USD", "us$": "USD", "usd$": "USD",
    "£": "GBP", "gbp": "GBP",
    "zł": "PLN", "zl": "PLN", "pln": "PLN",
    "chf": "CHF", "huf": "HUF", "ft": "HUF",
}


def _canonical_currency(v):
    """Map a written currency (symbol/code/word) to an ISO 4217 code."""
    if not v:
        return v
    s = str(v).strip()
    key = s.lower().rstrip(".")
    if key in _CURRENCY_MAP:
        return _CURRENCY_MAP[key]
    letters = re.sub(r"[^a-zA-Z]", "", s)
    if len(letters) == 3:  # already a 3-letter code like SEK/NOK/JPY
        return letters.upper()
    return s

SYSTEM_PROMPT = (
    "You are an invoice data extraction tool. Extract the requested fields "
    "from the document text and return them as structured data. The document "
    "may be in English or Czech.\n"
    "Rules:\n"
    "- Use null for any field you cannot find. Do not guess.\n"
    "- Every field is a single scalar value (a string or number), never an "
    "object.\n"
    "- Each field holds ONLY its own single value. NEVER concatenate several "
    "values into one field. In particular `bank_account` is ONLY the bank "
    "account number or IBAN (a short digits/slashes/spaces token) — never "
    "amounts, dates, addresses, IČO, DIČ, payment-method words, or the rest "
    "of the document.\n"
    "- document_type: 'invoice' for a faktura / daňový doklad, 'receipt' for a "
    "paragon / účtenka / pokladní doklad / simplified tax document. Use "
    "'invoice' if unsure.\n"
    "- Dates: return exactly as written in the invoice (do not reformat).\n"
    "- currency: a code or symbol (e.g. USD, EUR, CZK, Kč, $).\n"
    "- Amounts (subtotal, tax, total): digits only, no currency symbol or "
    "thousands separators.\n"
    "- subtotal is the amount BEFORE tax (the net / 'základ daně' / 'celkem "
    "bez DPH').\n"
    "- tax is the VAT amount only (the 'DPH' line).\n"
    "- total is the final amount due AFTER tax (the gross / 'celkem k úhradě' "
    "/ 'k úhradě' / amount with VAT included).\n"
    "- subtotal and total are DIFFERENT numbers whenever tax is non-zero: "
    "total = subtotal + tax. Never copy total into subtotal or subtotal into "
    "total. If you see one smaller (net) and one larger (gross) amount, the "
    "smaller is subtotal and the larger is total.\n"
    "- There are TWO parties. The vendor/supplier (Czech 'Dodavatel') ISSUED "
    "the invoice: put its name in `vendor`, its IČO in `ico`, its bank account "
    "in `bank_account`. The customer/buyer (Czech 'Odběratel' / 'Objednatel') "
    "is billed: put its name in `customer` and its IČO in `customer_ico`. "
    "Never swap these two parties.\n"
    "- variable_symbol (Czech 'variabilní symbol' / 'VS') is the numeric "
    "payment reference, distinct from the invoice number.\n"
    "- Return one entry in `invoices` per distinct invoice. Almost always this "
    "is a single entry; only return multiple entries when the text clearly "
    "holds several separate invoices with different invoice numbers or "
    "vendors."
)

_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("human", 'Invoice text:\n"""\n{text}\n"""'),
    ]
)

# Document classifier — decides which extraction path a PDF needs. Kept tiny and
# cheap: it only picks a kind, it does not extract any fields.
_CLASSIFY_SYSTEM_PROMPT = (
    "You are a document classifier. Decide whether the given document text is a "
    "BANK STATEMENT or an INVOICE-like document.\n"
    "- 'bank_statement': a bank ACCOUNT statement (Czech 'výpis z účtu') — it "
    "lists many transactions/movements on ONE account over a period, with an "
    "opening and closing balance ('počáteční/konečný zůstatek'). Issued BY a "
    "bank.\n"
    "- 'invoice': anything else — a single invoice/faktura, a receipt/účtenka/"
    "paragon, or a bill. A payment request for one purchase, not a list of "
    "account movements.\n"
    "When unsure, answer 'invoice'."
)
_CLASSIFY_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", _CLASSIFY_SYSTEM_PROMPT),
        ("human", 'Document text:\n"""\n{text}\n"""'),
    ]
)

BANK_SYSTEM_PROMPT = (
    "You are a bank-statement data extraction tool. Extract the statement's "
    "header fields and EVERY transaction from the statement text. The text may "
    "be in Czech or English.\n"
    "Rules:\n"
    "- Use null for any field you cannot find. Do not guess.\n"
    "- account_number is the statement's OWN account (Czech 'číslo účtu'), not "
    "a counterparty account.\n"
    "- Dates: return exactly as written (do not reformat).\n"
    "- Amounts (opening_balance, closing_balance, transaction amount, "
    "balance_after): digits only, no currency symbol or thousands separators.\n"
    "- Each transaction's `amount` is SIGNED: negative when money LEAVES the "
    "account (debit / 'odchozí'), positive when money ARRIVES (credit / "
    "'příchozí'). Keep the sign.\n"
    "- counterparty_name / counterparty_account describe the OTHER party, not "
    "the statement's own account.\n"
    "- Return one entry in `transactions` for EVERY movement line, in order. Do "
    "NOT turn summary totals or the opening/closing balance lines into "
    "transactions."
)
_BANK_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", BANK_SYSTEM_PROMPT),
        ("human", 'Bank statement text:\n"""\n{text}\n"""'),
    ]
)

# Vision path = OCR only. We do NOT ask the vision model to produce structured
# fields directly — small local vision models (e.g. qwen2.5vl) transcribe a page
# competently but are unreliable at field-splitting (they leave amounts/dates
# null or dump everything into one field). So the vision model just transcribes
# each page to plain text, and that text goes through the SAME proven text
# extraction path used for text PDFs.
_OCR_SYSTEM_PROMPT = (
    "You are an OCR engine. Transcribe the text of the document image exactly "
    "as it appears."
)
_OCR_HUMAN_PROMPT = (
    "This is a scanned page of an invoice or receipt (Czech or English). "
    "Transcribe ALL visible text verbatim — every label, name, number, date "
    "and amount — preserving line breaks and reading order. Output only the "
    "transcribed text, with no commentary, headings or explanation."
)


class PdfTextError(Exception):
    """Raised when a PDF cannot be read or has no extractable text."""


def extract_text_pages(pdf_bytes: bytes) -> list[str]:
    """Extract text for each PDF page. Raises PdfTextError if none is found."""
    try:
        pages = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pages.append(page.get_text())
    except Exception as exc:  # malformed / not a PDF
        raise PdfTextError(f"Could not read PDF: {exc}") from exc

    if not any(p.strip() for p in pages):
        raise PdfTextError(
            "No extractable text found (looks like a scanned/image PDF). "
            "OCR is not supported yet."
        )
    return pages


def extract_text(pdf_bytes: bytes) -> str:
    """Extract all plain text from a PDF as one string."""
    return "\n".join(extract_text_pages(pdf_bytes)).strip()


def _page_png(page) -> bytes:
    """Rasterize one page to PNG at _RENDER_DPI, long edge capped."""
    # Zoom for the target DPI, but clamp so the long edge stays under
    # _MAX_EDGE_PX (keeps the image legible yet token-bounded).
    zoom = _RENDER_DPI / 72.0
    long_edge_pt = max(page.rect.width, page.rect.height) or 1
    zoom = min(zoom, _MAX_EDGE_PX / long_edge_pt)
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


def render_pages_to_images(pdf_bytes: bytes) -> list[bytes]:
    """Rasterize each PDF page to a PNG (one per page) for the vision model.

    Used only for scanned/image PDFs that have no extractable text. Raises
    PdfTextError if the PDF can't be opened.
    """
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            images = [_page_png(page) for page in doc]
    except Exception as exc:  # malformed / not a PDF
        raise PdfTextError(f"Could not render PDF pages: {exc}") from exc
    return images


def _render_first_page(pdf_bytes: bytes) -> bytes | None:
    """Rasterize only page 1, or None for a PDF with no pages.

    Rendering the whole document to reach its first page would cost a pixmap per
    page on a statement that can run to hundreds of them.
    """
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return _page_png(doc[0]) if doc.page_count else None
    except Exception as exc:  # malformed / not a PDF
        raise PdfTextError(f"Could not render PDF pages: {exc}") from exc


def _normalize(data: dict) -> dict:
    """Coerce one model_dump()'d InvoiceFields into a clean FIELDS-only dict."""
    normalized = {}
    for f in FIELDS:
        v = data.get(f)
        if isinstance(v, str):
            v = v.strip() or None
        if v in ("", "null", "None"):
            v = None
        if f == "currency":
            v = _canonical_currency(v)
        normalized[f] = v
    return normalized


def extract_documents_from_text(text: str) -> list[dict]:
    """Run the LLM structured extraction over document text.

    Returns a list of dicts (one per invoice found in the document); each dict
    has exactly the keys in FIELDS with missing/empty values as None. A normal
    single-invoice document yields a one-element list.
    """
    model = get_chat_model().with_structured_output(InvoiceExtraction)
    chain = _PROMPT | model
    result: InvoiceExtraction = chain.invoke({"text": text})

    docs = [_normalize(inv.model_dump()) for inv in (result.invoices or [])]
    # Never return an empty list — an empty extraction is still a stored row
    # (all fields None) so the user sees the file and can correct it.
    return docs or [{f: None for f in FIELDS}]


def extract_fields_from_text(text: str) -> dict:
    """Single-invoice convenience wrapper (used by the evals/tests).

    Returns the first extracted invoice as a dict with exactly the keys in
    FIELDS; missing/empty -> None.
    """
    return extract_documents_from_text(text)[0]


# Asks for the same structured output as _PROMPT, but over the PDF itself
# rather than a text dump. The rules stay in SYSTEM_PROMPT so both paths extract
# by identical instructions.
_INVOICE_DOCUMENT_PROMPT = (
    "Extract the fields of every invoice in this PDF, then return them with "
    "the record_invoices tool. A PDF that bundles several separate invoices "
    "(different invoice numbers or vendors) must produce one entry per "
    "invoice; pages that merely continue the previous invoice do not start a "
    "new entry."
)

# A handful of invoices with all their fields serializes to a few thousand
# tokens of tool input at most, so this ceiling is generous even for a bundled
# batch while staying small enough for a plain non-streaming call.
_INVOICE_DOCUMENT_MAX_TOKENS = 8192

_INVOICE_DOCUMENT_TOOL = "record_invoices"


def extract_invoices_via_haiku_document(pdf_bytes: bytes) -> list[dict]:
    """Extract invoices by sending the PDF itself to Claude.

    Returns the same shape as ``extract_documents_from_text`` — one dict per
    invoice, each with exactly the FIELDS keys and empty values as None.

    Anthropic-only: the PDF goes up as a native ``document`` block, so the model
    sees the original layout (which column an IČO sits under, which party a bank
    account belongs to) instead of the flattened text stream PyMuPDF produces,
    and scans need no separate OCR pass. One call covers every page, so there is
    no per-page merge to fold continuation pages in. The response is forced
    through a tool call whose schema is ``InvoiceExtraction`` itself, which is
    what makes the output structured without LangChain.
    """
    import anthropic  # lazy: only the Anthropic path needs the SDK

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=active_model_name(),
        max_tokens=_INVOICE_DOCUMENT_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "name": _INVOICE_DOCUMENT_TOOL,
                "description": (
                    "Record the extracted fields of every invoice or receipt "
                    "in the document."
                ),
                "input_schema": InvoiceExtraction.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": _INVOICE_DOCUMENT_TOOL},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _INVOICE_DOCUMENT_PROMPT},
                ],
            }
        ],
    )

    block = next((b for b in message.content if b.type == "tool_use"), None)
    if block is None:
        raise PdfTextError("The model returned no invoice for this PDF.")
    result = InvoiceExtraction.model_validate(block.input)
    docs = [_normalize(inv.model_dump()) for inv in (result.invoices or [])]
    # Never return an empty list — an empty extraction is still a stored row
    # (all fields None) so the user sees the file and can correct it.
    return docs or [{f: None for f in FIELDS}]


# How much of the document text to hand the classifier. The kind is obvious from
# the first page (headings, 'výpis z účtu' vs 'faktura'), so a prefix keeps the
# classification call cheap.
_CLASSIFY_MAX_CHARS = 3000


def classify_document(text: str) -> str:
    """Classify document text as 'bank_statement' or 'invoice' via the LLM.

    Falls back to 'invoice' (the default path) on any unexpected value so a
    classifier hiccup can never strand a normal invoice.
    """
    model = get_chat_model().with_structured_output(DocumentKind)
    chain = _CLASSIFY_PROMPT | model
    result: DocumentKind = chain.invoke({"text": text[:_CLASSIFY_MAX_CHARS]})
    return "bank_statement" if result.kind == "bank_statement" else "invoice"


def _normalize_bank(statement: BankStatement) -> dict:
    """Coerce a BankStatement into a clean dict: header fields + transactions.

    Header keys are exactly BANK_STATEMENT_FIELDS; each transaction is a dict
    with exactly BANK_TRANSACTION_FIELDS. Empty strings become None; currency is
    canonicalized like the invoice path.
    """
    data = statement.model_dump()

    def _clean(v):
        if isinstance(v, str):
            v = v.strip() or None
        return None if v in ("", "null", "None") else v

    header = {}
    for f in BANK_STATEMENT_FIELDS:
        v = _clean(data.get(f))
        header[f] = _canonical_currency(v) if f == "currency" else v

    txns = []
    for t in data.get("transactions") or []:
        txns.append({f: _clean(t.get(f)) for f in BANK_TRANSACTION_FIELDS})
    header["transactions"] = txns
    return header


def extract_bank_statement_from_text(text: str) -> dict:
    """Run the LLM structured extraction for a bank statement over its text.

    Returns a dict with the keys in BANK_STATEMENT_FIELDS plus a
    ``transactions`` list (each a BANK_TRANSACTION_FIELDS dict).
    """
    model = get_chat_model().with_structured_output(BankStatement)
    chain = _BANK_PROMPT | model
    result: BankStatement = chain.invoke({"text": text})
    return _normalize_bank(result)


# Asks for the same structured output as _BANK_PROMPT, but over the PDF itself
# rather than a text dump. The rules stay in BANK_SYSTEM_PROMPT so both paths
# extract by identical instructions.
_BANK_DOCUMENT_PROMPT = (
    "Extract this bank statement's header fields and EVERY transaction, then "
    "return them with the record_bank_statement tool. Never omit, merge or "
    "summarize movements: a statement listing 200 of them must produce 200 "
    "entries."
)

# Output ceiling for the one-shot document call: a 200+ movement statement
# serializes to tens of thousands of tokens of tool input, so this sits at the
# model's maximum. The call is streamed because the SDK rejects non-streaming
# requests with a budget this large (they outlive the HTTP timeout).
_BANK_DOCUMENT_MAX_TOKENS = 64000

_BANK_DOCUMENT_TOOL = "record_bank_statement"


def extract_bank_statement_via_haiku_document(pdf_bytes: bytes) -> dict:
    """Extract a bank statement by sending the PDF itself to Claude.

    Returns the same shape as ``extract_bank_statement_from_text`` (the
    BANK_STATEMENT_FIELDS keys plus ``transactions``).

    Anthropic-only: the PDF goes up as a native ``document`` block, so the model
    sees the original page layout — columns, table cells, the alignment the
    coordinate parsers in bank_parsers exist to recover — instead of the
    flattened text stream PyMuPDF produces. One call covers the whole document,
    so there is no per-chunk merge to lose header fields in. The response is
    forced through a tool call whose schema is ``BankStatement`` itself, which
    is what makes the output structured without LangChain.
    """
    import anthropic  # lazy: only the Anthropic path needs the SDK

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    with client.messages.stream(
        model=active_model_name(),
        max_tokens=_BANK_DOCUMENT_MAX_TOKENS,
        system=BANK_SYSTEM_PROMPT,
        tools=[
            {
                "name": _BANK_DOCUMENT_TOOL,
                "description": (
                    "Record a bank statement's header fields and every "
                    "transaction on it."
                ),
                "input_schema": BankStatement.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": _BANK_DOCUMENT_TOOL},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _BANK_DOCUMENT_PROMPT},
                ],
            }
        ],
    ) as stream:
        message = stream.get_final_message()

    block = next((b for b in message.content if b.type == "tool_use"), None)
    if block is None:
        raise PdfTextError("The model returned no bank statement for this PDF.")
    return _normalize_bank(BankStatement.model_validate(block.input))


def _merge_bank_pages(page_statements: list[dict]) -> dict:
    """Merge per-page bank-statement extractions into one statement.

    Header fields (account, period, balances…) are taken from the first page
    that provides each — page 1 normally carries the summary — and every page's
    transactions are concatenated in document order. This mirrors the per-page
    approach used for invoices: a long statement spans many pages, and sending
    all of them in one LLM call overflows small context windows (and is
    unreliable even on large ones), so each page is extracted on its own.
    """
    merged = {f: None for f in BANK_STATEMENT_FIELDS}
    merged["transactions"] = []
    for st in page_statements:
        for f in BANK_STATEMENT_FIELDS:
            if merged[f] is None and st.get(f) is not None:
                merged[f] = st[f]
        merged["transactions"].extend(st.get("transactions") or [])
    return merged


# Pages per LLM call when extracting a bank statement. Grouping a few pages cuts
# the number of calls (and repeated system-prompt overhead / latency) versus one
# call per page, while staying well under the model's output-token limit — a
# single call over a whole long statement risks truncating the transaction list.
BANK_PAGE_CHUNK = 5


def _page_chunks(pages: list[str], size: int = BANK_PAGE_CHUNK) -> list[list[str]]:
    return [pages[i:i + size] for i in range(0, len(pages), size)]


def iter_bank_statement_chunks(pages: list[str], size: int = BANK_PAGE_CHUNK):
    """Yield one extracted statement dict per chunk of pages, in order.

    Each yielded dict has the BANK_STATEMENT_FIELDS keys plus ``transactions``
    for just that chunk. Callers merge headers (first non-null wins) and
    concatenate transactions — this is what lets the upload endpoint save and
    stream results chunk by chunk instead of only at the very end.
    """
    for chunk in _page_chunks(pages, size):
        yield extract_bank_statement_from_text("\n".join(chunk))


def extract_bank_statement_from_pages(pages: list[str]) -> dict:
    """Extract a bank statement from per-page text, merging chunk results.

    Single-page statements are extracted directly; longer ones are extracted in
    chunks of BANK_PAGE_CHUNK pages and merged (header from the first chunk that
    has it, all transactions concatenated).
    """
    if len(pages) <= 1:
        return extract_bank_statement_from_text(pages[0] if pages else "")
    return _merge_bank_pages(list(iter_bank_statement_chunks(pages)))


def _content_to_text(content) -> str:
    """Flatten a chat message's content into plain text.

    Providers return either a string or a list of content blocks; keep only the
    text of each block so we get one transcript string regardless of provider.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def transcribe_image(image: bytes) -> str:
    """OCR one page image to plain text via the vision model.

    Sends the PNG as a base64 data URI and asks the model only to transcribe —
    the structured field extraction is done afterwards by the text path.
    """
    b64 = base64.b64encode(image).decode("ascii")
    content = [
        {"type": "text", "text": _OCR_HUMAN_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    model = get_vision_chat_model()
    result = model.invoke(
        [SystemMessage(content=_OCR_SYSTEM_PROMPT), HumanMessage(content=content)]
    )
    return _content_to_text(result.content).strip()


# Fields that mark the START of a new invoice. A page carrying none of these
# (e.g. only spilled line items or a lone total) is treated as a
# continuation/terms page, not a separate invoice.
_IDENTITY_FIELDS = ("vendor", "invoice_number")


def _is_continuation(doc: dict) -> bool:
    return not any(doc.get(f) for f in _IDENTITY_FIELDS)


def _dedup_key(doc: dict):
    """Identity for de-duplicating the same invoice seen on two pages."""
    inv = (doc.get("invoice_number") or "").strip().lower()
    if inv:
        return ("inv", inv)
    return ("vt", (doc.get("vendor") or "").strip().lower(),
            (doc.get("total") or "").strip())


def _merge_pages(page_docs: list[list[dict]]) -> list[dict]:
    """Merge per-page extraction results into the final invoice list.

    Pages with no identifying fields (continuation/terms pages) are folded into
    the previous invoice rather than becoming phantom rows, and the same invoice
    appearing on two pages is de-duplicated.
    """
    docs: list[dict] = []
    seen = set()
    for docs_on_page in page_docs:
        for doc in docs_on_page:
            if _is_continuation(doc):
                # Spilled content (e.g. a total on page 2 of one invoice):
                # fold its fields into the previous invoice. Only fill fields
                # that are still missing.
                if docs:
                    prev = docs[-1]
                    for f in FIELDS:
                        if prev.get(f) is None and doc.get(f) is not None:
                            prev[f] = doc[f]
                continue
            key = _dedup_key(doc)
            if key in seen:
                continue
            seen.add(key)
            docs.append(doc)
    return docs or [{f: None for f in FIELDS}]


def extract_invoice_from_images(pdf_bytes: bytes) -> list[dict]:
    """Vision fallback for scanned/image PDFs (no extractable text).

    OCR-first: rasterize each page, transcribe it to text with the vision
    model, then run the SAME text extraction path used for text PDFs. This
    keeps field-splitting on the proven text model and asks the weak local
    vision model only to read text. Like the text path it works **page by
    page** and merges (multi-invoice + continuation aware).
    """
    images = render_pages_to_images(pdf_bytes)
    if not images:
        raise PdfTextError("PDF has no pages to render.")
    pages = [t for t in (transcribe_image(img) for img in images) if t.strip()]
    if not pages:
        raise PdfTextError(
            "Vision transcription produced no text from the scanned PDF."
        )
    if len(pages) == 1:
        return extract_documents_from_text(pages[0])
    return _merge_pages([extract_documents_from_text(p) for p in pages])


# Below this classification confidence pdf-inspector is not sure what it is
# looking at, so every page is sent to OCR rather than trusting the per-page
# verdict. Genuinely mixed documents score ~0.6, which stays above the floor.
_MIN_CLASSIFY_CONFIDENCE = 0.5


def _pages_needing_ocr(pdf_bytes: bytes) -> set[int]:
    """0-indexed pages whose embedded text pdf-inspector says not to trust.

    This is a structural verdict (broken font encodings, image-only content,
    text that is really a scan behind a thin text layer), which catches pages
    that PyMuPDF still returns *some* string for and a blank-check would wave
    through. Returning an empty set on failure degrades to exactly the old
    blank-string behavior — a classifier hiccup must never cost more than it
    saves.
    """
    try:
        result = pdf_inspector.classify_pdf_bytes(pdf_bytes)
    except Exception:
        return set()
    if result.confidence < _MIN_CLASSIFY_CONFIDENCE:
        return set(range(result.page_count))
    return set(result.pages_needing_ocr)


def _pdf_to_pages(pdf_bytes: bytes) -> list[str]:
    """Return non-empty per-page text for a PDF, OCR'ing scans as needed.

    Cheap path first: pull each page's embedded text. pdf-inspector decides per
    page whether that text is trustworthy; pages it flags — plus any page that
    came back blank — are transcribed individually via the pricier vision path.
    Per page, because a PDF can mix a digital cover page with scanned body pages
    and those scans would otherwise be dropped without a trace. Raises
    PdfTextError if even the vision path yields nothing.
    """
    try:
        raw = extract_text_pages(pdf_bytes)
    except PdfTextError:
        raw = None  # every page is blank: page count comes from the renderer

    needs_ocr = _pages_needing_ocr(pdf_bytes)

    if raw is None or needs_ocr or not all(p.strip() for p in raw):
        images = render_pages_to_images(pdf_bytes)
        if not images:
            raise PdfTextError("PDF has no pages to render.")
        if raw is None:
            raw = [""] * len(images)
        raw = [
            transcribe_image(img) if (i in needs_ocr or not t.strip()) else t
            for i, (t, img) in enumerate(zip(raw, images))
        ]

    pages = [p for p in raw if p.strip()]
    if not pages:
        raise PdfTextError(
            "Vision transcription produced no text from the scanned PDF."
        )
    return pages


def _invoices_from_pages(pages: list[str]) -> list[dict]:
    """Extract invoices from already-resolved per-page text.

    Multi-page PDFs are extracted **page by page** — small local models tend to
    return only one invoice when several pages are concatenated into a single
    blob, so per-page extraction is what surfaces every invoice.
    """
    if len(pages) <= 1:
        return extract_documents_from_text(pages[0] if pages else "")
    return _merge_pages([extract_documents_from_text(p) for p in pages])


def extract_invoice(pdf_bytes: bytes) -> list[dict]:
    """Full invoice pipeline: PDF bytes -> list of extracted invoices.

    Usually returns a one-element list; longer when the PDF bundles several
    invoices.
    """
    try:
        pages = _pdf_to_pages(pdf_bytes)
    except PdfTextError:
        # Preserve the historical "no extractable text" behavior for callers
        # that expect the image path to raise a descriptive PdfTextError.
        raise
    return _invoices_from_pages(pages)


def pdf_to_pages(pdf_bytes: bytes) -> list[str]:
    """Public wrapper: PDF bytes -> non-empty per-page text (OCR for scans).

    Raises PdfTextError if the PDF yields no text at all. Used by the streaming
    upload endpoint, which drives classification and chunked extraction itself.
    """
    return _pdf_to_pages(pdf_bytes)


def invoices_from_pages(pages: list[str]) -> list[dict]:
    """Public wrapper around the per-page invoice extraction + merge."""
    return _invoices_from_pages(pages)


def _llm_bank_statement(pdf_bytes: bytes, pages: list[str] | None) -> dict:
    """LLM fallback for a statement no deterministic parser recognized.

    Anthropic reads the original PDF in one call; every other provider gets the
    per-page text in chunks, since the native document block has no equivalent
    elsewhere and can't be faked.
    """
    if settings.llm_provider == "anthropic":
        return extract_bank_statement_via_haiku_document(pdf_bytes)
    return extract_bank_statement_from_pages(pages or [])


def _llm_invoices(pdf_bytes: bytes, pages: list[str] | None) -> list[dict]:
    """LLM invoice extraction over whichever input the provider accepts.

    Same split as _llm_bank_statement: the PDF itself for Anthropic, per-page
    text for everyone else.
    """
    if settings.llm_provider == "anthropic":
        return extract_invoices_via_haiku_document(pdf_bytes)
    return invoices_from_pages(pages or [])


@dataclass(frozen=True)
class DocumentPlan:
    """What a PDF is, and how much of it had to be read to find out.

    ``pages`` holds the per-page text when the document was read in full, and is
    None when it was left for Claude to read natively — in which case there is
    no text layer to hand a deterministic parser and ``full_text`` is empty.
    """

    kind: str
    pages: list[str] | None
    full_text: str


def _classification_text(pdf_bytes: bytes, needs_ocr: set[int]) -> str:
    """Text of page 1 only, OCR'd just for it if it is itself a scan.

    The kind is obvious from the first page (see _CLASSIFY_MAX_CHARS), so this
    bounds the cost of classifying a scan at a single vision call — and at none
    at all when page 1 carries real embedded text.
    """
    try:
        raw = extract_text_pages(pdf_bytes)
    except PdfTextError:  # every page is blank
        raw = []
    text = raw[0] if raw else ""
    if not text.strip() or 0 in needs_ocr:
        image = _render_first_page(pdf_bytes)
        if image is not None:
            text = transcribe_image(image)
    return text


def _classify(text: str) -> str:
    """Cheap keyword verdict first, LLM classifier only if it says no."""
    if bank_parsers.looks_like_bank_statement(text):
        return "bank_statement"
    try:
        return classify_document(text)
    except Exception:
        return "invoice"


def plan_document(pdf_bytes: bytes) -> DocumentPlan:
    """Classify a PDF, reading as little of it as the provider requires.

    A text PDF is read in full: its text is free, and it feeds both the
    deterministic bank parsers and the classifier. A PDF with scanned pages
    costs one vision call per page to read that way, and on Anthropic that
    spend buys nothing — the coordinate parsers need an embedded text layer a
    scan does not have, and the native document calls read the scan themselves
    from the original bytes. So there only page 1 is read, purely to classify,
    and the pages are left unread.

    Raises PdfTextError if the PDF yields no text at all.
    """
    if settings.llm_provider == "anthropic":
        needs_ocr = _pages_needing_ocr(pdf_bytes)
        if needs_ocr:
            return DocumentPlan(
                kind=_classify(_classification_text(pdf_bytes, needs_ocr)),
                pages=None,
                full_text="",
            )
    pages = pdf_to_pages(pdf_bytes)
    full_text = "\n".join(pages).strip()
    return DocumentPlan(kind=_classify(full_text), pages=pages, full_text=full_text)


def extract_document(pdf_bytes: bytes) -> dict:
    """Classify a PDF, then extract with the matching path.

    Returns either
      {"kind": "invoice", "invoices": [<invoice dict>, ...]}
    or
      {"kind": "bank_statement", "statement": <statement dict>}.
    """
    plan = plan_document(pdf_bytes)  # may raise PdfTextError for empty scans

    # Deterministic path first: an unambiguous statement from a known bank is
    # parsed by coordinates (reliable fields, no LLM). Fall back to the LLM only
    # when the bank is unrecognized or the parser finds nothing. It reads the
    # PDF's own text coordinates, so it has nothing to work with unless the
    # document was read as text.
    if plan.pages is not None and bank_parsers.looks_like_bank_statement(
        plan.full_text
    ):
        parsed = bank_parsers.parse_bank_statement(pdf_bytes, plan.full_text)
        if parsed:
            return {"kind": "bank_statement", "statement": parsed}

    if plan.kind == "bank_statement":
        return {
            "kind": "bank_statement",
            "statement": _llm_bank_statement(pdf_bytes, plan.pages),
        }
    return {"kind": "invoice", "invoices": _llm_invoices(pdf_bytes, plan.pages)}
