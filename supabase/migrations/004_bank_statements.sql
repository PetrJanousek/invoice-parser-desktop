-- Migration 004: bank statements (výpisy z účtu).
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- A bank statement is one document with many transactions, so unlike invoices
-- (one flat row per document) it gets its own table with the per-transaction
-- rows kept in a JSONB `transactions` array. Same multi-tenant + RLS pattern
-- as `invoices`.

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
