-- =============================================================================
-- The drafting model defers to the deployment until somebody chooses.
-- =============================================================================
--
-- 20260906150000 added `idea_provider` as `not null default 'claude'`. That
-- was wrong in a way that only shows up on an install whose deployment says
-- something else.
--
-- The column was meant to be authoritative *once chosen*, with the deployed
-- `IDEA_LLM_PROVIDER` answering until then -- that is what
-- `pipeline.trends.controls.DEFAULT_IDEA_PROVIDER` being an empty string is
-- for, and what its comment promises. But a NOT NULL column with a literal
-- default is never empty, so that fallback could never fire. The effect was a
-- migration silently overriding a working deployment: this install runs
-- `IDEA_LLM_PROVIDER=gemini` and has no Anthropic key, and every trend run
-- began failing at the provider check with a message naming an environment
-- variable that was, in fact, set correctly.
--
-- That is the worst shape a configuration bug can take. The error accused the
-- environment, the environment was right, and the real cause was a default
-- chosen months earlier by someone with no way of knowing what this install
-- would be running.
--
-- So the empty string becomes a real, storable value meaning "whatever the
-- pipeline is deployed with", and it is the default for a fresh install. A
-- migration should not get a vote on which model drafts.
-- =============================================================================

do $$
begin
  alter table public.trend_settings drop constraint if exists trend_settings_idea_provider;
  alter table public.trend_settings add constraint trend_settings_idea_provider
    check (idea_provider in ('', 'claude', 'gemini'));
end;
$$;

alter table public.trend_settings alter column idea_provider set default '';

comment on column public.trend_settings.idea_provider is
  'Which model drafts the queue. Empty defers to the pipeline''s IDEA_LLM_PROVIDER, which is the default until an owner chooses.';

-- ---------------------------------------------------------------------------
-- This install.
-- ---------------------------------------------------------------------------
--
-- Set to the empty string rather than to 'gemini', because the point is that
-- the database should not be the one deciding. The deployment already says
-- gemini and has the key for it; deferring makes the row agree with reality
-- instead of asserting over it.
--
-- Guarded on the value the earlier migration wrote. If an owner has since
-- picked a provider in the app -- including picking Claude deliberately --
-- that is a choice, and a migration overwriting it would be the same mistake
-- again in the opposite direction.
update public.trend_settings
   set idea_provider = ''
 where id
   and idea_provider = 'claude'
   and updated_by is null;
