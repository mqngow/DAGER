-- ==============================================================================
-- DAGER Supabase Security Hardening Script
-- Run this script once in the Supabase SQL Editor as the database owner
-- (`postgres`) AFTER DAGER's tables have been initialized.
--
-- What this script enforces:
-- 1. Enables Row Level Security (RLS) on all 7 tables in `public` with no
--    permissive policies, blocking direct PostgREST Data API reads/writes.
-- 2. Revokes all privileges from `anon` and `authenticated` roles.
-- 3. Creates two least-privilege login roles so Cloud Run and GitHub Actions
--    never connect as the `postgres` owner/superuser:
--      - `dager_web_app`: SELECT/INSERT/UPDATE on all 7 tables + DELETE on
--        `user_tickers` (needed only for history cap eviction). Zero DDL/DROP.
--      - `dager_pipeline`: SELECT/INSERT/UPDATE on ONLY the 5 market tables
--        (zero access to `users` or `user_tickers`).
-- ==============================================================================

-- 1. Enable Row Level Security (default deny for Supabase Data API roles)
ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.financial_statements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stock_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sentiment_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evaluation_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_tickers ENABLE ROW LEVEL SECURITY;

-- 2. Revoke direct table and sequence access from public/API roles
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated, PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated, PUBLIC;

-- Prevent future tables/sequences created in `public` from granting defaults
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated, PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated, PUBLIC;

-- 3. Create Least-Privilege Role for FastAPI Web App (Cloud Run)
-- Replace 'REPLACE_WITH_STRONG_WEB_PASSWORD' before running!
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dager_web_app') THEN
        CREATE ROLE dager_web_app LOGIN PASSWORD 'REPLACE_WITH_STRONG_WEB_PASSWORD' NOINHERIT;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO dager_web_app;
GRANT SELECT, INSERT, UPDATE ON
    public.companies,
    public.financial_statements,
    public.stock_prices,
    public.sentiment_records,
    public.evaluation_results,
    public.users
TO dager_web_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_tickers TO dager_web_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dager_web_app;

-- 4. Create Isolated Least-Privilege Role for Daily S&P 500 Pipeline (GitHub Actions)
-- Note: Zero permissions on `public.users` or `public.user_tickers`.
-- Replace 'REPLACE_WITH_STRONG_PIPELINE_PASSWORD' before running!
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dager_pipeline') THEN
        CREATE ROLE dager_pipeline LOGIN PASSWORD 'REPLACE_WITH_STRONG_PIPELINE_PASSWORD' NOINHERIT;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO dager_pipeline;
GRANT SELECT, INSERT, UPDATE ON
    public.companies,
    public.financial_statements,
    public.stock_prices,
    public.sentiment_records,
    public.evaluation_results
TO dager_pipeline;
GRANT USAGE, SELECT ON SEQUENCE
    public.companies_id_seq,
    public.financial_statements_id_seq,
    public.stock_prices_id_seq,
    public.sentiment_records_id_seq,
    public.evaluation_results_id_seq
TO dager_pipeline;

-- 5. Verification Queries
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND grantee IN ('anon', 'authenticated', 'dager_web_app', 'dager_pipeline')
ORDER BY grantee, table_name, privilege_type;
