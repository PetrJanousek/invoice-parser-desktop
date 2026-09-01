"""Local SQLite data access for the single-user desktop app.

Every public function still accepts the same arguments as the old Supabase
layer (including a leading ``user_id``) so ``app.main`` does not need to
change in this step. ``user_id`` is ignored: there is one local user and no
tenancy filter.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings

INVOICES = "invoices"
BANK_STATEMENTS = "bank_statements"
REPORTED_DOCUMENTS = "reported_documents"
EMAIL_SETTINGS = "email_settings"

# Single local email-settings row.
EMAIL_SETTINGS_ID = 1

INVOICE_COLUMNS = (
    "id",
    "filename",
    "source",
    "sender_email",
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
    "error",
    "pohoda_ico",
    "file_path",
    "reported",
    "reported_at",
    "created_at",
    "updated_at",
)

BANK_STATEMENT_COLUMNS = (
    "id",
    "filename",
    "source",
    "sender_email",
    "account_number",
    "statement_number",
    "currency",
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
    "transactions",
    "error",
    "pohoda_ico",
    "file_path",
    "reported",
    "reported_at",
    "matched_parser",
    "created_at",
    "updated_at",
)

REPORTED_DOCUMENT_COLUMNS = (
    "id",
    "kind",
    "original_row_id",
    "file_path",
    "extracted_data",
    "reported_at",
    "created_at",
)

EMAIL_SETTINGS_COLUMNS = (
    "id",
    "provider",
    "email_address",
    "imap_password_encrypted",
    "auto_poll",
    "last_polled_at",
    "created_at",
    "updated_at",
)

_COLUMNS = {
    INVOICES: INVOICE_COLUMNS,
    BANK_STATEMENTS: BANK_STATEMENT_COLUMNS,
    REPORTED_DOCUMENTS: REPORTED_DOCUMENT_COLUMNS,
    EMAIL_SETTINGS: EMAIL_SETTINGS_COLUMNS,
}

_BOOL_COLUMNS = frozenset({"reported", "auto_poll"})
_JSON_COLUMNS = {
    "transactions": list,
    "extracted_data": dict,
}

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {INVOICES} (
    id TEXT PRIMARY KEY,
    filename TEXT,
    source TEXT,
    sender_email TEXT,
    document_type TEXT,
    vendor TEXT,
    ico TEXT,
    customer TEXT,
    customer_ico TEXT,
    invoice_number TEXT,
    variable_symbol TEXT,
    invoice_date TEXT,
    due_date TEXT,
    currency TEXT,
    subtotal TEXT,
    tax TEXT,
    total TEXT,
    bank_account TEXT,
    error TEXT,
    pohoda_ico TEXT,
    file_path TEXT,
    reported INTEGER NOT NULL DEFAULT 0,
    reported_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS {BANK_STATEMENTS} (
    id TEXT PRIMARY KEY,
    filename TEXT,
    source TEXT,
    sender_email TEXT,
    account_number TEXT,
    statement_number TEXT,
    currency TEXT,
    period_start TEXT,
    period_end TEXT,
    opening_balance TEXT,
    closing_balance TEXT,
    transactions TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    pohoda_ico TEXT,
    file_path TEXT,
    reported INTEGER NOT NULL DEFAULT 0,
    reported_at TEXT,
    matched_parser TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS {REPORTED_DOCUMENTS} (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    original_row_id TEXT NOT NULL,
    file_path TEXT,
    extracted_data TEXT NOT NULL DEFAULT '{{}}',
    reported_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS {EMAIL_SETTINGS} (
    id INTEGER PRIMARY KEY,
    provider TEXT,
    email_address TEXT,
    imap_password_encrypted TEXT,
    auto_poll INTEGER NOT NULL DEFAULT 0,
    last_polled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
"""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _db_path() -> Path:
    return settings.data_dir / "invoice-parser.db"


