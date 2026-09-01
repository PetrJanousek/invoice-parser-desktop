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
