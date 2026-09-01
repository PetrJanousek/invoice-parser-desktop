"""Tests for report-time snapshots and source-PDF cleanup on delete (app.main)."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import User, current_user

USER_ID = "11111111-1111-1111-1111-111111111111"
FILE_PATH = f"{USER_ID}/abc.pdf"


@pytest.fixture
def client():
    main.app.dependency_overrides[current_user] = lambda: User(
        id=USER_ID, email="tester@example.com"
    )
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def snapshots(monkeypatch):
    """Collect the reported_documents rows the endpoints try to write."""
    written = []
    monkeypatch.setattr(
        main.db,
        "insert_reported_document",
        lambda **kwargs: written.append(kwargs) or {"id": "snap"},
    )
    return written


@pytest.fixture
def deletions(monkeypatch):
    """Collect the Storage paths the endpoints try to delete."""
    deleted = []
    monkeypatch.setattr(main.db, "delete_source_file", deleted.append)
    return deleted


def _stub_delete(monkeypatch, name, row):
    monkeypatch.setattr(main.db, name, lambda user_id, row_id: row)


def _stub_in_use(monkeypatch, in_use):
    calls = []

    def fake(user_id, file_path, exclude_id=None):
        calls.append((user_id, file_path, exclude_id))
        return in_use

    monkeypatch.setattr(main.db, "file_path_in_use", fake)
    return calls


# --- snapshots at report time ----------------------------------------------

def test_reporting_invoice_snapshots_the_row(client, monkeypatch, snapshots):
    row = {"id": "abc", "file_path": FILE_PATH, "vendor": "Acme", "reported": True}
    monkeypatch.setattr(main.db, "update_invoice", lambda u, i, f: row)

    assert client.post("/api/rows/abc/report").status_code == 200

    assert len(snapshots) == 1
    assert snapshots[0]["kind"] == "invoice"
    assert snapshots[0]["original_row_id"] == "abc"
    assert snapshots[0]["file_path"] == FILE_PATH
    assert snapshots[0]["extracted_data"]["vendor"] == "Acme"


def test_reporting_bank_statement_snapshots_the_row(client, monkeypatch, snapshots):
    row = {"id": "st1", "file_path": FILE_PATH, "account_number": "123"}
    monkeypatch.setattr(main.db, "update_bank_statement", lambda u, i, f: row)

    assert client.post("/api/bank-statements/st1/report").status_code == 200

    assert len(snapshots) == 1
    assert snapshots[0]["kind"] == "bank_statement"
    assert snapshots[0]["original_row_id"] == "st1"


def test_unreporting_keeps_the_snapshot(client, monkeypatch, snapshots):
    monkeypatch.setattr(
        main.db, "update_invoice", lambda u, i, f: {"id": "abc", "reported": False}
    )

    resp = client.post("/api/rows/abc/report", json={"reported": False})

    assert resp.status_code == 200
    assert snapshots == []
    # The log is append-only: nothing anywhere can remove a snapshot.
    assert not hasattr(main.db, "delete_reported_document")


# --- file cleanup on delete ------------------------------------------------

def test_delete_keeps_file_that_a_snapshot_pins(client, monkeypatch, deletions):
    _stub_delete(monkeypatch, "delete_invoice", {"id": "abc", "file_path": FILE_PATH})
    _stub_in_use(monkeypatch, True)

    assert client.delete("/api/rows/abc").status_code == 200
    assert deletions == []


def test_delete_keeps_file_shared_with_another_live_row(
    client, monkeypatch, deletions
):
    _stub_delete(monkeypatch, "delete_invoice", {"id": "abc", "file_path": FILE_PATH})
    calls = _stub_in_use(monkeypatch, True)

    assert client.delete("/api/rows/abc").status_code == 200
    assert calls == [(USER_ID, FILE_PATH, "abc")]
    assert deletions == []


def test_delete_removes_unreferenced_file(client, monkeypatch, deletions):
    _stub_delete(monkeypatch, "delete_invoice", {"id": "abc", "file_path": FILE_PATH})
    _stub_in_use(monkeypatch, False)

    assert client.delete("/api/rows/abc").status_code == 200
    assert deletions == [FILE_PATH]


def test_delete_bank_statement_removes_unreferenced_file(
    client, monkeypatch, deletions
):
    _stub_delete(
        monkeypatch, "delete_bank_statement", {"id": "st1", "file_path": FILE_PATH}
    )
    _stub_in_use(monkeypatch, False)

    assert client.delete("/api/bank-statements/st1").status_code == 200
    assert deletions == [FILE_PATH]


def test_delete_without_file_path_skips_storage(client, monkeypatch, deletions):
    _stub_delete(monkeypatch, "delete_invoice", {"id": "abc", "file_path": None})
    monkeypatch.setattr(
        main.db,
        "file_path_in_use",
        lambda *a, **k: pytest.fail("must not look up a missing file"),
    )

    assert client.delete("/api/rows/abc").status_code == 200
    assert deletions == []


def test_delete_survives_storage_failure(client, monkeypatch):
    _stub_delete(monkeypatch, "delete_invoice", {"id": "abc", "file_path": FILE_PATH})
    _stub_in_use(monkeypatch, False)

    def boom(path):
        raise RuntimeError("bucket missing")

    monkeypatch.setattr(main.db, "delete_source_file", boom)

    assert client.delete("/api/rows/abc").status_code == 200


def test_delete_unknown_invoice_is_404(client, monkeypatch):
    _stub_delete(monkeypatch, "delete_invoice", None)
    assert client.delete("/api/rows/nope").status_code == 404
