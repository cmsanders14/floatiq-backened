-- Reconcile tables that existed before the tracked FloatIQ migrations and make
-- the FastAPI service the only data path for tiered/proprietary features.
-- This migration is additive: it preserves existing rows and legacy columns.

-- Legacy projects may already have this table, in which case CREATE TABLE IF
-- NOT EXISTS in 000 does not add its constraints or current entitlement field.
alter table public.user_subscriptions
    add column if not exists max_charts integer not null default 2;

alter table public.user_subscriptions
    alter column max_charts set default 2;

update public.user_subscriptions
set max_charts = case tier_level
    when 'premium_scanner' then 4
    when 'autonomous_bot' then 6
    else 2
end
where max_charts is distinct from case tier_level
    when 'premium_scanner' then 4
    when 'autonomous_bot' then 6
    else 2
end;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'user_subscriptions_user_id_fkey'
          and conrelid = 'public.user_subscriptions'::regclass
    ) then
        alter table public.user_subscriptions
            add constraint user_subscriptions_user_id_fkey
            foreign key (user_id) references auth.users(id) on delete cascade
            not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'user_subscriptions_tier_level_check'
          and conrelid = 'public.user_subscriptions'::regclass
    ) then
        alter table public.user_subscriptions
            add constraint user_subscriptions_tier_level_check
            check (tier_level in ('free', 'premium_scanner', 'autonomous_bot'))
            not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'user_subscriptions_max_charts_check'
          and conrelid = 'public.user_subscriptions'::regclass
    ) then
        alter table public.user_subscriptions
            add constraint user_subscriptions_max_charts_check
            check (max_charts in (2, 4, 6))
            not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'user_trade_journal_user_id_fkey'
          and conrelid = 'public.user_trade_journal'::regclass
    ) then
        alter table public.user_trade_journal
            add constraint user_trade_journal_user_id_fkey
            foreign key (user_id) references auth.users(id) on delete cascade
            not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'user_ticker_history_user_id_fkey'
          and conrelid = 'public.user_ticker_history'::regclass
    ) then
        alter table public.user_ticker_history
            add constraint user_ticker_history_user_id_fkey
            foreign key (user_id) references auth.users(id) on delete cascade
            not valid;
    end if;

    -- user_watchlists predates the repository migrations and may not exist in a
    -- clean installation, so reconcile it only when present.
    if to_regclass('public.user_watchlists') is not null
       and not exists (
           select 1 from pg_constraint
           where conname = 'user_watchlists_user_id_fkey'
             and conrelid = 'public.user_watchlists'::regclass
       ) then
        alter table public.user_watchlists
            add constraint user_watchlists_user_id_fkey
            foreign key (user_id) references auth.users(id) on delete cascade
            not valid;
    end if;
end;
$$;

alter table public.user_subscriptions
    validate constraint user_subscriptions_user_id_fkey;
alter table public.user_subscriptions
    validate constraint user_subscriptions_tier_level_check;
alter table public.user_subscriptions
    validate constraint user_subscriptions_max_charts_check;
alter table public.user_trade_journal
    validate constraint user_trade_journal_user_id_fkey;
alter table public.user_ticker_history
    validate constraint user_ticker_history_user_id_fkey;

do $$
begin
    if to_regclass('public.user_watchlists') is not null then
        alter table public.user_watchlists
            validate constraint user_watchlists_user_id_fkey;
    end if;
end;
$$;

-- Remove legacy direct-read policies that expose full scanner statistics to
-- authenticated clients. Tier checks and redaction are enforced in FastAPI.
drop policy if exists "Authenticated users can view scan cache"
    on public.live_scan_cache;

do $$
begin
    if to_regclass('public.historical_setups') is not null then
        execute 'drop policy if exists "Authenticated users can view historical stats" on public.historical_setups';
    end if;
    if to_regclass('public.pattern_metadata') is not null then
        execute 'drop policy if exists "Authenticated users can view pattern meta" on public.pattern_metadata';
    end if;
end;
$$;

-- Supabase projects historically granted broad Data API privileges to anon and
-- authenticated. RLS still blocks tables with no policy, but revoking grants is
-- deliberate defense in depth and prevents a future permissive-policy mistake.
do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'user_subscriptions',
        'live_scan_cache',
        'user_trade_journal',
        'user_ticker_history',
        'user_watchlists',
        'historical_setups',
        'pattern_metadata',
        'daily_feature_usage',
        'market_alert_events',
        'user_alert_rules',
        'market_analog_profiles',
        'user_discipline_settings',
        'discipline_evaluations',
        'trade_process_reviews',
        'user_legal_acceptances',
        'supernova_scan_cache',
        'user_supernova_settings',
        'broker_connections',
        'broker_order_intents',
        'broker_order_events'
    ] loop
        if to_regclass(format('public.%I', table_name)) is not null then
            execute format(
                'revoke all privileges on table public.%I from anon, authenticated',
                table_name
            );
        end if;
    end loop;
end;
$$;

-- The legacy profile table contains subscription, payment, and legal flags.
-- Clients may read their own non-payment fields but may not edit authoritative
-- account state directly.
do $$
begin
    if to_regclass('public.users_profiles') is not null then
        execute 'drop policy if exists "Users can update their own profile" on public.users_profiles';
        execute 'drop policy if exists "Users can view their own profile" on public.users_profiles';
        execute 'revoke all privileges on table public.users_profiles from anon, authenticated';
        execute 'grant select (id, email, subscription_tier, advisory_accepted, execution_risk_accepted, terms_signed_at, created_at, updated_at) on table public.users_profiles to authenticated';
        execute 'create policy "Users can view their own profile" on public.users_profiles for select to authenticated using ((select auth.uid()) = id)';
    end if;
end;
$$;

-- Trigger functions do not need to be callable through the Data API.
revoke all on function public.set_updated_at() from public, anon, authenticated;
grant execute on function public.set_updated_at() to service_role;

-- Keep future tables private by default. Explicit grants should accompany any
-- intentionally supported direct-client API surface.
alter default privileges for role postgres in schema public
    revoke all on tables from anon, authenticated;

