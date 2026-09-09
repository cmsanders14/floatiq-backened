-- Additive, backend-owned billing audit and notification delivery foundation.
-- No payment or external notification provider is activated by this migration.

alter table public.user_subscriptions
    add column if not exists provider_event_created_at bigint;

create table if not exists public.billing_webhook_events (
    id uuid primary key default gen_random_uuid(),
    provider text not null default 'stripe' check (provider = 'stripe'),
    provider_event_id text not null,
    event_type text not null,
    processing_status text not null default 'processing' check (
        processing_status in ('processing', 'processed', 'failed', 'ignored')
    ),
    error_message text,
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    unique (provider, provider_event_id)
);

create table if not exists public.notifications (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    event_key text not null,
    event_type text not null,
    title text not null,
    message text not null,
    payload jsonb not null default '{}'::jsonb,
    read_at timestamptz,
    created_at timestamptz not null default now(),
    unique (user_id, event_key)
);

create table if not exists public.notification_deliveries (
    id uuid primary key default gen_random_uuid(),
    notification_id uuid not null references public.notifications(id) on delete cascade,
    channel text not null check (channel in ('in_app', 'push', 'web', 'email')),
    status text not null default 'pending' check (
        status in ('pending', 'processing', 'delivered', 'failed', 'dead_letter')
    ),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    max_attempts integer not null default 5 check (max_attempts between 1 and 20),
    available_at timestamptz not null default now(),
    locked_at timestamptz,
    delivered_at timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (notification_id, channel)
);

create index if not exists notifications_user_created_idx
    on public.notifications (user_id, created_at desc);
create index if not exists notifications_user_unread_idx
    on public.notifications (user_id, created_at desc) where read_at is null;
create index if not exists notification_deliveries_pending_idx
    on public.notification_deliveries (available_at, created_at)
    where status in ('pending', 'failed');
create index if not exists user_subscriptions_provider_event_created_idx
    on public.user_subscriptions (provider_event_created_at desc)
    where provider_event_created_at is not null;

drop trigger if exists set_notification_deliveries_updated_at
    on public.notification_deliveries;
create trigger set_notification_deliveries_updated_at
before update on public.notification_deliveries
for each row execute function public.set_updated_at();

alter table public.billing_webhook_events enable row level security;
alter table public.notifications enable row level security;
alter table public.notification_deliveries enable row level security;

revoke all privileges on table public.billing_webhook_events from anon, authenticated;
revoke all privileges on table public.notifications from anon, authenticated;
revoke all privileges on table public.notification_deliveries from anon, authenticated;

grant select, insert, update, delete on table public.billing_webhook_events to service_role;
grant select, insert, update, delete on table public.notifications to service_role;
grant select, insert, update, delete on table public.notification_deliveries to service_role;

-- The backend service role is the only data path. User ownership is enforced by
-- the authenticated FastAPI endpoints, not by accepting a client-supplied ID.
