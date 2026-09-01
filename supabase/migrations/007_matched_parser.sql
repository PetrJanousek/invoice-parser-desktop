-- Migration 007: which deterministic parser handled a bank statement.
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- A statement from a bank app/bank_parsers.py knows is parsed by coordinates:
-- the bank key it matched ('mbank' | 'raiffeisen' | 'ceska_sporitelna', see
-- detect_bank) is stored here. A statement that had to fall back to an LLM
-- leaves this NULL — that is the self-improvement signal:
--
--   -- statements whose bank layout we cannot parse deterministically yet:
--   select id, filename, account_number, created_at
--   from public.bank_statements
--   where matched_parser is null
--   order by created_at desc;
--
-- Each such file is a candidate for a new coordinate parser.

alter table public.bank_statements
    add column if not exists matched_parser text;
