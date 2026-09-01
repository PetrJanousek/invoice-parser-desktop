"""Supabase data access via the PostgREST HTTP API (httpx).

We talk to Supabase's REST endpoint directly rather than through the Python SDK
so the new `sb_publishable_` / `sb_secret_` API key format works. The server
authenticates with the service key and **always** filters by ``user_id`` so
tenants only ever touch their own rows; Row-Level Security in
supabase/schema.sql is a second line of defense.
"""
from functools import lru_cache
from typing import Optional

import httpx

from .config import settings

INVOICES = "invoices"
BANK_STATEMENTS = "bank_statements"
REPORTED_DOCUMENTS = "reported_documents"
EMAIL_SETTINGS = "email_settings"
ALLOWED_EMAILS = "allowed_emails"

UPLOADED_FILES_BUCKET = "uploaded-files"


class SupabaseNotConfigured(RuntimeError):
    """Raised when DB access is attempted before real Supabase creds are set."""


class SupabaseError(RuntimeError):
    """A non-2xx response from Supabase."""


@lru_cache
def _client() -> httpx.Client:
    if not settings.supabase_configured:
        raise SupabaseNotConfigured(
            "Supabase is not configured. Create a project, run "
            "supabase/schema.sql, and fill SUPABASE_URL / SUPABASE_SERVICE_KEY "
            "/ SUPABASE_ANON_KEY in .env (see README)."
        )
    return httpx.Client(
        base_url=settings.supabase_url.rstrip("/") + "/rest/v1",
        headers={
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
        },
        timeout=30.0,
    )


