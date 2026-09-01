-- Invoice Parser — Supabase schema
-- Run this in the Supabase SQL Editor (Dashboard → SQL → New query) once,
-- after creating your project.
--
-- Multi-tenant: every row belongs to a user_id (auth.users). Row-Level
-- Security ensures a user can only ever see/modify their own rows when
-- accessed with their JWT. The server also filters by user_id explicitly.

-- ---------------------------------------------------------------------------
-- invoices
-- ---------------------------------------------------------------------------
create table if not exists public.invoices (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users (id) on delete cascade,
    filename        text,
    source          text,                 -- 'upload' | 'email'
    sender_email    text,                 -- only for emailed invoices
    document_type   text,                 -- 'invoice' | 'receipt'
    vendor          text,                 -- Dodavatel (who issued)
    ico             text,                 -- Dodavatel IČO
    customer        text,                 -- Odběratel (billed to)
    customer_ico    text,                 -- Odběratel IČO
    invoice_number  text,
    variable_symbol text,
    invoice_date    text,
    due_date        text,
    currency        text,
    subtotal        text,
    tax             text,
    total           text,
    bank_account    text,
    error           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz
);

create index if not exists invoices_user_id_created_at_idx
    on public.invoices (user_id, created_at);

alter table public.invoices enable row level security;

drop policy if exists "invoices are private to their owner" on public.invoices;
create policy "invoices are private to their owner"
    on public.invoices
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- email_settings (one row per user)
-- ---------------------------------------------------------------------------
create table if not exists public.email_settings (
    user_id                 uuid primary key references auth.users (id) on delete cascade,
    provider                text,          -- 'gmail' | 'seznam'
    email_address           text,
    imap_password_encrypted text,          -- Fernet token, never plaintext
    auto_poll               boolean not null default false,
    last_polled_at          timestamptz,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz
);

alter table public.email_settings enable row level security;

drop policy if exists "email settings are private to their owner" on public.email_settings;
create policy "email settings are private to their owner"
    on public.email_settings
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- bank_statements (one row per statement; transactions in a JSONB array)
-- ---------------------------------------------------------------------------
create table if not exists public.bank_statements (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references auth.users (id) on delete cascade,
    filename         text,
    source           text,                 -- 'upload' | 'email'
    sender_email     text,                 -- only for emailed statements
    account_number   text,                 -- the statement's own account
    statement_number text,
    currency         text,
    period_start     text,
    period_end       text,
    opening_balance  text,
    closing_balance  text,
    transactions     jsonb not null default '[]'::jsonb,  -- list of movements
    matched_parser   text,                 -- bank key of the deterministic
                                           -- parser that handled it; NULL when
                                           -- an LLM had to (unknown layout)
    error            text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz
);

create index if not exists bank_statements_user_id_created_at_idx
    on public.bank_statements (user_id, created_at);

alter table public.bank_statements enable row level security;

drop policy if exists "bank statements are private to their owner" on public.bank_statements;
create policy "bank statements are private to their owner"
    on public.bank_statements
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- reported_documents (permanent snapshot of a row flagged for review)
-- ---------------------------------------------------------------------------
-- Written when a row is reported, never touched when it is un-reported: an
-- append-only audit log that outlives the live row and pins its source PDF.
create table if not exists public.reported_documents (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references auth.users (id) on delete cascade,
    kind             text not null,        -- 'invoice' | 'bank_statement'
    original_row_id  uuid not null,        -- no FK: outlives the original row
    file_path        text,                 -- Storage object of the source PDF
    extracted_data   jsonb not null default '{}'::jsonb,  -- row at report time
    reported_at      timestamptz not null default now(),
    created_at       timestamptz not null default now()
);

create index if not exists reported_documents_user_id_created_at_idx
    on public.reported_documents (user_id, created_at);

alter table public.reported_documents enable row level security;

drop policy if exists "reported documents are private to their owner" on public.reported_documents;
create policy "reported documents are private to their owner"
    on public.reported_documents
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Migration 003: email allowlist (closed signup).
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- Signup in Supabase stays open, but the backend refuses any request whose
-- user email is not in this table (see app/auth.py). The admin manages the
-- list here, by hand:
--
--   -- approve an email (store it LOWERCASE):
--   insert into public.allowed_emails (email, note)
--   values ('lower@case.com', 'Acme trial')
--   on conflict (email) do nothing;
--
--   -- revoke an email:
--   delete from public.allowed_emails where email = 'lower@case.com';
--
--   -- list who is approved:
--   select email, note, added_at from public.allowed_emails order by added_at;

create table if not exists public.allowed_emails (
    email     text primary key,   -- store lowercased; backend queries lowercased
    note      text,               -- free-form admin note (e.g. which customer)
    added_at  timestamptz not null default now()
);

-- RLS on with NO policy: only the service-role key (used by our backend) can
-- read this table. anon/authenticated users get nothing, so the allowlist is
-- never exposed to the browser.
alter table public.allowed_emails enable row level security;

-- ---------------------------------------------------------------------------
-- Grant this account access (closed-signup allowlist). Email must be lowercase.
-- ---------------------------------------------------------------------------
insert into public.allowed_emails (email, note)
values ('petejanousek@gmail.com', 'owner')
on conflict (email) do nothing;
