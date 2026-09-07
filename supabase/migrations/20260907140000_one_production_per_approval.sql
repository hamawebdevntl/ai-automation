-- An approved idea gets one production, not one per sweep.
--
-- The bug this fixes, found by running the driver against a real database:
-- `start_approved_productions` opened a production for every approved idea
-- with no *live* production, matching `productions_one_live_per_idea`, which
-- excludes rejected, failed and parked. Those exclusions are right for that
-- index -- a parked production must not permanently block a deliberate re-run.
--
-- They are wrong for a poller. Under Step Functions this was a Supabase
-- Database Webhook on the transition into `approved`, so it fired exactly once
-- however the production ended. Polling has no notion of a transition: park the
-- production and the idea is immediately eligible again, so the loop is
-- park -> reopen -> park, several times a second.
--
-- Measured before the fix: 44 productions for one idea in about two seconds.
-- Every one of those is a render submission. On `ai-presenter`, which bills
-- $1-2 against a pay-as-you-go wallet, that is the whole wallet inside a
-- minute, and `claim_render_slot` does not help -- it guards one row against
-- two workers, and these were 44 legitimately distinct rows.
--
-- So the poller opens a production only for an idea that has none at all. A
-- re-run after a park stays possible and stays deliberate: delete the parked
-- production and the idea becomes eligible again, which is the same "a person
-- decides" rule that parking exists to enforce. `productions_one_live_per_idea`
-- is untouched -- it is still what makes that re-run safe.

create or replace function public.start_approved_productions()
returns setof public.productions
language sql
security definer
set search_path = public
as $$
  insert into public.productions (idea_id, style_preset_id, status, stage, cost_estimate_usd)
  select i.id,
         i.approved_style_id,
         'queued',
         'queued',
         sp.est_cost_max_usd
    from public.ideas i
    join public.style_presets sp on sp.id = i.approved_style_id
   where i.status = 'approved'
     -- Any production, in any state. Not just a live one: see above.
     and not exists (
       select 1 from public.productions p where p.idea_id = i.id
     )
  on conflict do nothing
  returning *;
$$;

revoke all on function public.start_approved_productions() from public, anon, authenticated;
grant execute on function public.start_approved_productions() to service_role;
