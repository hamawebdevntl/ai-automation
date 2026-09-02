-- =============================================================================
-- Fix: the role guard locked out the only caller that could create an owner
-- =============================================================================
--
-- `guard_profile_role()` refused any role change unless `public.is_owner()`
-- returned true. `is_owner()` reads `auth.uid()`, which is null on a
-- service-role connection or a direct psql session — so the guard rejected the
-- backend as well as the browser, and with no owner in the table yet, nothing
-- could ever promote the first one. A chicken-and-egg deadlock.
--
-- The guard exists to stop a *signed-in user* editing their own row into
-- `owner`. A caller with no `auth.uid()` is holding a key that already bypasses
-- row-level security entirely; there is nothing left for this trigger to
-- protect against, and pretending otherwise only breaks legitimate
-- administration.
-- =============================================================================

create or replace function public.guard_profile_role()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.role is distinct from old.role then
    -- No session user: a service-role key or a direct database connection.
    -- Both already outrank this check.
    if auth.uid() is null then
      return new;
    end if;

    if not public.is_owner() then
      raise exception 'Only an owner may change a role' using errcode = '42501';
    end if;
  end if;

  return new;
end;
$$;
