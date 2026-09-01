# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Invoice Parser desktop shell.

Builds a onedir bundle (not onefile) for fast startup and easy debugging.
Entry point is desktop_app.py, which runs the FastAPI app (app/main.py)
under uvicorn in a background thread and shows it in a pywebview window.
"""

import sys
from pathlib import Path

block_cipher = None

# SPECPATH is injected by PyInstaller as the directory containing this spec
# file (repo root) — read the app version once here so it's available both
# for the [Files]/datas bundling below and the macOS Info.plist.
APP_VERSION = (Path(SPECPATH) / "VERSION").read_text().strip()

# uvicorn's loop/protocol/lifespan implementations are picked dynamically at
# runtime ("auto" selects uvloop/asyncio, h11/httptools, etc.), which
# PyInstaller's static analysis can miss. The langchain provider packages
# are only imported when the user actually selects that LLM provider, so a
# missing hidden import wouldn't be caught by a quick smoke test — list them
# explicitly rather than discover it at runtime.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_google_genai",
    "langchain_ollama",
]

a = Analysis(
    ["desktop_app.py"],
    pathex=[],
    binaries=[],
    # web/index.html lands at <bundle root>/web/index.html, which is the
    # same root app/main.py's BASE_DIR resolves to via
    # Path(__file__).resolve().parent.parent when frozen (both the frozen
    # "app" package and PyInstaller datas live under sys._MEIPASS in onedir
    # mode), so no change to app/main.py's path resolution is needed.
    datas=[("web", "web"), ("VERSION", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Invoice Parser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Invoice Parser",
)

# macOS gets a proper .app bundle. No .icns exists in the repo, so the
# bundle uses PyInstaller's default icon rather than inventing one.
# Windows' onedir COLLECT output (dist/Invoice Parser/Invoice Parser.exe)
# needs no extra bundling step.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Invoice Parser.app",
        icon=None,
        bundle_identifier="com.invoiceparser.desktop",
        info_plist={
            "CFBundleName": "Invoice Parser",
            "CFBundleDisplayName": "Invoice Parser",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": "True",
        },
    )
