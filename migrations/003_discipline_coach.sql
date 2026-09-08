-- User-authored trading rules and objective process reviews.
-- These records support discipline tooling; they do not authorize live brokerage activity.

create table if not exists public.user_discipline_settings (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    enabled boolean not null default true,
    mode text not null default 'coach' check (mode in ('monitor', 'coach', 'strict', 'locked')),
    max_risk_per_trade_pct numeric not null default 1 check (max_risk_per_trade_pct > 0 and max_risk_per_trade_pct <= 100),
    max_position_value_pct numeric not null default 20 check (max_position_value_pct > 0 and max_position_value_pct <= 100),
    max_daily_loss_pct numeric not null default 3 check (max_daily_loss_pct > 0 and max_daily_loss_pct <= 100),
    minimum_reward_to_risk numeric not null default 2 check (minimum_reward_to_risk > 0),
    required_relative_volume numeric check (required_relative_volume > 0),
    require_setup_confirmation boolean not null default true,
    max_open_positions integer not null default 5 check (max_open_positions >= 1),
    max_sector_exposure_pct numeric not null default 40 check (max_sector_exposure_pct > 0 and max_sector_exposure_pct <= 100),
    cooldown_after_loss_minutes integer not null default 15 check (cooldown_after_loss_minutes >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id)
);

create table if not exists public.discipline_evaluations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    ticker text not null,
    settings_snapshot jsonb not null,
    order_snapshot jsonb not null,
    violations jsonb not null default '[]'::jsonb,
    unable_to_evaluate jsonb not null default '[]'::jsonb,
    discipline_gate_passed boolean not null,
    acknowledgement_used boolean not null default false,
    broker_order_submitted boolean not null default false,
    evaluated_at timestamptz not null default now()
);

create index if not exists discipline_evaluations_user_idx
    on public.discipline_evaluations (user_id, evaluated_at desc);

create table if not exists public.trade_process_reviews (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    ticker text not null,
    trade_snapshot jsonb not null,
    discipline_score numeric not null check (discipline_score between 0 and 100),
    process_classification text not null,
    findings jsonb not null default '[]'::jsonb,
    reviewed_at timestamptz not null default now()
);

create index if not exists trade_process_reviews_user_idx
    on public.trade_process_reviews (user_id, reviewed_at desc);

create table if not exists public.user_legal_acceptances (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    document_type text not null check (document_type in (
        'terms', 'privacy', 'risk_disclosure', 'broker_terms'
    )),
    document_version text not null,
    document_sha256 text not null check (document_sha256 ~ '^[a-f0-9]{64}$'),
    acceptance_source text not null default 'flutterflow',
    user_agent text,
    accepted_at timestamptz not null default now(),
    unique (user_id, document_type, document_version)
);

alter table public.user_discipline_settings enable row level security;
alter table public.discipline_evaluations enable row level security;
alter table public.trade_process_reviews enable row level security;
alter table public.user_legal_acceptances enable row level security;

-- No direct-client policies are intentional. FastAPI validates the Supabase token and
-- subscription before using the backend service credential. Acceptance records document
-- consent but do not waive securities laws or enable live order routing.
