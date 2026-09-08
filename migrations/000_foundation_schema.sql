-- Core tables used by the original FloatIQ endpoints.
-- Run before numbered feature migrations in a new Supabase project.

create table if not exists public.user_subscriptions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    tier_level text not null default 'free' check (tier_level in (
        'free', 'premium_scanner', 'autonomous_bot'
    )),
    created_at timestamptz not null default now()
);

create table if not exists public.live_scan_cache (
    ticker text primary key,
    pattern_detected text not null,
    current_price numeric not null check (current_price > 0),
    current_rvol numeric not null check (current_rvol >= 0),
    gap_fill_probability numeric not null check (gap_fill_probability between 0 and 100),
    market_session text not null default 'regular',
    updated_at timestamptz not null default now()
);

create table if not exists public.user_trade_journal (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    ticker text not null,
    pattern_traded text not null,
    chart_interval text not null,
    market_trend text not null,
    pnl_percentage numeric not null,
    created_at timestamptz not null default now()
);

create table if not exists public.user_ticker_history (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    ticker text not null,
    last_viewed_at timestamptz not null default now(),
    unique (user_id, ticker)
);

create index if not exists user_subscriptions_user_idx
    on public.user_subscriptions (user_id, created_at desc);
create index if not exists user_trade_journal_user_idx
    on public.user_trade_journal (user_id, created_at desc);
create index if not exists user_ticker_history_user_idx
    on public.user_ticker_history (user_id, last_viewed_at desc);

alter table public.user_subscriptions enable row level security;
alter table public.live_scan_cache enable row level security;
alter table public.user_trade_journal enable row level security;
alter table public.user_ticker_history enable row level security;

-- These tables are accessed through the authenticated backend service. Direct-client
-- policies should be added later only for deliberately supported client-side operations.
