"""Desktop shell: local uvicorn + a native pywebview window."""

import socket
import threading
import time
import urllib.error
import urllib.request

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


def main():
    port = _free_port()
    thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    thread.start()
    _wait_for_server(port)
    webview.create_window(
        "Invoice Parser",
        f"http://127.0.0.1:{port}",
        width=1400,
        height=900,
    )
    webview.start()


if __name__ == "__main__":
    main()
