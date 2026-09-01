# Invoice Parser (cloud-deployable)

Local-first invoice parser, rebuilt to run as a hosted, multi-tenant web app.
Users log in, upload PDF invoices (or pull them from an email inbox), a local or
cloud LLM extracts the fields, and results land in an editable table. Czech
invoices (IČO / variabilní symbol / bank account) and Pohoda XML export are
supported.

- **Backend:** FastAPI (Python)
- **LLM:** LangChain, provider-switchable — `anthropic` | `openai` | `ollama`
- **DB + Auth:** Supabase (Postgres + Auth), multi-tenant with Row-Level Security
- **Host:** Render free tier (100-min request limit handles the email batch)

Same code runs locally and on Render — only environment variables differ.

## Layout

```
app/            FastAPI app, LLM abstraction, extraction, email ingest, Pohoda
web/            single-page UI (login, editable table, settings)
evals/          extraction eval cases + runner
tests/          pytest (Pohoda unit tests + extraction eval threshold)
supabase/       schema.sql (tables + RLS)
render.yaml     Render blueprint
```

## Run locally

Requires [uv](https://github.com/astral-sh/uv). For the local LLM, install
[Ollama](https://ollama.com) and pull a model (`ollama pull qwen2.5:7b`).

```bash
cd invoice-parser
uv venv --python 3.12
uv pip install -r requirements.txt

cp .env.example .env
# Generate an encryption key and paste it into .env (APP_ENCRYPTION_KEY):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

uv run uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

With `LLM_PROVIDER=ollama` (the default) no API keys are needed. The app boots
without Supabase, but login and stored data need a Supabase project (below).

### Supabase setup (needed for login + storage)

1. Create a free project at <https://supabase.com>.
2. In **SQL Editor**, run `supabase/schema.sql`.
3. In **Project Settings → API**, copy into `.env`:
   - Project URL → `SUPABASE_URL`
   - `anon` public key → `SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_KEY`
4. Restart the app, create an account on the login screen, and sign in.
   (Tokens are validated against Supabase's auth server, so no JWT secret is
   needed and it works with any project signing configuration.)

Local dev and the deployed app can point at the **same** Supabase project, so
behavior is identical in both.

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

Set `LLM_PROVIDER` (and the matching key). Default model per provider:
`anthropic → claude-haiku-4-5`, `openai → gpt-4o-mini`, `ollama → qwen2.5:7b`.
Override with `LLM_MODEL`. **Ollama is dev-only** — production on Render uses a
cloud provider (see `render.yaml`).

## Email ingestion

In **Settings**, connect a **Gmail** or **Seznam.cz** inbox (step-by-step
instructions are shown there). Passwords are encrypted at rest with
`APP_ENCRYPTION_KEY`. Nothing is read automatically unless you enable the
auto-read toggle (off by default) — use the **Read emails now** button, which
scans up to 20 unread messages and parses their PDF attachments.

## Deploy to Render

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo (it reads `render.yaml`).
3. Set the secret env vars in the dashboard: `ANTHROPIC_API_KEY` (or
   `OPENAI_API_KEY`), the three `SUPABASE_*` values, and `APP_ENCRYPTION_KEY`
   (use the **same** key as any existing stored credentials).
4. Deploy. Health check is `GET /api/health`.

Notes: the free tier sleeps after ~15 min idle (~1 min cold start) — hit the URL
a minute before a demo. No OCR yet — scanned/image PDFs are stored with an error.
