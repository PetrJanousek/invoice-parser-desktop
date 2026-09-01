"""FastAPI application: web UI + JSON API.

Routes:
  GET    /                       -> single-page UI
  GET    /api/config             -> public config (model, email providers)
  GET    /api/health             -> liveness
  GET    /api/rows               -> current user's invoices
  POST   /api/upload             -> multipart PDF -> extract -> store
  PATCH  /api/rows/{id}          -> edit invoice fields (manual correction)
  DELETE /api/rows/{id}          -> delete one invoice
  POST   /api/rows/{id}/report   -> flag/unflag one invoice for review
  GET    /api/bank-statements    -> current user's bank statements
  POST   /api/bank-statements/{id}/report -> flag/unflag one statement
  GET    /api/email-settings     -> current user's email settings (no password)
  PUT    /api/email-settings     -> save email settings (password encrypted)
  GET    /api/llm-settings       -> current LLM provider/model config (no keys)
  PUT    /api/llm-settings       -> save LLM provider + API key (key encrypted)
  GET    /api/update-check       -> is a newer GitHub Release available
  POST   /api/update/apply       -> download + silently install it (Windows only for now)
  POST   /api/emails/read        -> read up to N unread emails, parse, store
  GET    /api/export/pohoda.xml  -> all invoices as a Pohoda dataPack
"""
import datetime
import io
import json
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from . import bank_parsers, db, extract
from .config import apply_stored_llm_settings, settings
from .crypto import encrypt
from .email_ingest import PROVIDERS, EmailConfigError, read_unread
from .extract import PdfTextError
from .llm import (
    active_model_label,
    active_vision_model_label,
    get_chat_model,
    get_vision_chat_model,
)
from .pohoda import build_pohoda_bank_xml, build_pohoda_xml
from .schemas import BANK_STATEMENT_FIELDS, FIELDS
from . import updater

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX = BASE_DIR / "web" / "index.html"

app = FastAPI(title="Invoice Parser")

# Overlay any LLM provider/API-key settings saved via the Settings screen onto
# the live `settings` singleton before the app starts serving requests. No
# startup-event convention exists elsewhere in this codebase, so this runs
# directly at import time — main.py is only imported once per process.
apply_stored_llm_settings()

LLM_PROVIDERS = ("anthropic", "openai", "google", "ollama")

# Fields a user is allowed to edit via PATCH: the extracted fields, plus
# pohoda_ico (manually entered — never extracted, so it's not in FIELDS).
EDITABLE = set(FIELDS) | {"pohoda_ico"}
# Bank-statement fields a user may edit: the scalar header fields, the whole
# transactions array (edited client-side and sent back as a list), and
# pohoda_ico (manually entered, same as invoices).
EDITABLE_BANK = set(BANK_STATEMENT_FIELDS) | {"transactions", "pohoda_ico"}

# Absolute ceiling on how many emails one "Read emails now" click may process,
# regardless of the per-request value the user asks for (safety valve).
EMAIL_BATCH_HARD_MAX = 200

# Single local user: db.py functions still take a leading user_id argument
# (ignored internally) so this placeholder is passed everywhere one is needed.
LOCAL_USER = "local"


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model": active_model_label(),
        "vision_model": active_vision_model_label(),
    }


@app.get("/api/config")
def public_config():
    """Non-secret config the frontend needs to boot."""
    return {
        "model": active_model_label(),
        "vision_model": active_vision_model_label(),
        "email_providers": list(PROVIDERS.keys()),
        "email_batch_cap": settings.email_batch_cap,
        "email_batch_max": EMAIL_BATCH_HARD_MAX,
    }


@app.get("/api/rows")
def get_rows():
    return db.list_invoices(LOCAL_USER)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _store_source_pdf(user_id: str, rows: list[dict], pdf_bytes: bytes, update) -> None:
    """Save the PDF these rows came from and point each of them at it.

    One PDF can yield several invoice rows, so the file is stored once (under
    the first row's id) and shared by all of them. Storage is best-effort: a
    failure here must not lose an otherwise successful extraction, so the rows
    just keep a null ``file_path`` and stay reviewable by filename.
    """
    if not rows:
        return
    try:
        path = db.upload_source_file(user_id, rows[0]["id"], pdf_bytes)
    except Exception:
        return
    for row in rows:
        updated = update(user_id, row["id"], {"file_path": path})
        if updated:
            row.update(updated)


