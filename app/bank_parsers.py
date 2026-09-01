"""Deterministic bank-statement parsers for known Czech banks.

Czech bank statements have a fixed, per-bank table layout. PyMuPDF's plain
``get_text()`` flattens that table into a stream of lines with no column
boundaries, which is why the LLM path drops or merges fields like the variable
symbol (VS). Here we instead use each word's **x-coordinate** to assign it to
the right column, so fields land where they belong — reliably, with no LLM call.

Public API:
  * ``looks_like_bank_statement(text)`` — cheap keyword classifier.
  * ``detect_bank(text)`` — which known bank issued the statement, or None.
  * ``parse_bank_statement(pdf_bytes, text)`` — full statement dict (same shape
    as the LLM path: BANK_STATEMENT_FIELDS + ``transactions``), or None when the
    bank is unknown or nothing could be parsed (caller then falls back to LLM).

Each parser is intentionally specific to one bank's layout; an unknown bank
returns None and the caller uses the LLM path.
"""
import re
from collections import Counter

import fitz  # PyMuPDF

from .schemas import BANK_STATEMENT_FIELDS, BANK_TRANSACTION_FIELDS

_ACCOUNT_RE = re.compile(r"^\d{0,6}-?\d{2,10}/\d{4}$")
_DATE_DOT_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
_VS_RE = re.compile(r"^\d{2,}$")

# Vertical tolerance (px) between a word and its row's average y. Comfortably
# below the line spacing of every statement layout, yet wide enough for the
# baseline drift a scanned/deskewed page carries across a line.
_ROW_TOL = 5.0

# A monetary value, tolerant of the various space characters Czech PDFs use as
# thousands separators (regular, non-breaking, narrow no-break).
_BAL = "(-?\\d[\\d.,\u00a0\u202f ]*\\d)"


# --- classification + detection -------------------------------------------

def looks_like_bank_statement(text: str) -> bool:
    """True if the text is clearly a bank account statement (výpis z účtu).

    High precision: requires both a statement title and an opening/closing
    balance line, which invoices/receipts never have. Used to route to the
    deterministic/LLM statement path without an LLM classification call.
    """
    t = text.lower()
    title = any(
        s in t for s in (
            "výpis z účtu", "výpis z běžného účtu", "výpis z bankovního účtu",
        )
    )
    balance = "počáteční zůstatek" in t or "konečný zůstatek" in t
    return title and balance


def detect_bank(text: str):
    """Return a known-bank key ('mbank' | 'raiffeisen' | 'ceska_sporitelna')
    from unmistakable markers, or None for an unrecognized bank."""
    t = text.lower()
    if "mbank" in t:
        return "mbank"
    if "raiffeisenbank" in t or "rzbcczpp" in t:
        return "raiffeisen"
    if "gibaczpx" in t or "česká spořitelna" in t or "ceska sporitelna" in t:
        return "ceska_sporitelna"
    return None


# --- shared helpers --------------------------------------------------------

def _page_rows(page) -> list[tuple[float, list[tuple[float, str]]]]:
    """Group a page's words into visual rows.

    Returns a list of ``(y, cells)`` sorted top-to-bottom, where ``cells`` is a
    list of ``(x, word)`` sorted left-to-right. Words within ~3px vertically are
    treated as the same row.
    """
    # Cluster words into rows by y-proximity (words on one visual line share a
    # y within a couple of px). Rounding into fixed buckets would split a line
    # whose words straddle a bucket boundary, so grow a cluster while the next
    # word's y stays within _ROW_TOL of the cluster's *running average* — a
    # scanned line drifts a little with every word, and measuring against a
    # fixed anchor would break the line apart once that drift accumulated.
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    ys: list[float] = []
    cur: list[tuple[float, str]] = []
    for x0, y0, x1, y1, word, *_ in words:
        if not ys or abs(y0 - sum(ys) / len(ys)) <= _ROW_TOL:
            cur.append((x0, word))
            ys.append(y0)
        else:
            rows.append((ys[0], sorted(cur)))
            cur, ys = [(x0, word)], [y0]
    if cur:
        rows.append((ys[0], sorted(cur)))
    return rows


def _col(cells, lo, hi) -> list[str]:
    """Words whose x is in [lo, hi), in reading order."""
    return [w for x, w in cells if lo <= x < hi]


def _date_column_offset(doc, lo, hi) -> float:
    """How far this document's date column sits from the band a parser expects.

    Scans and alternative export templates shift the whole transaction table
    sideways; without correcting for it every word falls outside its column band
    and the parser silently finds nothing. The date column is the anchor because
    it repeats once per transaction — the most frequent date x on the document is
    the table's, not a one-off date in the header.
    """
    counts = Counter()
    for page in doc:
        for _y, cells in _page_rows(page):
            x = next((x for x, w in cells if _DATE_DOT_RE.match(w)), None)
            if x is not None:
                counts[round(x)] += 1
    if not counts:
        return 0.0
    x = max(counts, key=lambda k: (counts[k], -k))
    if lo <= x < hi:
        return 0.0
    # Align the date column with its band's left edge — column text is
    # left-aligned, so every other column lands just inside its own band. The
    # extra point of slack absorbs the rounding above.
    return x - lo - 1.0


