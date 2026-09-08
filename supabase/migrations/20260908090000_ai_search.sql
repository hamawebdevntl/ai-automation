-- =============================================================================
-- Searching by description.
-- =============================================================================
--
-- The saved keyword list is a standing brief: the audience the scout samples
-- every morning, chosen once and edited rarely. A person in the middle of a
-- task has a different question -- "I want to make something for busy parents
-- who want to get fit at home; what is moving right now?" -- and the list
-- knows nothing about it. Until now the only way to ask was to edit the list,
-- press the button, and remember to put the list back.
--
-- So a run can carry a description. It rides on the `trend_runs` row exactly
-- as the length overrides do, and for the same reason: it belongs to this run,
-- and nothing about it should become tomorrow morning's behaviour. The worker
-- reads the description into search terms in the active source's vocabulary,
-- writes how it understood the request back onto the row *before* scouting
-- (so the app can show "understood as ..." while the scout is still going),
-- scouts those terms for this run only, and drafts ideas that each carry a
-- score for how well they serve the description and a sentence on how they
-- connect to it.
--
-- Nothing here touches `trend_settings`. A described run does not advance the
-- rotation cursor and does not save its terms, so the touch trigger on that
-- table never sees it and the saved list stays an explicit audience choice.
--
-- The description is readable by every signed-in user through the existing
-- `trend_runs_read` policy, deliberately: a viewer who can see that a run is
-- going should be able to see what it is looking for, and recent searches are
-- meant to be returned to by whoever is reviewing the queue.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- trend_runs: the description, and how it was read.
-- ---------------------------------------------------------------------------
alter table public.trend_runs
  add column if not exists prompt         text,
  -- One document rather than five columns, for the reason `rejections` is: it
  -- is a display record whose shape will grow, and the app reads it whole.
  add column if not exists interpretation jsonb,
  -- Set alongside `interpretation`, so the banner can move from "reading your
  -- description" to "understood as" without parsing the document.
  add column if not exists interpreted_at timestamptz;

do $$
begin
  -- Non-blank and bounded. `btrim` with the explicit character list rather
  -- than the default, which strips spaces only -- a description of nothing
  -- but newlines would pass and then be shown back as an empty quote. A
  -- sentence or two is what the box is for; a thousand characters is generous.
  alter table public.trend_runs drop constraint if exists trend_runs_prompt;
  alter table public.trend_runs add constraint trend_runs_prompt
    check (prompt is null
           or (btrim(prompt, E' \t\n\r\f\v') <> '' and length(prompt) <= 1000));

  -- An interpretation is *of* a description. Keeps the two from drifting.
  alter table public.trend_runs drop constraint if exists trend_runs_interpretation_needs_prompt;
  alter table public.trend_runs add constraint trend_runs_interpretation_needs_prompt
    check (interpretation is null or prompt is not null);
end;
$$;

-- "Recent searches" is this, newest first.
create index if not exists trend_runs_prompted_idx
  on public.trend_runs (requested_at desc)
  where prompt is not null;

comment on column public.trend_runs.prompt is
  'What the owner said they were working on, verbatim. Null on an ordinary or scheduled run. Read by the worker; never written to trend_settings.';
comment on column public.trend_runs.interpretation is
  'How the worker read the prompt: {restatement, terms, vocabulary, source, vague, nudge, suggestions, provider, model}. Written before scouting starts.';
comment on column public.trend_runs.interpreted_at is
  'When the interpretation was written. Null until then.';


-- ---------------------------------------------------------------------------
-- ideas: which run drafted this, and how it connects to what was asked.
-- ---------------------------------------------------------------------------
--
-- `trend_run_id` is new for every run, not only described ones. An idea had no
-- link to its run at all: a queue row could not say which run, which terms or
-- which breakdown produced it. Nullable and `on delete set null`, because an
-- idea must outlive its run row if one is ever pruned, and because every row
-- that exists today predates this column.
alter table public.ideas
  add column if not exists trend_run_id uuid references public.trend_runs (id) on delete set null,
  -- Both null on an ordinary run. Only a described run has anything to score
  -- against.
  add column if not exists relevance    smallint,
  add column if not exists connection   text;

do $$
begin
  alter table public.ideas drop constraint if exists ideas_relevance_range;
  alter table public.ideas add constraint ideas_relevance_range
    check (relevance is null or relevance between 0 and 100);
end;
$$;

-- The filtered queue: one run's ideas, best fit first.
create index if not exists ideas_trend_run_idx
  on public.ideas (trend_run_id)
  where trend_run_id is not null;

comment on column public.ideas.trend_run_id is
  'The trend run that drafted this idea. Null on rows from before this existed.';
comment on column public.ideas.relevance is
  '0-100: how directly this serves what the owner described when they started the run. Null on an ordinary run.';
comment on column public.ideas.connection is
  'One sentence, addressed to the owner, on how this idea connects to what they described. Null on an ordinary run.';


-- ---------------------------------------------------------------------------
-- Requesting a run, optionally by description.
-- ---------------------------------------------------------------------------
--
-- One function, not a second one. A described search is a trend run: the same
-- row, the same single in-flight lock, the same 55006 when one is already
-- going. A `request_trend_search` alongside this would be a second copy of the
-- exception mapping and a second grant to keep in step.
--
-- Dropped and recreated rather than `create or replace`, because the argument
-- list is changing -- see 20260906170000 for why leaving the old signature in
-- place makes the call ambiguous. Every historical signature is dropped so
-- re-running this is safe. An older web bundle still sending only the two
-- integers resolves to this function, with the prompt defaulted to null.
drop function if exists public.request_trend_run();
drop function if exists public.request_trend_run(integer, integer);
drop function if exists public.request_trend_run(integer, integer, text);

create function public.request_trend_run(
  p_budget_minutes   integer default null,
  p_hashtags_per_run integer default null,
  p_prompt           text    default null
)
returns public.trend_runs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_run    public.trend_runs;
  v_prompt text := nullif(btrim(coalesce(p_prompt, ''), E' \t\n\r\f\v'), '');
begin
  if not public.is_owner() then
    raise exception 'Only an owner may start a trend run' using errcode = '42501';
  end if;

  -- A description that was sent but is blank must not quietly become an
  -- ordinary run: the owner would be shown the saved list's ideas as the
  -- answer to a question they typed.
  if p_prompt is not null and v_prompt is null then
    raise exception 'Describe what you are working on before searching'
      using errcode = '22023';
  end if;
  if length(v_prompt) > 1000 then
    raise exception 'That description is % characters; the limit is 1000', length(v_prompt)
      using errcode = '22023';
  end if;

  begin
    insert into public.trend_runs (
      requested_by, override_run_budget_minutes, override_hashtags_per_run, prompt
    )
    values (auth.uid(), p_budget_minutes, p_hashtags_per_run, v_prompt)
    returning * into v_run;
  exception
    when unique_violation then
      -- Not an error worth a stack trace: the owner pressed the button twice,
      -- or two of them pressed it at once. 55006 is object_in_use, which the
      -- app maps to a plain sentence rather than a failure.
      raise exception 'A trend run is already in progress'
        using errcode = '55006';
    when check_violation then
      raise exception 'That search length is outside the allowed range'
        using errcode = '22023';
  end;

  return v_run;
end;
$$;

revoke all on function public.request_trend_run(integer, integer, text) from public, anon;
grant execute on function public.request_trend_run(integer, integer, text) to authenticated;

comment on function public.request_trend_run(integer, integer, text) is
  'Start a trend run. The two integers override trend_settings for this run only; p_prompt makes it a described search, scouted on terms read from the description for this run only.';