def _report_fields(reported: bool) -> dict:
    return {
        "reported": reported,
        "reported_at": _now_iso() if reported else None,
        "updated_at": _now_iso(),
    }


def _snapshot_reported(kind: str, user_id: str, row: dict) -> None:
    """Record what a row looked like the moment it was reported.

    The snapshot is what makes a report durable: the live row may later be
    corrected or deleted, and the point of the report is the state that looked
    wrong. Un-reporting never removes a snapshot — this is an append-only log.
    """
    db.insert_reported_document(
        user_id=user_id,
        kind=kind,
        original_row_id=row["id"],
        file_path=row.get("file_path"),
        extracted_data=row,
    )


def _cleanup_source_file(user_id: str, row: dict) -> None:
    """Delete a deleted row's PDF unless something else still points at it.

    One PDF can back several rows and any snapshot in ``reported_documents``
    pins it for good, so the file only goes when the last reference does.
    Storage is best-effort here for the same reason as in ``_store_source_pdf``:
    the row is already gone, and a leaked object must not fail the request.
    """
    file_path = row.get("file_path")
    if not file_path:
        return
    try:
        if not db.file_path_in_use(user_id, file_path, exclude_id=row["id"]):
            db.delete_source_file(file_path)
    except Exception:
        return


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Extract a PDF and store it, streaming progress as Server-Sent Events.

    The response is a text/event-stream of `data: {json}` events:
      - {"type": "invoices", "rows": [...]}                  (invoice/receipt PDF)
      - {"type": "statement_started", "row": {...}}          (first chunk saved)
      - {"type": "statement_progress", "id", "transactions", "added"}
      - {"type": "error", "message": ...}                    (fatal for this file)
      - {"type": "done", "kind": ...}                        (always last)

    Bank statements are extracted in page-chunks and the row is saved after each
    chunk, so the client sees transactions fill in live instead of waiting for a
    long multi-page statement to finish.
    """
    filename = file.filename
    pdf_bytes = await file.read()

    def _invoice_base() -> dict:
        return {
            **{f: None for f in FIELDS},
            "filename": filename,
            "source": "upload",
            "sender_email": None,
            "error": None,
        }

    def _statement_base() -> dict:
        return {
            **{f: None for f in BANK_STATEMENT_FIELDS},
            "transactions": [],
            # Set only when a deterministic parser matched; a null marks a bank
            # layout we can't parse yet (see supabase/migrations/007).
            "matched_parser": None,
            "filename": filename,
            "source": "upload",
            "sender_email": None,
            "error": None,
        }

    async def stream():
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        # 1. Read + classify the PDF. A text PDF is read in full (its text is
        # free and drives the parsers below); a scan on Anthropic is classified
        # from page 1 alone and left for the document calls to read natively,
        # so plan.pages is None and no per-page OCR was paid for.
        try:
            plan = await run_in_threadpool(extract.plan_document, pdf_bytes)
        except PdfTextError as exc:
            row = db.insert_invoice(LOCAL_USER, {**_invoice_base(), "error": str(exc)})
            _store_source_pdf(LOCAL_USER, [row], pdf_bytes, db.update_invoice)
            yield sse({"type": "invoices", "rows": [row]})
            yield sse({"type": "done", "kind": "invoice"})
            return
        except Exception as exc:
            row = db.insert_invoice(
                LOCAL_USER, {**_invoice_base(), "error": f"Extraction failed: {exc}"}
            )
            _store_source_pdf(LOCAL_USER, [row], pdf_bytes, db.update_invoice)
            yield sse({"type": "invoices", "rows": [row]})
            yield sse({"type": "done", "kind": "invoice"})
            return

        pages, full_text, kind = plan.pages, plan.full_text, plan.kind

        # 2. Deterministic fast path: an unambiguous statement from a known bank
        # is parsed by coordinates instantly (reliable VS/amounts, no LLM). The
        # whole statement is saved and emitted at once. It reads the PDF's own
        # text coordinates, so it is only worth attempting on a document that
        # was read as text.
        if pages is not None and bank_parsers.looks_like_bank_statement(full_text):
            parsed = await run_in_threadpool(
                bank_parsers.parse_bank_statement, pdf_bytes, full_text
            )
            if parsed:
                row = db.insert_bank_statement(
                    LOCAL_USER,
                    {
                        **_statement_base(),
                        **parsed,
                        "matched_parser": bank_parsers.detect_bank(full_text),
                    },
                )
                _store_source_pdf(
                    LOCAL_USER, [row], pdf_bytes, db.update_bank_statement
                )
                yield sse({"type": "statement_started", "row": row})
                yield sse({"type": "done", "kind": "bank_statement", "id": row["id"]})
                return

        # 3a. Invoice path: extract all, insert, emit once. With Anthropic the
        # PDF itself goes up in one call — the model reads the original layout
        # instead of the flattened per-page text, and no other provider accepts
        # raw PDF bytes, so the text path stays for them.
        if kind != "bank_statement":
            try:
                if settings.llm_provider == "anthropic":
                    invoices = await run_in_threadpool(
                        extract.extract_invoices_via_haiku_document, pdf_bytes
                    )
                else:
                    invoices = await run_in_threadpool(
                        extract.invoices_from_pages, pages
                    )
                rows = [{**_invoice_base(), **inv} for inv in invoices]
            except Exception as exc:
                rows = [{**_invoice_base(), "error": f"Extraction failed: {exc}"}]
            inserted = [db.insert_invoice(LOCAL_USER, r) for r in rows]
            _store_source_pdf(LOCAL_USER, inserted, pdf_bytes, db.update_invoice)
            yield sse({"type": "invoices", "rows": inserted})
            yield sse({"type": "done", "kind": "invoice"})
            return

        # 3b. Unknown bank layout, Anthropic configured: send the PDF itself to
        # Claude in one call. It reads the original table layout instead of the
        # flattened text the chunked path below works from, and no other
        # provider accepts raw PDF bytes, so this branch is Anthropic-only.
        if settings.llm_provider == "anthropic":
            try:
                statement = await run_in_threadpool(
                    extract.extract_bank_statement_via_haiku_document, pdf_bytes
                )
            except Exception as exc:
                statement = {"error": f"Extraction failed: {exc}"}
            row = db.insert_bank_statement(
                LOCAL_USER, {**_statement_base(), **statement}
            )
            _store_source_pdf(LOCAL_USER, [row], pdf_bytes, db.update_bank_statement)
            yield sse({"type": "statement_started", "row": row})
            yield sse({"type": "done", "kind": "bank_statement", "id": row["id"]})
            return

        # 3c. Bank-statement path: extract in page-chunks, saving + emitting
        # after each so the client sees transactions accumulate live.
        chunks = extract._page_chunks(pages)
        header = {f: None for f in BANK_STATEMENT_FIELDS}
        all_txns: list[dict] = []
        row = None
        for chunk_pages in chunks:
            chunk_text = "\n".join(chunk_pages)
            try:
                chunk_st = await run_in_threadpool(
                    extract.extract_bank_statement_from_text, chunk_text
                )
            except Exception as exc:
                yield sse({"type": "warning", "message": f"A page group failed: {exc}"})
                continue
            for f in BANK_STATEMENT_FIELDS:
                if header[f] is None and chunk_st.get(f) is not None:
                    header[f] = chunk_st[f]
            new_txns = chunk_st.get("transactions") or []
            all_txns.extend(new_txns)

            if row is None:
                row = db.insert_bank_statement(
                    LOCAL_USER,
                    {**_statement_base(), **header, "transactions": all_txns},
                )
                _store_source_pdf(
                    LOCAL_USER, [row], pdf_bytes, db.update_bank_statement
                )
                yield sse({"type": "statement_started", "row": row})
            else:
                row = db.update_bank_statement(
                    LOCAL_USER,
                    row["id"],
                    {**header, "transactions": all_txns, "updated_at": _now_iso()},
                )
                yield sse({
                    "type": "statement_progress",
                    "id": row["id"],
                    "transactions": all_txns,
                    "added": len(new_txns),
                })

        # No chunk produced a row (every group failed) -> store an empty one so
        # the file still shows up and can be retried/corrected.
        if row is None:
            row = db.insert_bank_statement(
                LOCAL_USER,
                {**_statement_base(), "error": "No transactions could be extracted."},
            )
            _store_source_pdf(
                LOCAL_USER, [row], pdf_bytes, db.update_bank_statement
            )
            yield sse({"type": "statement_started", "row": row})

        yield sse({"type": "done", "kind": "bank_statement", "id": row["id"]})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.patch("/api/rows/{invoice_id}")
async def edit_row(invoice_id: str, payload: dict):
    fields = {k: v for k, v in payload.items() if k in EDITABLE}
    if not fields:
        raise HTTPException(status_code=400, detail="No editable fields given.")
    fields["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    updated = db.update_invoice(LOCAL_USER, invoice_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    return updated


@app.post("/api/rows/{invoice_id}/report")
def report_row(invoice_id: str, payload: dict = Body(default={})):
    """Flag an invoice as needing a manual/agent review of its extraction.

    Reporting also snapshots the row into ``reported_documents``. Send
    ``{"reported": false}`` to clear the flag again — the UI's button is a
    toggle so a mis-click is undoable — which leaves the snapshot in place.
    """
    reported = bool(payload.get("reported", True))
    updated = db.update_invoice(LOCAL_USER, invoice_id, _report_fields(reported))
    if updated is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if reported:
        _snapshot_reported("invoice", LOCAL_USER, updated)
    return updated


@app.delete("/api/rows/{invoice_id}")
def remove_row(invoice_id: str):
    deleted = db.delete_invoice(LOCAL_USER, invoice_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    _cleanup_source_file(LOCAL_USER, deleted)
    return {"deleted": invoice_id}


# --- Bank statements -------------------------------------------------------

@app.get("/api/bank-statements")
def get_bank_statements():
    return db.list_bank_statements(LOCAL_USER)


@app.patch("/api/bank-statements/{statement_id}")
def edit_bank_statement(statement_id: str, payload: dict):
    fields = {k: v for k, v in payload.items() if k in EDITABLE_BANK}
    if not fields:
        raise HTTPException(status_code=400, detail="No editable fields given.")
    if "transactions" in fields and not isinstance(fields["transactions"], list):
        raise HTTPException(status_code=400, detail="transactions must be a list.")
    fields["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    updated = db.update_bank_statement(LOCAL_USER, statement_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Bank statement not found.")
    return updated


@app.post("/api/bank-statements/{statement_id}/report")
def report_bank_statement(statement_id: str, payload: dict = Body(default={})):
    """Flag a bank statement for review; ``{"reported": false}`` clears it."""
    reported = bool(payload.get("reported", True))
    updated = db.update_bank_statement(
        LOCAL_USER, statement_id, _report_fields(reported)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Bank statement not found.")
    if reported:
        _snapshot_reported("bank_statement", LOCAL_USER, updated)
    return updated


@app.delete("/api/bank-statements/{statement_id}")
def remove_bank_statement(statement_id: str):
    deleted = db.delete_bank_statement(LOCAL_USER, statement_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Bank statement not found.")
    _cleanup_source_file(LOCAL_USER, deleted)
    return {"deleted": statement_id}


# --- Email settings + ingestion -------------------------------------------

@app.get("/api/email-settings")
def read_email_settings():
    row = db.get_email_settings(LOCAL_USER) or {}
    # Never leak the encrypted password; just say whether one is set.
    return {
        "provider": row.get("provider"),
        "email_address": row.get("email_address"),
        "auto_poll": bool(row.get("auto_poll", False)),
        "has_password": bool(row.get("imap_password_encrypted")),
        "last_polled_at": row.get("last_polled_at"),
    }


@app.put("/api/email-settings")
def save_email_settings(payload: dict):
    provider = (payload.get("provider") or "").lower()
    if provider and provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}.")

    fields = {
        "provider": provider or None,
        "email_address": payload.get("email_address"),
        "auto_poll": bool(payload.get("auto_poll", False)),
    }
    # Only overwrite the password when a new (non-empty) one is supplied.
    password = payload.get("password")
    if password:
        fields["imap_password_encrypted"] = encrypt(password)

    db.upsert_email_settings(LOCAL_USER, fields)
    return read_email_settings()


# --- LLM settings ------------------------------------------------------------

@app.get("/api/llm-settings")
def read_llm_settings():
    # `settings` is the live source of truth after startup (mutated in place
    # by apply_stored_llm_settings()), so read off it directly rather than
    # the DB row. Never return raw keys — only whether one is set.
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "vision_model": settings.llm_vision_model,
        "ollama_url": settings.ollama_url,
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "has_openai_key": bool(settings.openai_api_key),
        "has_google_key": bool(settings.google_api_key),
    }


@app.put("/api/llm-settings")
def save_llm_settings(payload: dict):
    provider = payload.get("provider")
    if provider is not None and provider not in LLM_PROVIDERS:
        raise HTTPException(
            status_code=400, detail=f"Unknown provider {provider!r}."
        )

    fields: dict = {}
    if "provider" in payload:
        fields["provider"] = provider
    if "model" in payload:
        fields["model"] = payload.get("model")
    if "vision_model" in payload:
        fields["vision_model"] = payload.get("vision_model")
    if "ollama_url" in payload:
        fields["ollama_url"] = payload.get("ollama_url")

    # Only overwrite the encrypted key for the active provider (the one this
    # request implies is active: the newly-given provider, else the current
    # one), and only when a non-empty key was actually supplied — same
    # "blank means keep existing" pattern the email password already uses.
    api_key = payload.get("api_key")
    if api_key:
        active_provider = provider or settings.llm_provider
        key_column = {
            "anthropic": "anthropic_api_key_encrypted",
            "openai": "openai_api_key_encrypted",
            "google": "google_api_key_encrypted",
        }.get(active_provider)
        if key_column:
            fields[key_column] = encrypt(api_key)

    db.upsert_llm_settings(LOCAL_USER, fields)
    apply_stored_llm_settings()
    get_chat_model.cache_clear()
    get_vision_chat_model.cache_clear()
    return read_llm_settings()


# --- Update check ------------------------------------------------------------

@app.get("/api/update-check")
def update_check():
    return updater.check_for_update()


@app.post("/api/update/apply")
def update_apply():
    """Download and silently install the latest release, then exit.

    Windows only for now (see app/updater.py) — the installer's
    CloseApplications/RestartApplications close this process and relaunch it
    once the update is in place, so nothing else needs to happen here beyond
    telling the frontend it's underway.
    """
    check = updater.check_for_update()
    if not check["available"] or not check["download_url"]:
        raise HTTPException(status_code=400, detail="No update available.")
    try:
        updater.apply_windows_update(check["download_url"])
    except updater.UpdateApplyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "installing"}


@app.post("/api/emails/read")
def read_emails_now(payload: dict = Body(default={})):
    row = db.get_email_settings(LOCAL_USER)
    if not row:
        raise HTTPException(
            status_code=400,
            detail="No email settings saved. Configure email in Settings first.",
        )
    # How many unread emails to process this click. Defaults to the configured
    # batch cap; clamped to [1, EMAIL_BATCH_HARD_MAX].
    try:
        requested = int(payload.get("limit"))
    except (TypeError, ValueError):
        requested = settings.email_batch_cap
    cap = max(1, min(requested, EMAIL_BATCH_HARD_MAX))
    try:
        result = read_unread(row, cap=cap)
    except EmailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted = [db.insert_invoice(LOCAL_USER, r) for r in result["rows"]]
    db.upsert_email_settings(
        LOCAL_USER,
        {
            "last_polled_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
        },
    )
    return {
        "scanned": result["scanned"],
        "parsed": result["parsed"],
        "skipped": result["skipped"],
        "cap": cap,
        "rows": inserted,
    }


# --- Pohoda export ---------------------------------------------------------

def _group_by_pohoda_ico(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by their (stripped) ``pohoda_ico``, blank values under "".

    A Pohoda dataPack carries exactly one ``ico`` attribute for the whole
    file, so rows bound for different accounting units can't share one file.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        ico = (row.get("pohoda_ico") or "").strip()
        groups.setdefault(ico, []).append(row)
    return groups


def _export_response(groups: dict[str, list[dict]], build, base_name: str) -> Response:
    """Return one XML file, or a zip of one XML per IČO group when >1 group."""
    if len(groups) <= 1:
        ico, rows = next(iter(groups.items())) if groups else ("", [])
        xml = build(rows, ico=ico)
        return Response(
            content=xml,
            media_type="application/xml; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.xml"'
            },
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for ico, rows in groups.items():
            name = f"{base_name}_{ico or 'blank'}.xml"
            zf.writestr(name, build(rows, ico=ico))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}.zip"'
        },
    )


@app.get("/api/export/pohoda.xml")
def export_pohoda():
    groups = _group_by_pohoda_ico(db.list_invoices(LOCAL_USER))
    return _export_response(groups, build_pohoda_xml, "pohoda-invoices")


@app.get("/api/export/pohoda-bank.xml")
def export_pohoda_bank():
    groups = _group_by_pohoda_ico(db.list_bank_statements(LOCAL_USER))
    return _export_response(groups, build_pohoda_bank_xml, "pohoda-bank")