def _join(words) -> str | None:
    s = " ".join(words).strip()
    return s or None


def _first_match(words, pattern) -> str | None:
    for w in words:
        if pattern.match(w):
            return w
    return None


def _first_amount(words) -> str | None:
    """First monetary token from a column's words (e.g. ['+1','439.44'] ->
    '+1 439.44'). Stops at the first value so trailing page-footer/summary text
    that happens to sit in the amount column isn't swept into the number."""
    m = re.search(r"[+-]?\d[\d   .,]*\d|[+-]?\d", " ".join(words))
    return m.group(0) if m else None


def _blank_statement() -> dict:
    d = {f: None for f in BANK_STATEMENT_FIELDS}
    d["transactions"] = []
    return d


def _blank_txn() -> dict:
    return {f: None for f in BANK_TRANSACTION_FIELDS}


def _search(text, pattern, group=1):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(group).strip() if m else None


def _period(text):
    """Extract 'DD.MM.YYYY - DD.MM.YYYY' (spaces optional) -> (start, end)."""
    m = re.search(
        r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})\s*[-–]\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})",
        text,
    )
    if not m:
        return None, None
    norm = lambda s: s.replace(" ", "")
    return norm(m.group(1)), norm(m.group(2))


# --- mBank -----------------------------------------------------------------

def _parse_mbank(doc, text, off=0.0) -> dict:
    st = _blank_statement()
    st["account_number"] = _search(text, r"Číslo účtu\s*\n?\s*([\d-]+/\d{4})")
    st["currency"] = _search(text, r"Měna účtu\s*\n?\s*([A-Z]{3})")
    st["period_start"], st["period_end"] = _period(text)
    st["opening_balance"] = _search(text, r"Počáteční zůstatek:\s*" + _BAL)
    st["closing_balance"] = _search(text, r"Konečný zůstatek:\s*" + _BAL)

    txns = []
    for page in doc:
        cur = None
        body: list[str] = []

        def flush():
            nonlocal cur, body
            if cur is None:
                return
            acc_idx = next((i for i, l in enumerate(body) if _ACCOUNT_RE.match(l)), None)
            if acc_idx is not None:
                cur["counterparty_account"] = body[acc_idx]
                if acc_idx >= 1:
                    cur["counterparty_name"] = body[acc_idx - 1]
                desc = [body[0]] if acc_idx > 0 else []
                desc += body[acc_idx + 1:]
                cur["description"] = _join(desc)
            else:
                cur["description"] = _join(body)
            txns.append(cur)
            cur, body = None, []

        for y, cells in _page_rows(page):
            date = _first_match(_col(cells, 45 + off, 105 + off), _DATE_DOT_RE)
            amt = _join(_col(cells, 405 + off, 477 + off))
            if date and amt:  # a transaction's first row (date + amount present)
                flush()
                cur = _blank_txn()
                cur["date"] = date
                cur["amount"] = amt.replace(" ", " ")
            elif cur is not None:
                line = _join(_col(cells, 155 + off, 405 + off))
                if line:
                    body.append(line)
        flush()
    st["transactions"] = txns
    return st


# --- Raiffeisenbank --------------------------------------------------------

def _parse_raiffeisen(doc, text, off=0.0) -> dict:
    st = _blank_statement()
    st["account_number"] = _search(text, r"Číslo účtu:\s*\n?\s*([\d-]+/\d{4})")
    st["currency"] = _search(text, r"Číslo účtu:\s*\n?\s*[\d-]+/\d{4}\s*([A-Z]{3})") or "CZK"
    st["period_start"], st["period_end"] = _period(text)
    st["opening_balance"] = _search(text, r"Počáteční zůstatek:\s*" + _BAL)
    st["closing_balance"] = _search(text, r"Konečný zůstatek:\s*" + _BAL)

    txns = []
    for page in doc:
        cur = None
        body_lines: list[str] = []   # x∈[95,200): typ / account / name
        zprava: list[str] = []       # x∈[200,355): category + message
        vs_words: list[str] = []     # x∈[355,430): VS / SS

        def flush():
            nonlocal cur, body_lines, zprava, vs_words
            if cur is None:
                return
            cur["counterparty_account"] = _first_match(body_lines, _ACCOUNT_RE)
            cur.pop("_typ", None)
            names = [l for l in body_lines if not _ACCOUNT_RE.match(l)]
            cur["counterparty_name"] = _join(names)
            cur["description"] = _join(zprava)
            cur["variable_symbol"] = _first_match(vs_words, _VS_RE)
            txns.append(cur)
            cur, body_lines, zprava, vs_words = None, [], [], []

        for y, cells in _page_rows(page):
            date_words = _col(cells, 30 + off, 95 + off)
            date = "".join(date_words) if len(date_words) >= 3 and date_words[-1].isdigit() else None
            amt_words = [w for w in _col(cells, 490 + off, 560 + off) if w != "CZK"]
            if date and amt_words:  # first row of a transaction
                flush()
                cur = _blank_txn()
                cur["date"] = _reformat_spaced_date(date)
                cur["amount"] = _join(amt_words)
                typ = _join(_col(cells, 95 + off, 200 + off))
                cur["_typ"] = typ or ""
                zprava += _col(cells, 200 + off, 355 + off)
                vs_words += _col(cells, 355 + off, 430 + off)
            elif cur is not None:
                body_lines += [l for l in [_join(_col(cells, 95 + off, 200 + off))] if l]
                zprava += _col(cells, 200 + off, 355 + off)
                vs_words += _col(cells, 355 + off, 430 + off)
        flush()
    st["transactions"] = txns
    return st


