-- Add subscription lifecycle fields and keep mutable-table timestamps accurate.

alter table public.user_subscriptions
    add column if not exists status text not null default 'active',
    add column if not exists current_period_end timestamptz,
    add column if not exists provider_customer_id text,
    add column if not exists provider_subscription_id text,
    add column if not exists updated_at timestamptz not null default now();

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'user_subscriptions_status_check'
          and conrelid = 'public.user_subscriptions'::regclass
    ) then
        alter table public.user_subscriptions
            add constraint user_subscriptions_status_check
            check (status in ('active', 'trialing', 'past_due', 'canceled', 'incomplete', 'unpaid'));
    end if;
end;
$$;

create unique index if not exists user_subscriptions_provider_id_idx
    on public.user_subscriptions (provider_subscription_id)
    where provider_subscription_id is not null;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists set_user_subscriptions_updated_at on public.user_subscriptions;
create trigger set_user_subscriptions_updated_at
before update on public.user_subscriptions
for each row execute function public.set_updated_at();

drop trigger if exists set_daily_feature_usage_updated_at on public.daily_feature_usage;
create trigger set_daily_feature_usage_updated_at
before update on public.daily_feature_usage
for each row execute function public.set_updated_at();

drop trigger if exists set_user_alert_rules_updated_at on public.user_alert_rules;
create trigger set_user_alert_rules_updated_at
before update on public.user_alert_rules
for each row execute function public.set_updated_at();

drop trigger if exists set_market_analog_profiles_updated_at on public.market_analog_profiles;
create trigger set_market_analog_profiles_updated_at
before update on public.market_analog_profiles
for each row execute function public.set_updated_at();

drop trigger if exists set_user_discipline_settings_updated_at on public.user_discipline_settings;
create trigger set_user_discipline_settings_updated_at
before update on public.user_discipline_settings
for each row execute function public.set_updated_at();
