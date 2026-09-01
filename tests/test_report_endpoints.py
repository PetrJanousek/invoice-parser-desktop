"""Tests for the report-for-review endpoints and source-PDF storage (app.main)."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import User, current_user

USER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client():
    main.app.dependency_overrides[current_user] = lambda: User(
        id=USER_ID, email="tester@example.com"
    )
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def stub_snapshots(monkeypatch):
    """Reporting also writes a reported_documents snapshot; see
    tests/test_reported_documents.py. It is irrelevant to the flag itself."""
    monkeypatch.setattr(
        main.db, "insert_reported_document", lambda **kwargs: {"id": "snap"}
    )


def _capture(monkeypatch, name, result):
    """Replace a db update function, recording the args it was called with."""
    calls = []

    def fake(user_id, row_id, fields):
        calls.append((user_id, row_id, fields))
        return result

    monkeypatch.setattr(main.db, name, fake)
    return calls


def test_report_invoice_sets_flag_and_timestamp(client, monkeypatch):
    calls = _capture(monkeypatch, "update_invoice", {"id": "abc", "reported": True})
    resp = client.post("/api/rows/abc/report")
    assert resp.status_code == 200
    assert resp.json()["reported"] is True
    user_id, row_id, fields = calls[0]
    assert (user_id, row_id) == (USER_ID, "abc")
    assert fields["reported"] is True
    assert fields["reported_at"] is not None


def test_unreport_invoice_clears_timestamp(client, monkeypatch):
    calls = _capture(monkeypatch, "update_invoice", {"id": "abc", "reported": False})
    resp = client.post("/api/rows/abc/report", json={"reported": False})
    assert resp.status_code == 200
    fields = calls[0][2]
    assert fields["reported"] is False
    assert fields["reported_at"] is None


def test_report_unknown_invoice_is_404(client, monkeypatch):
    _capture(monkeypatch, "update_invoice", None)
    assert client.post("/api/rows/nope/report").status_code == 404


def test_report_bank_statement_sets_flag(client, monkeypatch):
    calls = _capture(
        monkeypatch, "update_bank_statement", {"id": "st1", "reported": True}
    )
    resp = client.post("/api/bank-statements/st1/report")
    assert resp.status_code == 200
    user_id, row_id, fields = calls[0]
    assert (user_id, row_id) == (USER_ID, "st1")
    assert fields["reported"] is True


def test_report_unknown_bank_statement_is_404(client, monkeypatch):
    _capture(monkeypatch, "update_bank_statement", None)
    assert client.post("/api/bank-statements/nope/report").status_code == 404


# --- source PDF storage ----------------------------------------------------

def test_store_source_pdf_shares_one_object_across_rows(monkeypatch):
    uploads = []
    monkeypatch.setattr(
        main.db,
        "upload_source_file",
        lambda uid, rid, data: uploads.append((uid, rid, data))
        or f"{uid}/{rid}.pdf",
    )
    rows = [{"id": "a"}, {"id": "b"}]
    updates = []

    def update(user_id, row_id, fields):
        updates.append((row_id, fields))
        return {"id": row_id, **fields}

    main._store_source_pdf(USER_ID, rows, b"%PDF-1.4", update)

    assert len(uploads) == 1
    assert uploads[0][1] == "a"
    assert [r["file_path"] for r in rows] == [f"{USER_ID}/a.pdf"] * 2
    assert [u[0] for u in updates] == ["a", "b"]


def test_store_source_pdf_survives_storage_failure(monkeypatch):
    def boom(user_id, row_id, data):
        raise RuntimeError("bucket missing")

    monkeypatch.setattr(main.db, "upload_source_file", boom)
    rows = [{"id": "a"}]

    def update(user_id, row_id, fields):  # pragma: no cover - must not be reached
        raise AssertionError("rows must not be updated when storage fails")

    main._store_source_pdf(USER_ID, rows, b"%PDF-1.4", update)
    assert "file_path" not in rows[0]


def test_store_source_pdf_noop_without_rows(monkeypatch):
    monkeypatch.setattr(
        main.db,
        "upload_source_file",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not upload")),
    )
    main._store_source_pdf(USER_ID, [], b"%PDF-1.4", lambda *a: None)
