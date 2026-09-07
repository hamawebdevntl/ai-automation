-- Retire the Step Functions callback machinery.
--
-- Run this last. Everything it drops is worthless the moment Step Functions is
-- gone -- a task token is a capability against an AWS service that no longer
-- has anything running in it -- but nothing should reference a dropped function
-- while the old deployment might still be up.
--
-- Gate 2 is now: `open_gate2` writes `awaiting_review` and the `await_gate2`
-- marker in one update, and `claim_production` admits the row again once
-- `decide_production` has moved it to approved or rejected. No token, no
-- callback, no webhook, and no window in which a decision can be lost.
--
-- Dropping rather than leaving these in place, for three reasons:
--
--   1. `20260902190100_lock_down_gate_functions.sql` exists because Supabase's
--      default privileges had granted these security-definer functions to
--      `anon` and `authenticated` -- an anonymous caller got HTTP 200 from
--      `take_gate_token`. Dead security-definer functions in `public` that
--      reach into `private` are exactly the shape that a future change to those
--      defaults would re-expose, and dead code is not code anyone is watching.
--   2. `private.gate_tokens` carries `references public.productions on delete
--      cascade`, so it is not inert -- it participates in every production
--      delete.
--   3. Two gate mechanisms in the tree, one of them live, is the most expensive
--      thing to leave for the next reader.

drop function if exists public.list_pending_gate_resumes();
drop function if exists public.peek_gate_decision(uuid);
drop function if exists public.record_gate_decision(uuid, text);
drop function if exists public.release_gate_token(uuid);
drop function if exists public.take_gate_token(uuid);
drop function if exists public.set_gate_token(uuid, text);

drop table if exists private.gate_decisions;
drop table if exists private.gate_tokens;

-- The schema itself stays, empty. It costs nothing, it is still absent from
-- `config.toml`'s exposed schema list, and it is the right place for the next
-- thing that must be unreachable through PostgREST.

-- `execution_arn` held the Step Functions execution a production belonged to.
-- It can only ever be null now, and a column the type system says exists but
-- which is always empty is a lie that every future reader has to check.
alter table public.productions drop column if exists execution_arn;
