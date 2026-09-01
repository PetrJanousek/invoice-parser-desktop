"""Check GitHub Releases for a newer packaged build, and apply it on Windows.

The app's own version lives in the ``VERSION`` file at the repo root, bundled
alongside ``web/`` by ``desktop_app.spec`` (same ``BASE_DIR``-relative
resolution as ``app/main.py``'s ``INDEX``, so it resolves the same way in a
source checkout and inside a frozen PyInstaller bundle). Release publishing
is CI's job (see ``.github/workflows/desktop-build.yml``'s ``release`` job) —
this module only ever reads the public GitHub Releases API, no auth needed.
"""
import json
import platform
import subprocess
import tempfile
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
