-- Append-only accuracy evidence, versioned regime observations, and launch controls.
-- These tables are backend-only. FlutterFlow must never receive the service-role key.

alter table public.session_opportunity_cache
    add column if not exists algorithm_version text not null
        default 'session_opportunity_v1',
    add column if not exists market_regime text not null
        default 'insufficient_data' check (
            market_regime in (
                'bullish_trend', 'bearish_trend', 'range_bound',
                'high_volatility', 'insufficient_data'
            )
        ),
    add column if not exists regime_tags text[] not null default '{}'::text[],
    add column if not exists regime_basis jsonb not null default '{}'::jsonb;

alter table public.supernova_scan_cache
    add column if not exists algorithm_version text not null default 'supernova_v1',
    add column if not exists market_regime text not null
        default 'insufficient_data' check (
            market_regime in (
                'bullish_trend', 'bearish_trend', 'range_bound',
                'high_volatility', 'insufficient_data'
            )
        ),
    add column if not exists regime_tags text[] not null default '{}'::text[],
    add column if not exists regime_basis jsonb not null default '{}'::jsonb;

create table if not exists public.scanner_algorithm_versions (
    algorithm_key text not null,
    version text not null,
    description text not null,
    configuration jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    primary key (algorithm_key, version),
    check (char_length(algorithm_key) between 1 and 80),
    check (char_length(version) between 1 and 80)
);

create table if not exists public.market_regime_snapshots (
    id uuid primary key default gen_random_uuid(),
    observed_at timestamptz not null,
    received_at timestamptz not null default now(),
    benchmark_symbol text not null,
    market_regime text not null check (
        market_regime in (
            'bullish_trend', 'bearish_trend', 'range_bound',
            'high_volatility', 'insufficient_data'
        )
    ),
    regime_tags text[] not null default '{}'::text[],
    regime_basis jsonb not null,
    algorithm_version text not null,
    source_name text not null,
    source_event_id text not null,
    created_at timestamptz not null default now(),
    unique (source_name, source_event_id)
);

create table if not exists public.scanner_signal_events (
    id uuid primary key default gen_random_uuid(),
    source_name text not null,
    source_event_id text not null,
    ticker text not null,
    asset_type text not null check (
        asset_type in ('equity', 'etf', 'fund', 'crypto', 'forex', 'futures')
    ),
    venue text,
    pattern_key text not null,
    signal_direction text not null check (signal_direction in ('up', 'down', 'neutral')),
    signal_status text not null check (
        signal_status in ('observed', 'published', 'suppressed')
    ),
    ranking_score numeric not null check (ranking_score between 0 and 100),
    observed_price numeric not null check (observed_price > 0),
    observed_at timestamptz not null,
    received_at timestamptz not null,
    quote_age_seconds numeric not null check (quote_age_seconds >= 0),
    market_session text not null check (
        market_session in (
            'pre_market', 'regular', 'after_hours', 'overnight',
            'weekend', 'continuous', 'closed'
        )
    ),
    market_regime text not null check (
        market_regime in (
            'bullish_trend', 'bearish_trend', 'range_bound',
            'high_volatility', 'insufficient_data'
        )
    ),
    algorithm_key text not null,
    algorithm_version text not null,
    proposed_trigger_price numeric check (proposed_trigger_price > 0),
    proposed_stop_price numeric check (proposed_stop_price > 0),
    proposed_target_price numeric check (proposed_target_price > 0),
    raw_metrics jsonb not null default '{}'::jsonb,
    reason_codes text[] not null default '{}'::text[],
    supersedes_signal_id uuid references public.scanner_signal_events(id),
    created_at timestamptz not null default now(),
    unique (source_name, source_event_id),
    check (observed_at <= received_at + interval '30 seconds')
);

create table if not exists public.signal_outcome_observations (
    id uuid primary key default gen_random_uuid(),
    signal_event_id uuid not null references public.scanner_signal_events(id),
    horizon_label text not null check (
        horizon_label in ('5m', '15m', '30m', '60m', 'market_close')
    ),
    evaluation_version text not null,
    evaluated_at timestamptz not null,
    observed_price numeric not null check (observed_price > 0),
    maximum_favorable_excursion_pct numeric,
    maximum_adverse_excursion_pct numeric,
    trigger_reached boolean not null,
    stop_reached boolean not null,
    target_reached boolean not null,
    first_threshold_event text not null check (
        first_threshold_event in ('trigger', 'stop', 'target', 'none', 'indeterminate')
    ),
    outcome_status text not null check (
        outcome_status in ('success', 'failure', 'neutral', 'incomplete')
    ),
    estimated_spread_bps numeric check (estimated_spread_bps >= 0),
    estimated_slippage_bps numeric check (estimated_slippage_bps >= 0),
    provider_fees numeric check (provider_fees >= 0),
    evidence jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (signal_event_id, horizon_label, evaluation_version)
);

create index if not exists regime_snapshots_observed_idx
    on public.market_regime_snapshots (observed_at desc, market_regime);
create index if not exists scanner_signal_analysis_idx
    on public.scanner_signal_events (
        algorithm_version, market_regime, pattern_key, observed_at desc
    );
create index if not exists scanner_signal_ticker_observed_idx
    on public.scanner_signal_events (ticker, observed_at desc);
create index if not exists signal_outcomes_analysis_idx
    on public.signal_outcome_observations (
        horizon_label, outcome_status, evaluated_at desc
    );

