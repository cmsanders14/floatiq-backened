-- Broker-neutral storage. This migration does not enable order submission.
-- OAuth secrets must live in an approved encrypted secret store; only an opaque
-- reference is retained here so database exports never contain broker tokens.

create table if not exists public.broker_connections (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider in ('schwab', 'webull', 'robinhood')),
    provider_user_reference text,
    external_secret_reference text,
    connection_status text not null default 'pending' check (
        connection_status in ('pending', 'active', 'reauthorization_required', 'revoked', 'error')
    ),
    scopes jsonb not null default '[]'::jsonb,
    metadata jsonb not null default '{}'::jsonb,
    connected_at timestamptz,
    last_verified_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, provider)
);

create table if not exists public.broker_order_intents (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    broker_connection_id uuid references public.broker_connections(id) on delete set null,
    discipline_evaluation_id uuid references public.discipline_evaluations(id) on delete set null,
    provider text not null check (provider in ('schwab', 'webull', 'robinhood')),
    idempotency_key text not null,
    request_fingerprint text not null check (request_fingerprint ~ '^[a-f0-9]{64}$'),
    order_payload jsonb not null,
    user_confirmed boolean not null default false,
    status text not null default 'recorded_not_submitted' check (status in (
        'recorded_not_submitted', 'paper_submitted', 'submitted', 'partially_filled',
        'filled', 'cancel_pending', 'cancelled', 'rejected', 'expired', 'unknown'
    )),
    provider_order_reference text,
    submitted_at timestamptz,
    terminal_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, idempotency_key)
);

create index if not exists broker_order_intents_user_created_idx
    on public.broker_order_intents (user_id, created_at desc);

create table if not exists public.broker_order_events (
    id bigserial primary key,
    order_intent_id uuid not null references public.broker_order_intents(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    event_type text not null,
    from_status text,
    to_status text not null,
    provider_event_reference text,
    event_payload jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now()
);

create index if not exists broker_order_events_intent_idx
    on public.broker_order_events (order_intent_id, occurred_at);

alter table public.broker_connections enable row level security;
alter table public.broker_order_intents enable row level security;
alter table public.broker_order_events enable row level security;

-- No direct-client policies are intentional. The backend service validates the
-- signed-in user and performs all reads/writes. Live order routing remains off.