def _files_dir() -> Path:
    path = settings.data_dir / "files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_source_file(path: str) -> Path:
    """Absolute path of a stored PDF.

    Rows keep a relative ``file_path`` such as ``{row_id}.pdf``. The file
    lives under ``settings.data_dir / "files"``.
    """
    given = Path(path)
    if given.is_absolute():
        return given
    return _files_dir() / given.name


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _encode(column: str, value: Any) -> Any:
    if column in _BOOL_COLUMNS:
        if value is None:
            return 0
        return 1 if value else 0
    if column in _JSON_COLUMNS:
        if value is None:
            return json.dumps(_JSON_COLUMNS[column]())
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value
    return value


def _decode_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    for column in _BOOL_COLUMNS:
        if column in out and out[column] is not None:
            out[column] = bool(out[column])
    for column, factory in _JSON_COLUMNS.items():
        if column not in out:
            continue
        raw = out[column]
        if raw is None:
            out[column] = factory()
            continue
        if isinstance(raw, (dict, list)):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            out[column] = factory()
            continue
        out[column] = parsed if isinstance(parsed, (dict, list)) else factory()
    return out


def _pick(row: dict, columns: tuple[str, ...]) -> dict:
    return {k: row[k] for k in columns if k in row}


def _insert(table: str, payload: dict) -> dict:
    columns = tuple(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    values = [_encode(c, payload[c]) for c in columns]
    with _connect() as conn:
        conn.execute(sql, values)
        fetched = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (payload["id"],)
        ).fetchone()
    return _decode_row(fetched)


def _get_by_id(table: str, row_id: Any) -> Optional[dict]:
    with _connect() as conn:
        fetched = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
    return _decode_row(fetched) if fetched else None


def _update(table: str, row_id: str, fields: dict) -> Optional[dict]:
    allowed = _COLUMNS[table]
    payload = _pick(fields, allowed)
    payload.pop("id", None)
    if not payload:
        return _get_by_id(table, row_id)
    assignments = ", ".join(f"{c} = ?" for c in payload)
    values = [_encode(c, payload[c]) for c in payload]
    values.append(row_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?", values
        )
        if cur.rowcount == 0:
            return None
        fetched = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
    return _decode_row(fetched) if fetched else None


def _delete(table: str, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        fetched = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        if fetched is None:
            return None
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    return _decode_row(fetched)


def _list_by_created_at(table: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY created_at ASC"
        ).fetchall()
    return [_decode_row(r) for r in rows]


# --- Storage: original uploaded PDFs ---------------------------------------

def upload_source_file(user_id: str, row_id: str, pdf_bytes: bytes) -> str:
    """Store an uploaded PDF and return its relative path.

    The path is ``{row_id}.pdf`` under ``settings.data_dir / "files"``.
    Re-uploading the same row overwrites the file.
    """
    del user_id
    rel = f"{row_id}.pdf"
    dest = _files_dir() / rel
    dest.write_bytes(pdf_bytes)
    return rel


def delete_source_file(path: str) -> None:
    """Remove an uploaded PDF. A missing file is not an error."""
    if not path:
        return
    dest = resolve_source_file(path)
    try:
        dest.unlink()
    except FileNotFoundError:
        return


# --- Invoices --------------------------------------------------------------

def list_invoices(user_id: str) -> list[dict]:
    del user_id
    return _list_by_created_at(INVOICES)


def insert_invoice(user_id: str, row: dict) -> dict:
    del user_id
    payload = _pick(row, INVOICE_COLUMNS)
    payload.setdefault("id", str(uuid.uuid4()))
    payload.setdefault("created_at", _now_iso())
    payload.setdefault("reported", False)
    return _insert(INVOICES, payload)


def update_invoice(user_id: str, invoice_id: str, fields: dict) -> Optional[dict]:
    del user_id
    return _update(INVOICES, invoice_id, fields)


def delete_invoice(user_id: str, invoice_id: str) -> Optional[dict]:
    """Delete an invoice and return the row as it was, or None if there was none.

    The deleted row is returned so callers can clean up its source PDF without
    a second read: by the time we know the delete happened, the row is gone.
    """
    del user_id
    return _delete(INVOICES, invoice_id)


# --- Bank statements -------------------------------------------------------

def list_bank_statements(user_id: str) -> list[dict]:
    del user_id
    return _list_by_created_at(BANK_STATEMENTS)


def insert_bank_statement(user_id: str, row: dict) -> dict:
    del user_id
    payload = _pick(row, BANK_STATEMENT_COLUMNS)
    payload.setdefault("id", str(uuid.uuid4()))
    payload.setdefault("created_at", _now_iso())
    payload.setdefault("reported", False)
    payload.setdefault("transactions", [])
    return _insert(BANK_STATEMENTS, payload)


def update_bank_statement(
    user_id: str, statement_id: str, fields: dict
) -> Optional[dict]:
    del user_id
    return _update(BANK_STATEMENTS, statement_id, fields)


def delete_bank_statement(user_id: str, statement_id: str) -> Optional[dict]:
    """Delete a statement and return the row as it was, or None if there was none."""
    del user_id
    return _delete(BANK_STATEMENTS, statement_id)


# --- Reported documents ----------------------------------------------------

def insert_reported_document(
    user_id: str,
    kind: str,
    original_row_id: str,
    file_path: Optional[str],
    extracted_data: dict,
) -> dict:
    """Append a permanent snapshot of a row that was just reported."""
    del user_id
    now = _now_iso()
    payload = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "original_row_id": original_row_id,
        "file_path": file_path,
        "extracted_data": extracted_data if extracted_data is not None else {},
        "reported_at": now,
        "created_at": now,
    }
    return _insert(REPORTED_DOCUMENTS, payload)


