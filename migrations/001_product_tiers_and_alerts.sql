-- FloatIQ product-tier support tables.
-- Run once in the Supabase SQL editor before enabling the matching FlutterFlow calls.

create table if not exists public.daily_feature_usage (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    usage_date date not null default current_date,
    feature_name text not null,
    resource_key text not null,
    selected_key text not null,
    usage_count integer not null default 1 check (usage_count >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, usage_date, feature_name, resource_key)
);

create table if not exists public.market_alert_events (
    id uuid primary key default gen_random_uuid(),
    ticker text not null,
    event_type text not null check (event_type in (
        'institutional_scale_trade',
        'verified_insider_purchase'
    )),
    verification_status text not null check (verification_status in (
        'potential',
        'confirmed_market_data',
        'verified_public_filing'
    )),
    side text check (side in ('buy', 'sell', 'unknown')),
    shares numeric,
    price numeric,
    notional_value numeric,
    relative_volume numeric,
    actor_name text,
    actor_role text,
    source_name text not null,
    source_url text,
    source_event_id text,
    detected_at timestamptz not null default now(),
    verified_at timestamptz,
    message text not null,
    raw_payload jsonb,
    created_at timestamptz not null default now(),
    unique (source_name, source_event_id)
);

create index if not exists market_alert_events_detected_at_idx
    on public.market_alert_events (detected_at desc);
create index if not exists market_alert_events_ticker_idx
    on public.market_alert_events (ticker, detected_at desc);

create table if not exists public.user_alert_rules (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    ticker text,
    pattern_name text,
    minimum_win_rate numeric not null default 90 check (minimum_win_rate between 0 and 100),
    minimum_sample_size integer not null default 30 check (minimum_sample_size >= 2),
    minimum_relative_volume numeric,
    channels text[] not null default array['in_app']::text[],
    enabled boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (ticker is not null or pattern_name is not null)
);

create index if not exists user_alert_rules_user_id_idx
    on public.user_alert_rules (user_id, enabled);

alter table public.daily_feature_usage enable row level security;
alter table public.market_alert_events enable row level security;
alter table public.user_alert_rules enable row level security;

-- No direct-client policies are intentional. The FastAPI backend validates the
-- Supabase JWT, checks the subscription, and then uses its service credential.
-- This prevents a modified FlutterFlow request from bypassing paid-tier rules.

