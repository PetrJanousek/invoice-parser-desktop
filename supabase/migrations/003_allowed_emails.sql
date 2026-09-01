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
