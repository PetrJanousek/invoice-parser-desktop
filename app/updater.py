"""Check GitHub Releases for a newer packaged build, and apply it.

The app's own version lives in the ``VERSION`` file at the repo root, bundled
alongside ``web/`` by ``desktop_app.spec`` (same ``BASE_DIR``-relative
resolution as ``app/main.py``'s ``INDEX``, so it resolves the same way in a
source checkout and inside a frozen PyInstaller bundle). Release publishing
is CI's job (see ``.github/workflows/desktop-build.yml``'s ``release`` job) —
this module only ever reads the public GitHub Releases API, no auth needed.
"""
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

REPO = "PetrJanousek/invoice-parser-desktop"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

BASE_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = BASE_DIR / "VERSION"

# Per-platform release asset name (see the release job's `files:` list).
_ASSET_NAMES = {
    "Windows": "InvoiceParserSetup.exe",
    "Darwin": "Invoice Parser-mac.zip",
}

# Avoid hitting the GitHub API on every UI load/reload.
_CACHE_TTL = 3600.0
_cache: Optional[dict] = None
_cache_at: float = 0.0


def get_current_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "0.0.0"


def _parse_version(v: str) -> tuple:
    """Best-effort dotted-int parse; a malformed string sorts as older than anything."""
    try:
        return tuple(int(p) for p in v.strip().lstrip("v").split("."))
    except ValueError:
        return (-1,)


def _fetch_latest_release() -> Optional[dict]:
    """Raw GitHub API response for the latest release, or None on any failure.

    Network/API errors must never break the app — an update check is a nice-
    to-have, not a dependency of the extraction workflow.
    """
    try:
        req = urllib.request.Request(
            LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def check_for_update() -> dict:
    """Compare the current version against the latest GitHub Release.

    Returns a dict that's safe to hand straight to the frontend:
    ``{"current_version", "latest_version", "available", "download_url",
    "html_url"}``. ``download_url``/``latest_version``/``html_url`` are None
    when the check failed or no release exists yet — the frontend just
    doesn't show the update banner in that case.
    """
    global _cache, _cache_at

    current = get_current_version()
    now = time.monotonic()
    if _cache is None or (now - _cache_at) > _CACHE_TTL:
        _cache = _fetch_latest_release()
        _cache_at = now

    release = _cache
    if not release or release.get("draft") or release.get("prerelease"):
        return {
            "current_version": current,
            "latest_version": None,
            "available": False,
            "download_url": None,
            "html_url": None,
        }

    tag = release.get("tag_name") or ""
    latest = tag.lstrip("v")
    asset_name = _ASSET_NAMES.get(platform.system())
    download_url = None
    for asset in release.get("assets") or []:
        if asset.get("name") == asset_name:
            download_url = asset.get("browser_download_url")
            break

    return {
        "current_version": current,
        "latest_version": latest or None,
        "available": bool(latest) and _parse_version(latest) > _parse_version(current),
        "download_url": download_url,
        "html_url": release.get("html_url"),
    }


class UpdateApplyError(RuntimeError):
    """Raised when applying an update fails or isn't supported here."""


def apply_windows_update(download_url: str) -> None:
    """Download the Windows installer and run it as a silent background update.

    Spawns the installer detached (survives after this process exits/is
    closed) with Inno Setup's silent + close/restart-app switches:
    ``/CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`` tell it to close the running
    app (installer.iss sets ``CloseApplications=yes``/``RestartApplications=yes``,
    which is how it finds and closes/reopens us — it detects the running app
    via its open file handles on the exe/dlls being replaced, no extra mutex
    needed on our end). The installer's own EXE launch still triggers one
    Windows UAC consent prompt (Program Files requires admin) — that's a
    Windows security boundary the silent switches don't bypass; the caller
    should tell the user to expect it.

    Only supported on Windows; raises UpdateApplyError otherwise or if the
    download/spawn itself fails.
    """
    if platform.system() != "Windows":
        raise UpdateApplyError("Silent update is only implemented on Windows.")

    dest = Path(tempfile.gettempdir()) / "InvoiceParserSetup-update.exe"
    try:
        urllib.request.urlretrieve(download_url, dest)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateApplyError(f"Could not download the update: {exc}") from exc

    try:
        subprocess.Popen(
            [
                str(dest),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ],
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        raise UpdateApplyError(f"Could not launch the installer: {exc}") from exc


def _current_app_bundle() -> Optional[Path]:
    """The running ``.app`` bundle's path, or None if not running as one.

    True only for a PyInstaller-frozen build launched from inside a
    ``.app`` (the normal end-user case); a raw ``dist/`` onedir binary run
    directly, or ``python desktop_app.py`` in dev, has no bundle to replace.
    """
    if not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def apply_macos_update(download_url: str) -> None:
    """Download the macOS app zip and swap it in, then relaunch and exit.

    macOS doesn't lock a running executable's files the way Windows does, but
    replacing the bundle *while it's still executing* is still asking for a
    half-written .app if something goes wrong mid-copy. So instead: download
    and unzip the new .app into a temp dir, then spawn a small detached shell
    helper that waits for this process to exit, swaps the old bundle for the
    new one, relaunches it, and cleans up — and only then does this process
    exit itself (``os._exit`` after a short delay, so the HTTP response
    announcing "installing" has time to reach the frontend first).

    Only supported when running from a packaged .app; raises UpdateApplyError
    otherwise, or if the download/unzip/spawn itself fails.
    """
    if platform.system() != "Darwin":
        raise UpdateApplyError("This update path is only implemented on macOS.")

    app_bundle = _current_app_bundle()
    if app_bundle is None:
        raise UpdateApplyError(
            "Automatic update only works for the installed app — not the dev server."
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="invoice-parser-update-"))
    zip_path = tmp_dir / "Invoice Parser-mac.zip"
    try:
        urllib.request.urlretrieve(download_url, zip_path)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateApplyError(f"Could not download the update: {exc}") from exc

    extract_dir = tmp_dir / "extracted"
    extract_dir.mkdir()
    try:
        # ditto (not zipfile/unzip) is what CI used to create the zip
        # (--keepParent), and correctly restores bundle metadata on extract.
        subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(extract_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise UpdateApplyError(f"Could not unpack the update: {exc}") from exc

    new_app = extract_dir / app_bundle.name
    if not new_app.exists():
        raise UpdateApplyError("Downloaded update is missing the app bundle.")

    helper_script = tmp_dir / "apply_update.sh"
    helper_script.write_text(
        f"""#!/bin/bash
set -e
# Wait for the running app to exit before touching its bundle.
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done
rm -rf "{app_bundle}"
mv "{new_app}" "{app_bundle}"
open -n "{app_bundle}"
rm -rf "{tmp_dir}"
"""
    )
    helper_script.chmod(0o755)

    try:
        subprocess.Popen(
            ["/bin/bash", str(helper_script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateApplyError(f"Could not launch the updater: {exc}") from exc

    # Give the HTTP response time to flush back to the frontend before this
    # process disappears out from under it; os._exit is a hard exit that
    # works regardless of pywebview's blocking main thread.
    threading.Timer(1.0, lambda: os._exit(0)).start()


def apply_update(download_url: str) -> None:
    """Dispatch to the current platform's silent-update implementation."""
    system = platform.system()
    if system == "Windows":
        apply_windows_update(download_url)
    elif system == "Darwin":
        apply_macos_update(download_url)
    else:
        raise UpdateApplyError(f"Automatic update isn't supported on {system}.")
