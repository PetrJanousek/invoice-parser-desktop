-- Migration 006: "report this row for review" + keeping the original PDF.
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- Extraction is imperfect, so a user can flag a row as needing a second look
-- (see POST /api/rows/{id}/report). Reviewing a flagged row means comparing the
-- extracted fields against the document they came from, so every upload now
-- also stores its original PDF in a private Storage bucket and each row keeps
-- the object path in `file_path`. Rows created before this migration simply
-- keep a null `file_path`.

alter table public.invoices
    add column if not exists file_path text,
    add column if not exists reported boolean not null default false,
    add column if not exists reported_at timestamptz;

alter table public.bank_statements
    add column if not exists file_path text,
    add column if not exists reported boolean not null default false,
    add column if not exists reported_at timestamptz;

-- ---------------------------------------------------------------------------
-- Storage: original uploaded PDFs, one object per row at {user_id}/{row_id}.pdf
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('uploaded-files', 'uploaded-files', false)
on conflict (id) do nothing;

-- Same private-per-owner rule as the tables, expressed on the object path: the
-- first path segment is the owner's user id.
drop policy if exists "uploaded files are private to their owner" on storage.objects;
create policy "uploaded files are private to their owner"
    on storage.objects
    for all
    using (
        bucket_id = 'uploaded-files'
        and (storage.foldername(name))[1] = auth.uid()::text
    )
    with check (
        bucket_id = 'uploaded-files'
        and (storage.foldername(name))[1] = auth.uid()::text
    );