def file_path_in_use(
    user_id: str, file_path: str, exclude_id: Optional[str] = None
) -> bool:
    """True while anything still needs the file at ``file_path``.

    A single PDF can back several rows (a multi-invoice upload) and is kept
    forever once a snapshot in ``reported_documents`` points at it, so deleting
    one row may not free the file. ``exclude_id`` skips the row being deleted,
    whose own reference no longer counts.
    """
    del user_id
    if not file_path:
        return False
    with _connect() as conn:
        for table in (REPORTED_DOCUMENTS, INVOICES, BANK_STATEMENTS):
            sql = f"SELECT id FROM {table} WHERE file_path = ?"
            params: list[Any] = [file_path]
            if exclude_id and table != REPORTED_DOCUMENTS:
                sql += " AND id != ?"
                params.append(exclude_id)
            sql += " LIMIT 1"
            if conn.execute(sql, params).fetchone():
                return True
    return False


# --- Email settings --------------------------------------------------------

def get_email_settings(user_id: str) -> Optional[dict]:
    del user_id
    return _get_by_id(EMAIL_SETTINGS, EMAIL_SETTINGS_ID)


def upsert_email_settings(user_id: str, fields: dict) -> dict:
    del user_id
    payload = _pick(fields, EMAIL_SETTINGS_COLUMNS)
    payload.pop("id", None)
    existing = get_email_settings("")
    if existing is None:
        payload["id"] = EMAIL_SETTINGS_ID
        payload.setdefault("created_at", _now_iso())
        payload.setdefault("auto_poll", False)
        return _insert(EMAIL_SETTINGS, payload)
    updated = _update(EMAIL_SETTINGS, EMAIL_SETTINGS_ID, payload)
    return updated if updated is not None else existing


# --- Stubs kept so app.auth still imports until that module is removed -----

class SupabaseNotConfigured(RuntimeError):
    """Unused. Kept so ``app.auth`` can still import this name."""


def is_email_allowed(email: str) -> bool:
    """Unused stub. Auth is removed in a later step."""
    del email
    return False


def get_user_from_token(access_token: str) -> Optional[dict]:
    """Unused stub. Auth is removed in a later step."""
    del access_token
    return None
