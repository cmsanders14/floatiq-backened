-- Fresh, precomputed candidates for the default Pro/Elite Supernova Radar.
-- The market-data worker owns writes; clients read only through FastAPI.

create table if not exists public.supernova_scan_cache (
    ticker text primary key,
    stage text not null check (stage in (
        'WATCHING', 'TRIGGERED', 'CONFIRMED_MOMENTUM'
    )),
    score numeric not null check (score between 0 and 100),
    direction text not null check (direction in ('up', 'down')),
    price numeric not null check (price > 0),
    relative_volume_at_time numeric not null check (relative_volume_at_time >= 0),
    volume_acceleration numeric not null check (volume_acceleration >= 0),
    price_change_pct numeric not null,
    range_breakout_pct numeric not null,
    vwap_distance_pct numeric not null,
    average_dollar_volume numeric not null check (average_dollar_volume >= 0),
    spread_bps numeric not null check (spread_bps >= 0),
    float_turnover_pct numeric check (float_turnover_pct >= 0),
    follow_through_confirmed boolean not null default false,
    market_session text not null check (market_session in (
        'pre_market', 'regular', 'after_hours'
    )),
    score_components jsonb not null,
    source_name text not null,
    source_event_id text,
    detected_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (source_name, source_event_id)
);

create index if not exists supernova_scan_cache_score_idx
    on public.supernova_scan_cache (score desc, updated_at desc);

create table if not exists public.user_supernova_settings (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    enabled boolean not null default true,
    minimum_score numeric not null default 65 check (minimum_score between 0 and 100),
    minimum_relative_volume numeric not null default 3 check (minimum_relative_volume >= 0),
    minimum_price_expansion_pct numeric not null default 2 check (
        minimum_price_expansion_pct between 0 and 100
    ),
    minimum_average_dollar_volume numeric not null default 1000000 check (
        minimum_average_dollar_volume >= 0
    ),
    maximum_spread_bps numeric not null default 150 check (maximum_spread_bps >= 0),
    directions text[] not null default array['up', 'down']::text[],
    channels text[] not null default array['in_app']::text[],
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id),
    check (directions <@ array['up', 'down']::text[]),
    check (channels <@ array['in_app', 'push', 'web']::text[])
);

drop trigger if exists set_supernova_scan_cache_updated_at on public.supernova_scan_cache;
create trigger set_supernova_scan_cache_updated_at
before update on public.supernova_scan_cache
for each row execute function public.set_updated_at();

drop trigger if exists set_user_supernova_settings_updated_at on public.user_supernova_settings;
create trigger set_user_supernova_settings_updated_at
before update on public.user_supernova_settings
for each row execute function public.set_updated_at();

alter table public.supernova_scan_cache enable row level security;
alter table public.user_supernova_settings enable row level security;

-- No direct-client policy. Paid-tier enforcement and stale-candidate filtering live in FastAPI.