create or replace function public.capture_session_signal_event()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    insert into public.scanner_signal_events (
        source_name, source_event_id, ticker, asset_type, venue, pattern_key,
        signal_direction, signal_status, ranking_score, observed_price,
        observed_at, received_at, quote_age_seconds, market_session,
        market_regime, algorithm_key, algorithm_version, raw_metrics,
        reason_codes
    ) values (
        new.source_name,
        new.source_event_id,
        new.ticker,
        new.asset_type,
        new.venue,
        'session_opportunity',
        case
            when new.price_change_pct > 0 then 'up'
            when new.price_change_pct < 0 then 'down'
            else 'neutral'
        end,
        case
            when new.ranking_score >= 40
                and new.provider_supports_session
                and new.market_session <> 'closed'
                then 'published'
            else 'suppressed'
        end,
        new.ranking_score,
        new.price,
        new.observed_at,
        new.received_at,
        greatest(0, extract(epoch from (new.received_at - new.observed_at))),
        new.market_session,
        new.market_regime,
        'session_opportunity',
        new.algorithm_version,
        jsonb_build_object(
            'relative_volume', new.relative_volume,
            'average_dollar_volume', new.average_dollar_volume,
            'spread_bps', new.spread_bps,
            'price_change_pct', new.price_change_pct,
            'range_breakout_pct', new.range_breakout_pct,
            'vwap_distance_pct', new.vwap_distance_pct,
            'regime_basis', new.regime_basis
        ),
        new.reason_codes
    )
    on conflict (source_name, source_event_id) do nothing;
    return new;
end;
$$;

revoke all on function public.capture_session_signal_event()
    from public, anon, authenticated;
grant execute on function public.capture_session_signal_event() to service_role;

drop trigger if exists capture_session_signal_event
    on public.session_opportunity_cache;
create trigger capture_session_signal_event
after insert or update on public.session_opportunity_cache
for each row execute function public.capture_session_signal_event();

create or replace function public.capture_supernova_signal_event()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if new.source_event_id is null or btrim(new.source_event_id) = '' then
        return new;
    end if;
    insert into public.scanner_signal_events (
        source_name, source_event_id, ticker, asset_type, pattern_key,
        signal_direction, signal_status, ranking_score, observed_price,
        observed_at, received_at, quote_age_seconds, market_session,
        market_regime, algorithm_key, algorithm_version, raw_metrics,
        reason_codes
    ) values (
        new.source_name,
        new.source_event_id,
        new.ticker,
        'equity',
        'supernova_radar',
        new.direction,
        case
            when new.stage in ('TRIGGERED', 'CONFIRMED_MOMENTUM') then 'published'
            else 'observed'
        end,
        new.score,
        new.price,
        new.detected_at,
        new.updated_at,
        greatest(0, extract(epoch from (new.updated_at - new.detected_at))),
        new.market_session,
        new.market_regime,
        'supernova',
        new.algorithm_version,
        jsonb_build_object(
            'relative_volume_at_time', new.relative_volume_at_time,
            'volume_acceleration', new.volume_acceleration,
            'price_change_pct', new.price_change_pct,
            'range_breakout_pct', new.range_breakout_pct,
            'vwap_distance_pct', new.vwap_distance_pct,
            'average_dollar_volume', new.average_dollar_volume,
            'spread_bps', new.spread_bps,
            'float_turnover_pct', new.float_turnover_pct,
            'follow_through_confirmed', new.follow_through_confirmed,
            'regime_basis', new.regime_basis
        ),
        array[new.stage]
    )
    on conflict (source_name, source_event_id) do nothing;
    return new;
end;
$$;

revoke all on function public.capture_supernova_signal_event()
    from public, anon, authenticated;
grant execute on function public.capture_supernova_signal_event() to service_role;

drop trigger if exists capture_supernova_signal_event
    on public.supernova_scan_cache;
create trigger capture_supernova_signal_event
after insert or update on public.supernova_scan_cache
for each row execute function public.capture_supernova_signal_event();

alter table public.scanner_algorithm_versions enable row level security;
alter table public.market_regime_snapshots enable row level security;
alter table public.scanner_signal_events enable row level security;
alter table public.signal_outcome_observations enable row level security;

revoke all privileges on table public.scanner_algorithm_versions
    from anon, authenticated, service_role;
revoke all privileges on table public.market_regime_snapshots
    from anon, authenticated, service_role;
revoke all privileges on table public.scanner_signal_events
    from anon, authenticated, service_role;
revoke all privileges on table public.signal_outcome_observations
    from anon, authenticated, service_role;

-- Evidence rows are append-only for the application role: corrections are new
-- versioned observations, never edits to history.
grant select, insert on table public.scanner_algorithm_versions to service_role;
grant select, insert on table public.market_regime_snapshots to service_role;
grant select, insert on table public.scanner_signal_events to service_role;
grant select, insert on table public.signal_outcome_observations to service_role;

insert into public.scanner_algorithm_versions (
    algorithm_key, version, description, configuration
) values (
    'session_opportunity',
    'session_opportunity_v1',
    'Transparent session-aware opportunity ranking before regime calibration.',
    '{"prediction_claimed": false, "minimum_display_sample": 30}'::jsonb
) , (
    'supernova',
    'supernova_v1',
    'Transparent momentum-condition scoring before regime calibration.',
    '{"prediction_claimed": false, "minimum_display_sample": 30}'::jsonb
)
on conflict (algorithm_key, version) do nothing;
