-- Migration 005: per-row Pohoda IČO.
-- Run this in the Supabase SQL Editor once. Safe to run more than once.
--
-- Pohoda's XML import needs the target accounting unit's IČO on every export
-- (see app/pohoda.py). It varies per row (different companies), so it's a
-- manually-entered column rather than a single global setting: users fill it
-- in themselves after parsing, then export groups rows by this value.

alter table public.invoices
    add column if not exists pohoda_ico text;

alter table public.bank_statements
    add column if not exists pohoda_ico text;
