-- Precomputed profiles make a 6,000-stock analog search fast enough for an API request.
-- A scheduled data worker should refresh `current` profiles and separately curate reference eras.

create table if not exists public.market_analog_profiles (
    id uuid primary key default gen_random_uuid(),
    ticker text not null,
    company_name text,
    sector text,
    profile_type text not null check (profile_type in ('current', 'reference_era')),
    profile_key text not null,
    era_label text,
    window_start date not null,
    window_end date not null,
    features jsonb not null,
    data_sources jsonb not null default '[]'::jsonb,
    calculated_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (ticker, profile_type, profile_key),
    check (window_end >= window_start),
    check (profile_type = 'current' or era_label is not null)
);

create index if not exists market_analog_profiles_profile_type_idx
    on public.market_analog_profiles (profile_type, ticker);
create index if not exists market_analog_profiles_features_gin_idx
    on public.market_analog_profiles using gin (features);

alter table public.market_analog_profiles enable row level security;

-- Deliberately no direct-client policy. The authenticated API applies subscription and
-- experimental-feature checks before it reads or ranks these proprietary profiles.