def _reformat_spaced_date(joined: str) -> str:
    """'1.6.2026' stays; already dot-joined. Kept as-is (as written)."""
    return joined


# --- Česká spořitelna ------------------------------------------------------

def _parse_ceska_sporitelna(doc, text, off=0.0) -> dict:
    st = _blank_statement()
    st["account_number"] = _search(text, r"Číslo účtu/kód banky:\s*([\d-]+/\d{4})")
    st["statement_number"] = _search(text, r"Číslo výpisu:\s*(\w+)")
    st["currency"] = _search(text, r"Měna účtu:\s*([A-Z]{3})")
    st["period_start"], st["period_end"] = _period(text)
    st["opening_balance"] = _search(text, r"Počáteční zůstatek:\s*" + _BAL)
    st["closing_balance"] = _search(text, r"Konečný zůstatek:\s*" + _BAL)

    txns = []
    for page in doc:
        cur = None
        desc: list[str] = []
        name: list[str] = []
        vs_words: list[str] = []
        amt_words: list[str] = []

        def flush():
            nonlocal cur, desc, name, vs_words, amt_words
            if cur is None:
                return
            # First monetary token in the amount column: the transaction's real
            # amount, ignoring any page-footer/summary numbers that follow it.
            amount = _first_amount(amt_words)
            if amount:  # only rows that carry an amount are real movements
                cur["amount"] = amount
                cur["description"] = _join(desc)
                cur["counterparty_name"] = _join(name)
                cur["variable_symbol"] = _first_match(vs_words, _VS_RE)
                txns.append(cur)
            cur, desc, name, vs_words, amt_words = None, [], [], [], []

        for y, cells in _page_rows(page):
            date = _first_match(_col(cells, 40 + off, 90 + off), _DATE_DOT_RE)
            if date:
                flush()
                cur = _blank_txn()
                cur["date"] = date
            if cur is None:
                continue
            desc += _col(cells, 90 + off, 245 + off)
            for w in _col(cells, 245 + off, 400 + off):
                if _ACCOUNT_RE.match(w) and not cur["counterparty_account"]:
                    cur["counterparty_account"] = w
                else:
                    name.append(w)
            vs_words += _col(cells, 400 + off, 500 + off)
            amt_words += _col(cells, 500 + off, 580 + off)
        flush()
    st["transactions"] = txns
    return st


# --- dispatch --------------------------------------------------------------

_PARSERS = {
    "mbank": _parse_mbank,
    "raiffeisen": _parse_raiffeisen,
    "ceska_sporitelna": _parse_ceska_sporitelna,
}

# Each parser's date column band, used to re-align a sideways-shifted table.
_DATE_BANDS = {
    "mbank": (45, 105),
    "raiffeisen": (30, 95),
    "ceska_sporitelna": (40, 90),
}


def parse_bank_statement(pdf_bytes: bytes, text: str):
    """Deterministically parse a statement from a known bank.

    Returns a statement dict (BANK_STATEMENT_FIELDS + ``transactions``) or None
    when the bank is unrecognized or no transactions could be extracted — in
    both cases the caller should fall back to the LLM path.
    """
    bank = detect_bank(text)
    if bank is None:
        return None
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            statement = _PARSERS[bank](doc, text)
            if not statement["transactions"]:
                # Nothing in the expected columns: the table may be shifted
                # sideways (scan / other template), so retry re-aligned.
                off = _date_column_offset(doc, *_DATE_BANDS[bank])
                if off:
                    statement = _PARSERS[bank](doc, text, off)
    except Exception:
        return None
    # Clean empty strings -> None so the shape matches the LLM path exactly.
    for t in statement["transactions"]:
        for k, v in list(t.items()):
            if isinstance(v, str) and not v.strip():
                t[k] = None
        t.pop("_typ", None)
    if not statement["transactions"]:
        return None
    # Canonicalize currency and drop empty header strings, matching the LLM path.
    cur = statement.get("currency")
    if cur:
        cur = str(cur).strip().upper()
        statement["currency"] = {"KČ": "CZK", "KC": "CZK"}.get(cur, cur)
    for k in BANK_STATEMENT_FIELDS:
        v = statement.get(k)
        if isinstance(v, str) and not v.strip():
            statement[k] = None
    return statement
