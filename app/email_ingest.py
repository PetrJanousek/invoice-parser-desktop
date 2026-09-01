"""Email ingestion over IMAP for Gmail and Seznam.cz.

The "Read emails now" flow logs in with the user's stored (encrypted)
credentials, scans up to ``cap`` UNSEEN messages, extracts every PDF
attachment, and runs the same extraction pipeline the web upload uses.
Successfully-scanned messages are marked \\Seen so they aren't re-imported.

No auto-reading happens here — this is only invoked on an explicit request
(button click) unless the user turns on the auto-poll setting elsewhere.
"""
import email
import imaplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message

from .crypto import decrypt
from .extract import PdfTextError, extract_invoice
from .schemas import FIELDS

# provider -> (IMAP host, port). Both use implicit TLS on 993.
PROVIDERS = {
    "gmail": ("imap.gmail.com", 993),
    "seznam": ("imap.seznam.cz", 993),
}


class EmailConfigError(RuntimeError):
    """Bad/missing provider or credentials."""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _pdf_attachments(msg: Message):
    """Yield (filename, bytes) for each PDF attachment in the message."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode(part.get_filename())
        ctype = (part.get_content_type() or "").lower()
        is_pdf = ctype == "application/pdf" or filename.lower().endswith(".pdf")
        if part.get("Content-Disposition") and is_pdf:
            payload = part.get_payload(decode=True)
            if payload:
                yield (filename or "attachment.pdf", payload)


def _blank_row() -> dict:
    return {f: None for f in FIELDS}


def read_unread(email_settings: dict, cap: int) -> dict:
    """Process up to ``cap`` unread emails; return a summary + parsed rows.

    ``email_settings`` is a row from the email_settings table (provider,
    email_address, imap_password_encrypted). Returns:
        {"scanned": n, "parsed": n, "skipped": n, "rows": [...]}
    Each row is ready to insert (extracted fields + source/sender/filename/error).
    """
    provider = (email_settings.get("provider") or "").lower()
    if provider not in PROVIDERS:
        raise EmailConfigError(f"Unsupported email provider: {provider!r}")
    address = email_settings.get("email_address")
    enc = email_settings.get("imap_password_encrypted")
    if not address or not enc:
        raise EmailConfigError("Email address or password not configured.")

    host, port = PROVIDERS[provider]
    password = decrypt(enc)

    rows: list[dict] = []
    scanned = 0
    skipped = 0

    imap = imaplib.IMAP4_SSL(host, port)
    try:
        try:
            imap.login(address, password)
        except imaplib.IMAP4.error as exc:
            raise EmailConfigError(
                f"IMAP login failed for {address}: {exc}. Check the address "
                "and app password, and that IMAP is enabled."
            ) from exc

        imap.select("INBOX")
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK":
            raise EmailConfigError("Could not search the mailbox.")
        ids = data[0].split()[:cap]

        for msg_id in ids:
            scanned += 1
            # PEEK so we don't mark \Seen until we've handled the message.
            typ, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                skipped += 1
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode(msg.get("From"))
            now = datetime.now(timezone.utc).isoformat()

            attachments = list(_pdf_attachments(msg))
            if not attachments:
                skipped += 1
                imap.store(msg_id, "+FLAGS", "\\Seen")
                continue

            for filename, pdf_bytes in attachments:
                meta = {
                    "filename": filename,
                    "source": "email",
                    "sender_email": sender,
                    "created_at": now,
                    "error": None,
                }
                try:
                    # One attachment may hold several invoices -> several rows.
                    for inv in extract_invoice(pdf_bytes):
                        rows.append({**_blank_row(), **meta, **inv})
                except PdfTextError as exc:
                    rows.append({**_blank_row(), **meta, "error": str(exc)})
                except Exception as exc:  # LLM / parsing failure
                    rows.append(
                        {**_blank_row(), **meta, "error": f"Extraction failed: {exc}"}
                    )

            imap.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    parsed = sum(1 for r in rows if not r.get("error"))
    return {"scanned": scanned, "parsed": parsed, "skipped": skipped, "rows": rows}
