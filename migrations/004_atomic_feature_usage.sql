-- Atomically reserve a single feature selection so concurrent requests cannot consume
-- different Free-tier win-rate reveals for the same chart and UTC day.

create or replace function public.claim_daily_feature_selection(
    p_user_id uuid,
    p_usage_date date,
    p_feature_name text,
    p_resource_key text,
    p_selected_key text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    claimed_key text;
begin
    insert into public.daily_feature_usage (
        user_id,
        usage_date,
        feature_name,
        resource_key,
        selected_key,
        usage_count
    ) values (
        p_user_id,
        p_usage_date,
        p_feature_name,
        p_resource_key,
        p_selected_key,
        1
    )
    on conflict (user_id, usage_date, feature_name, resource_key) do nothing;

    select selected_key
      into claimed_key
      from public.daily_feature_usage
     where user_id = p_user_id
       and usage_date = p_usage_date
       and feature_name = p_feature_name
       and resource_key = p_resource_key;

    return claimed_key = p_selected_key;
end;
$$;

revoke all on function public.claim_daily_feature_selection(uuid, date, text, text, text)
    from public, anon, authenticated;
grant execute on function public.claim_daily_feature_selection(uuid, date, text, text, text)
    to service_role;
