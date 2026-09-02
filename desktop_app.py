"""Desktop shell: local uvicorn + a native pywebview window."""

import socket
import threading
import time
import urllib.error
import urllib.request
from email.message import Message

import uvicorn
import webview

import app.main


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(port, timeout=5.0):
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if 200 <= response.status < 300:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)


def _run_server(port):
    uvicorn.run(app.main.app, host="127.0.0.1", port=port, log_level="warning")


def _filename_from_headers(headers, fallback):
    """Pull the download filename out of a Content-Disposition header."""
    disposition = headers.get("Content-Disposition") or ""
    if not disposition:
        return fallback
    parsed = Message()
    parsed["Content-Disposition"] = disposition
    return parsed.get_filename() or fallback


def _file_types(filename):
    if filename.lower().endswith(".zip"):
        return ("Zip archive (*.zip)", "All files (*.*)")
    return ("XML file (*.xml)", "All files (*.*)")


class DesktopApi:
    """Methods exposed to the web UI as ``window.pywebview.api.*``."""

    def __init__(self, port):
        self._port = port
        # The leading underscore matters: pywebview builds the JS bridge by
        # walking this object's public attributes and recursing into
        # non-callables, and recursing into a Window blows up — leaving the
        # page with no ``window.pywebview.api`` at all.
        self._window = None

    def save_export(self, path, fallback_name):
        """Fetch an export endpoint and save it wherever the user picks.

        The browser's blob + ``<a download>`` trick doesn't download inside
        the native window — it just navigates to the blob, stranding the user
        on a blank page — so the desktop build routes exports through the
        platform's native save dialog instead.
        """
        url = f"http://127.0.0.1:{self._port}{path}"
        try:
            with urllib.request.urlopen(url) as response:
                data = response.read()
                filename = _filename_from_headers(response.headers, fallback_name)
        except urllib.error.HTTPError as exc:
            return {"status": "error", "message": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError) as exc:
            return {"status": "error", "message": str(exc)}

        target = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=filename,
            file_types=_file_types(filename),
        )
        # Depending on the platform backend this is None, a string, or a
        # one-element sequence of paths.
        if not target:
            return {"status": "cancelled"}
        if not isinstance(target, str):
            target = target[0]

        try:
            with open(target, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            return {"status": "error", "message": str(exc)}
        return {"status": "saved", "path": target}


def main():
    port = _free_port()
    thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    thread.start()
    _wait_for_server(port)
    api = DesktopApi(port)
    api._window = webview.create_window(
        "Invoice Parser",
        f"http://127.0.0.1:{port}",
        width=1400,
        height=900,
        js_api=api,
    )
    webview.start()


if __name__ == "__main__":
    main()
