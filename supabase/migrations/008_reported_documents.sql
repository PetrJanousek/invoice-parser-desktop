-- Migration 008: permanent snapshots of reported rows.
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- Reporting a row (see POST /api/rows/{id}/report) means "this extraction looks
-- wrong, keep it for review". The live row can still be edited or deleted
-- afterwards, so the snapshot is taken at report time — capturing it later
-- would preserve an already-corrected version instead of the bad extraction
-- that prompted the report.
--
-- One table holds both kinds (`kind` = 'invoice' | 'bank_statement') because
-- the snapshot itself lives in `extracted_data` and the two schemas would
-- otherwise be identical. `original_row_id` is deliberately NOT a foreign key:
-- the snapshot must outlive the row it came from.
--
-- The snapshot also pins the source PDF: a row's Storage object is deleted with
-- the row only when nothing here (and no other live row) still points at it.

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