def _rows(resp: httpx.Response) -> list[dict]:
    if resp.status_code >= 300:
        raise SupabaseError(f"Supabase {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else []


# --- Storage: original uploaded PDFs ---------------------------------------

@lru_cache
def _storage_client() -> httpx.Client:
    if not settings.supabase_configured:
        raise SupabaseNotConfigured(
            "Supabase is not configured. Create a project, run "
            "supabase/schema.sql, and fill SUPABASE_URL / SUPABASE_SERVICE_KEY "
            "/ SUPABASE_ANON_KEY in .env (see README)."
        )
    return httpx.Client(
        base_url=settings.supabase_url.rstrip("/") + "/storage/v1",
        headers={
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
        },
        timeout=60.0,
    )


def upload_source_file(user_id: str, row_id: str, pdf_bytes: bytes) -> str:
    """Store an uploaded PDF and return its object path.

    The path is ``{user_id}/{row_id}.pdf`` so the storage policy in
    supabase/migrations/006_reported_and_source_files.sql can enforce the same
    private-per-owner rule the tables use. Re-uploading the same row's file
    overwrites it (x-upsert) rather than failing.
    """
    path = f"{user_id}/{row_id}.pdf"
    resp = _storage_client().post(
        f"/object/{UPLOADED_FILES_BUCKET}/{path}",
        content=pdf_bytes,
        headers={"Content-Type": "application/pdf", "x-upsert": "true"},
    )
    if resp.status_code >= 300:
        raise SupabaseError(f"Supabase {resp.status_code}: {resp.text[:300]}")
    return path


def delete_source_file(path: str) -> None:
    """Remove an uploaded PDF from Storage. A missing object is not an error."""
    resp = _storage_client().delete(f"/object/{UPLOADED_FILES_BUCKET}/{path}")
    if resp.status_code >= 300 and resp.status_code != 404:
        raise SupabaseError(f"Supabase {resp.status_code}: {resp.text[:300]}")


# --- Invoices --------------------------------------------------------------

def list_invoices(user_id: str) -> list[dict]:
    resp = _client().get(
        f"/{INVOICES}",
        params={
            "user_id": f"eq.{user_id}",
            "select": "*",
            "order": "created_at.asc",
        },
    )
    return _rows(resp)


def insert_invoice(user_id: str, row: dict) -> dict:
    payload = dict(row)
    payload["user_id"] = user_id
    resp = _client().post(
        f"/{INVOICES}", json=payload, headers={"Prefer": "return=representation"}
    )
    return _rows(resp)[0]


def update_invoice(user_id: str, invoice_id: str, fields: dict) -> Optional[dict]:
    resp = _client().patch(
        f"/{INVOICES}",
        params={"id": f"eq.{invoice_id}", "user_id": f"eq.{user_id}"},
        json=fields,
        headers={"Prefer": "return=representation"},
    )
    rows = _rows(resp)
    return rows[0] if rows else None


def delete_invoice(user_id: str, invoice_id: str) -> Optional[dict]:
    """Delete an invoice and return the row as it was, or None if there was none.

    The deleted row is returned so callers can clean up its source PDF without
    a second read: by the time we know the delete happened, the row is gone.
    """
    resp = _client().delete(
        f"/{INVOICES}",
        params={"id": f"eq.{invoice_id}", "user_id": f"eq.{user_id}"},
        headers={"Prefer": "return=representation"},
    )
    rows = _rows(resp)
    return rows[0] if rows else None


# --- Bank statements -------------------------------------------------------

def list_bank_statements(user_id: str) -> list[dict]:
    resp = _client().get(
        f"/{BANK_STATEMENTS}",
        params={
            "user_id": f"eq.{user_id}",
            "select": "*",
            "order": "created_at.asc",
        },
    )
    return _rows(resp)


def insert_bank_statement(user_id: str, row: dict) -> dict:
    payload = dict(row)
    payload["user_id"] = user_id
    resp = _client().post(
        f"/{BANK_STATEMENTS}",
        json=payload,
        headers={"Prefer": "return=representation"},
    )
    return _rows(resp)[0]


def update_bank_statement(
    user_id: str, statement_id: str, fields: dict
) -> Optional[dict]:
    resp = _client().patch(
        f"/{BANK_STATEMENTS}",
        params={"id": f"eq.{statement_id}", "user_id": f"eq.{user_id}"},
        json=fields,
        headers={"Prefer": "return=representation"},
    )
    rows = _rows(resp)
    return rows[0] if rows else None


def delete_bank_statement(user_id: str, statement_id: str) -> Optional[dict]:
    """Delete a statement and return the row as it was, or None if there was none."""
    resp = _client().delete(
        f"/{BANK_STATEMENTS}",
        params={"id": f"eq.{statement_id}", "user_id": f"eq.{user_id}"},
        headers={"Prefer": "return=representation"},
    )
    rows = _rows(resp)
    return rows[0] if rows else None


# --- Reported documents ----------------------------------------------------

def insert_reported_document(
    user_id: str,
    kind: str,
    original_row_id: str,
    file_path: Optional[str],
    extracted_data: dict,
) -> dict:
    """Append a permanent snapshot of a row that was just reported."""
    resp = _client().post(
        f"/{REPORTED_DOCUMENTS}",
        json={
            "user_id": user_id,
            "kind": kind,
            "original_row_id": original_row_id,
            "file_path": file_path,
            "extracted_data": extracted_data,
        },
        headers={"Prefer": "return=representation"},
    )
    return _rows(resp)[0]


def file_path_in_use(
    user_id: str, file_path: str, exclude_id: Optional[str] = None
) -> bool:
    """True while anything still needs the Storage object at ``file_path``.

    A single PDF can back several rows (a multi-invoice upload) and is kept
    forever once a snapshot in ``reported_documents`` points at it, so deleting
    one row may not free the file. ``exclude_id`` skips the row being deleted,
    whose own reference no longer counts.
    """
    if not file_path:
        return False
    for table in (REPORTED_DOCUMENTS, INVOICES, BANK_STATEMENTS):
        params = {
            "user_id": f"eq.{user_id}",
            "file_path": f"eq.{file_path}",
            "select": "id",
            "limit": 1,
        }
        if exclude_id and table != REPORTED_DOCUMENTS:
            params["id"] = f"neq.{exclude_id}"
        if _rows(_client().get(f"/{table}", params=params)):
            return True
    return False


# --- Email settings --------------------------------------------------------

def get_email_settings(user_id: str) -> Optional[dict]:
    resp = _client().get(
        f"/{EMAIL_SETTINGS}",
        params={"user_id": f"eq.{user_id}", "select": "*", "limit": 1},
    )
    rows = _rows(resp)
    return rows[0] if rows else None


def upsert_email_settings(user_id: str, fields: dict) -> dict:
    payload = dict(fields)
    payload["user_id"] = user_id
    resp = _client().post(
        f"/{EMAIL_SETTINGS}",
        params={"on_conflict": "user_id"},
        json=payload,
        headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    return _rows(resp)[0]


# --- Access allowlist ------------------------------------------------------

def is_email_allowed(email: str) -> bool:
    """True iff ``email`` is on the admin-managed access allowlist.

    Signup is open in Supabase, but only allowlisted emails may use the app
    (see app/auth.py). Emails are stored lowercased in ``allowed_emails``; we
    lowercase the input to match. Fails **closed**: any error (e.g. the table
    doesn't exist yet) is treated as "not allowed" so a misconfiguration can
    never silently grant access.
    """
    if not email:
        return False
    normalized = email.strip().lower()
    if not normalized:
        return False
    try:
        resp = _client().get(
            f"/{ALLOWED_EMAILS}",
            params={
                "email": f"eq.{normalized}",
                "select": "email",
                "limit": 1,
            },
        )
        return bool(_rows(resp))
    except Exception:
        return False


# --- Auth: validate a user's access token ----------------------------------

def get_user_from_token(access_token: str) -> Optional[dict]:
    """Validate a Supabase access token; return the user dict or None.

    Uses the GoTrue /auth/v1/user endpoint with the anon key + the user's
    token, so it works regardless of the project's JWT signing configuration.
    """
    if not settings.supabase_configured:
        raise SupabaseNotConfigured("Supabase is not configured.")
    resp = httpx.get(
        settings.supabase_url.rstrip("/") + "/auth/v1/user",
        headers={
            "apikey": settings.supabase_anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=15.0,
    )
    if resp.status_code == 200:
        return resp.json()
    return None
