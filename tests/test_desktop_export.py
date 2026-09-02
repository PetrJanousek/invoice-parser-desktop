"""Tests for the desktop shell's native save-dialog export bridge."""
import io
import urllib.error

import pytest

import desktop_app


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers):
        super().__init__(data)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeWindow:
    """Stands in for the pywebview window's native file dialog."""

    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create_file_dialog(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.result


@pytest.fixture
def api(monkeypatch):
    def fake_urlopen(url):
        assert url == "http://127.0.0.1:1234/api/export/pohoda.xml"
        return FakeResponse(
            b"<dataPack/>",
            {"Content-Disposition": 'attachment; filename="pohoda-invoices.xml"'},
        )

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)
    return desktop_app.DesktopApi(1234)


def test_save_writes_the_file_the_user_picked(api, tmp_path):
    target = tmp_path / "chosen.xml"
    api._window = FakeWindow(str(target))

    result = api.save_export("/api/export/pohoda.xml", "fallback.xml")

    assert result == {"status": "saved", "path": str(target)}
    assert target.read_bytes() == b"<dataPack/>"
    # The dialog is pre-filled with the server's Content-Disposition name.
    assert api._window.kwargs["save_filename"] == "pohoda-invoices.xml"


def test_save_handles_a_dialog_that_returns_a_sequence(api, tmp_path):
    target = tmp_path / "chosen.xml"
    api._window = FakeWindow((str(target),))

    assert api.save_export("/api/export/pohoda.xml", "fallback.xml")["status"] == "saved"
    assert target.read_bytes() == b"<dataPack/>"


def test_cancelled_dialog_writes_nothing(api):
    api._window = FakeWindow(None)
    assert api.save_export("/api/export/pohoda.xml", "fallback.xml") == {
        "status": "cancelled"
    }


def test_failed_fetch_reports_an_error(monkeypatch):
    def fake_urlopen(url):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)
    api = desktop_app.DesktopApi(1234)
    api._window = FakeWindow("/nowhere/out.xml")

    assert api.save_export("/api/export/pohoda.xml", "fallback.xml") == {
        "status": "error",
        "message": "HTTP 500",
    }


def test_filename_falls_back_when_the_header_is_missing():
    assert desktop_app._filename_from_headers({}, "fallback.xml") == "fallback.xml"


def test_zip_exports_offer_a_zip_file_type():
    assert desktop_app._file_types("pohoda-invoices.zip")[0].startswith("Zip")
    assert desktop_app._file_types("pohoda-invoices.xml")[0].startswith("XML")
