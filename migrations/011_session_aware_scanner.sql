-- Provider-neutral market-session capabilities, cached observations, and saved presets.
-- Live ingestion remains disabled until a licensed feed is configured and calibrated.

alter table public.supernova_scan_cache
    drop constraint if exists supernova_scan_cache_market_session_check;
alter table public.supernova_scan_cache
    add constraint supernova_scan_cache_market_session_check check (
        market_session in (
            'pre_market', 'regular', 'after_hours', 'overnight',
            'weekend', 'continuous', 'closed'
        )
    );

create table if not exists public.instrument_session_capabilities (
    ticker text primary key,
    asset_type text not null check (
        asset_type in ('equity', 'etf', 'fund', 'crypto', 'forex', 'futures')
    ),
    venue text,
    timezone_name text not null default 'America/New_York',
    supported_sessions text[] not null default array['regular']::text[],
    weekend_data_supported boolean not null default false,
    source_name text not null,
    verified_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (cardinality(supported_sessions) > 0),
    check (supported_sessions <@ array[
        'pre_market', 'regular', 'after_hours', 'overnight',
        'weekend', 'continuous', 'closed'
    ]::text[])
);

create table if not exists public.session_opportunity_cache (
    ticker text primary key,
    asset_type text not null check (
        asset_type in ('equity', 'etf', 'fund', 'crypto', 'forex', 'futures')
    ),
    venue text,
    price numeric not null check (price > 0),
    relative_volume numeric not null check (relative_volume >= 0),
    average_dollar_volume numeric not null check (average_dollar_volume >= 0),
    spread_bps numeric not null check (spread_bps >= 0),
    price_change_pct numeric not null,
    range_breakout_pct numeric not null,
    vwap_distance_pct numeric not null,
    observed_at timestamptz not null,
    received_at timestamptz not null,
    market_session text not null check (
        market_session in (
            'pre_market', 'regular', 'after_hours', 'overnight',
            'weekend', 'continuous', 'closed'
        )
    ),
    provider_supports_session boolean not null default false,
    ranking_score numeric not null check (ranking_score between 0 and 100),
    score_components jsonb not null default '{}'::jsonb,
    reason_codes text[] not null default '{}'::text[],
    source_name text not null,
    source_event_id text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (source_name, source_event_id)
);

create table if not exists public.user_session_scanner_presets (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null check (char_length(name) between 1 and 80),
    asset_types text[] not null default array['equity', 'crypto']::text[],
    market_sessions text[] not null default array['regular']::text[],
    weekend_only boolean not null default false,
    minimum_score numeric not null default 40 check (minimum_score between 0 and 100),
    minimum_relative_volume numeric not null default 0 check (minimum_relative_volume >= 0),
    minimum_average_dollar_volume numeric not null default 0 check (
        minimum_average_dollar_volume >= 0
    ),
    maximum_spread_bps numeric not null default 150 check (maximum_spread_bps >= 0),
    include_unconfirmed boolean not null default false,
    enabled boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, name),
    check (cardinality(asset_types) > 0),
    check (asset_types <@ array[
        'equity', 'etf', 'fund', 'crypto', 'forex', 'futures'
    ]::text[]),
    check (cardinality(market_sessions) > 0),
    check (market_sessions <@ array[
        'pre_market', 'regular', 'after_hours', 'overnight',
        'weekend', 'continuous', 'closed'
    ]::text[])
);

create index if not exists session_opportunity_filter_rank_idx
    on public.session_opportunity_cache (
        asset_type, market_session, ranking_score desc, observed_at desc
    );
create index if not exists session_opportunity_observed_idx
    on public.session_opportunity_cache (observed_at desc);
create index if not exists user_session_scanner_presets_user_idx
    on public.user_session_scanner_presets (user_id, enabled, updated_at desc);

drop trigger if exists set_instrument_session_capabilities_updated_at
    on public.instrument_session_capabilities;
create trigger set_instrument_session_capabilities_updated_at
before update on public.instrument_session_capabilities
for each row execute function public.set_updated_at();

drop trigger if exists set_session_opportunity_cache_updated_at
    on public.session_opportunity_cache;
create trigger set_session_opportunity_cache_updated_at
before update on public.session_opportunity_cache
for each row execute function public.set_updated_at();

drop trigger if exists set_user_session_scanner_presets_updated_at
    on public.user_session_scanner_presets;
create trigger set_user_session_scanner_presets_updated_at
before update on public.user_session_scanner_presets
for each row execute function public.set_updated_at();

alter table public.instrument_session_capabilities enable row level security;
alter table public.session_opportunity_cache enable row level security;
alter table public.user_session_scanner_presets enable row level security;

revoke all privileges on table public.instrument_session_capabilities
    from anon, authenticated;
revoke all privileges on table public.session_opportunity_cache
    from anon, authenticated;
revoke all privileges on table public.user_session_scanner_presets
    from anon, authenticated;

grant select, insert, update, delete on table public.instrument_session_capabilities
    to service_role;
grant select, insert, update, delete on table public.session_opportunity_cache
    to service_role;
grant select, insert, update, delete on table public.user_session_scanner_presets
    to service_role;

-- FastAPI is the sole data path. It resolves the authenticated user and paid
-- tier before reading cached observations or user presets.
