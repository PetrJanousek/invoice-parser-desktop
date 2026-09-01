-- Migration 002: document type + customer (Odběratel) party columns.
-- Run this in the Supabase SQL Editor if your `invoices` table was created
-- before these columns existed (i.e. from the original schema.sql). Safe to
-- run more than once.

alter table public.invoices add column if not exists document_type text;  -- 'invoice' | 'receipt'
alter table public.invoices add column if not exists customer      text;  -- Odběratel (billed to)
alter table public.invoices add column if not exists customer_ico  text;  -- Odběratel IČO
