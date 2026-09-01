# Invoice Parser (desktop)

Local-first invoice parser desktop app. Upload PDF invoices and receipts (or
pull them from an email inbox), a local or cloud LLM extracts the fields, and
results land in an editable table. Czech invoices (IČO / variabilní symbol /
bank account), Pohoda XML export, and bank statement parsing are supported.

- **Backend:** FastAPI (Python)
- **UI:** pywebview native window over the web UI
- **Storage:** SQLite on disk (no cloud database)
- **LLM:** LangChain, provider-switchable — `anthropic` | `openai` | `google` | `ollama`

Everything runs on your machine.

## Layout

```
app/                  FastAPI backend, LLM abstraction, extraction, email ingest, Pohoda
web/                  single-page UI (editable table, settings)
desktop_app.py        pywebview entry point
desktop_app.spec      PyInstaller build spec
evals/                extraction eval cases + runner
tests/                pytest
.github/workflows/    CI desktop builds (macOS + Windows)
```

## Run locally

Requires [uv](https://github.com/astral-sh/uv). The default LLM provider is
Anthropic (Claude Haiku), which needs an API key — set it either in `.env` or
from the app's **Settings** screen after first launch (no restart needed).
For a free local LLM instead, install [Ollama](https://ollama.com), pull a
model (`ollama pull qwen2.5:7b`), and switch the provider to `ollama` (via
`.env` or Settings).

```bash
uv venv --python 3.12
uv pip install -r requirements.txt

cp .env.example .env
# .env is optional — you can also set the provider and API key from the
# app's Settings screen instead of editing this file.

uv run python desktop_app.py
```

That launches a native window. For backend-only dev/debugging, run the API
in a browser instead:

```bash
uv run uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

With `LLM_PROVIDER=anthropic` (the default) you need an `ANTHROPIC_API_KEY`
— set it in `.env`, or leave it blank and paste the key into the app's
Settings screen instead. `LLM_PROVIDER=ollama` needs no API key.

## Building the packaged app

```bash
pip install pyinstaller
pyinstaller desktop_app.spec
```

This is a **onedir** build (a folder, not a single file):

- macOS: `dist/Invoice Parser.app`
- Windows: `dist/Invoice Parser/Invoice Parser.exe`

The GitHub Actions workflow (`.github/workflows/desktop-build.yml`) builds both
platforms on push to `main` and on manual dispatch, then uploads the artifacts.

### Windows installer

The raw onedir folder above still needs to be unzipped and run in place. For a
proper "download, double-click, done" installer, `installer.iss` (an
[Inno Setup](https://jrsoftware.org/isinfo.php) script) wraps that build into a
single `InvoiceParserSetup.exe`: it installs to Program Files, adds a Start
Menu shortcut (and an optional Desktop shortcut), and registers a normal
Windows uninstaller. Build it (on Windows, after the PyInstaller step above)
with Inno Setup's compiler:

```
iscc installer.iss
```

producing `installer_output/InvoiceParserSetup.exe`. CI builds this
automatically on the `windows-latest` job and uploads it as the
`invoice-parser-windows-installer` artifact — that's the file to hand to a
Windows user: they run it once to install, then launch "Invoice Parser" from
the Start Menu or its shortcut like any other app.

### Cutting a release

The app's version lives in the `VERSION` file at the repo root (a bare
`X.Y.Z`, no `v` prefix) — it's baked into the build (macOS `Info.plist`,
the Windows installer) and is what the in-app update check compares
against. To publish a new release:

1. Bump `VERSION` (e.g. `1.0.0` → `1.1.0`) and commit it.
2. Tag that commit `v1.1.0` (matching `VERSION` exactly, `v` prefix on the
   tag only) and push the tag: `git tag v1.1.0 && git push origin v1.1.0`.

Pushing a `v*.*.*` tag triggers `.github/workflows/desktop-build.yml`'s
`release` job, which re-checks the tag matches `VERSION` (fails the build
otherwise, so a forgotten version bump can't ship), then builds both
platforms and publishes a GitHub Release with `InvoiceParserSetup.exe` and
`Invoice-Parser-mac.zip` attached — the assets the update check downloads.

## Data storage

Local data lives under the OS app-data directory (via `platformdirs`):

- macOS: `~/Library/Application Support/InvoiceParser`
- Windows: `%APPDATA%\InvoiceParser`

That directory holds the SQLite database, uploaded PDFs, and the auto-generated
encryption key (`secret.key`). Override the location with the `DATA_DIR` env var.

## Evals

```bash
uv run python evals/run_evals.py         # prints per-case + overall field accuracy
uv run pytest                             # Pohoda unit tests + eval threshold
```

Cases live in `evals/cases/*.json` (`text` + expected fields). Add more by
dropping in new JSON files. The eval test skips automatically if no LLM backend
is reachable. With local `qwen2.5:7b` we see ~90%+ field accuracy; cloud Haiku
should be higher.

## Switching the LLM

The easiest way is the app's **Settings** screen: pick a provider, paste an
API key, and it takes effect immediately — no `.env` edit, no restart.

Alternatively, set `LLM_PROVIDER` (and the matching key) in `.env`. Default
provider is `anthropic` (Claude Haiku). Default model per provider:
`anthropic → claude-haiku-4-5`, `openai → gpt-4o-mini`,
`google → gemini-flash-latest`, `ollama → qwen2.5:7b`.
Override with `LLM_MODEL`. Ollama needs no API key.

## Email ingestion

In **Settings**, connect a **Gmail** or **Seznam.cz** inbox (step-by-step
instructions are shown there). Passwords are encrypted at rest with a Fernet
key that the app generates and stores in `secret.key` on first use. Nothing is
read automatically unless you enable the auto-read toggle (off by default) —
use the **Read emails now** button, which scans up to 20 unread messages and
parses their PDF attachments.
