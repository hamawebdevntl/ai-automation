-- Upload a finished cut, and publish it through Gate 2.
--
-- Everything downstream of the render is already provider-agnostic: the
-- quality check inspects a file, `copy.py` writes four captions, Gate 2 asks a
-- person, and `publish.py` hands the result to Postiz. None of it cares how
-- the video came to exist. The only reason a manually-made or externally
-- supplied cut could not use any of it was that the *entrance* was welded to
-- the generation half -- `productions.idea_id` and `productions.style_preset_id`
-- were both `not null`, so a production could only exist as the consequence of
-- an approved idea.
--
-- So this migration is mostly subtraction. An uploaded production is an
-- ordinary production with no idea and no style, entering the graph at
-- `check_upload` instead of `write_script`, and every gate, control,
-- reconciler and index below it works on it unchanged.
--
-- What it deliberately does NOT do: give an upload a synthetic idea row. That
-- was the other option, and it is worse in three separate ways. The ideas
-- queue is a list of things a person has not decided on yet, and seeding it
-- with rows nobody proposed makes the queue lie. `ideas_style_required_when_approved`
-- would force us to invent a style preset for a video that was never rendered,
-- so the production's `est_cost_max_usd`, `render_mode` and params would all be
-- fiction. And the falsehood would be permanent: every later reader of that
-- idea -- the dedup window, `expire_stale_ideas`, the trend report -- would
-- count a video somebody uploaded as a trend the scout found.

-- =============================================================================
-- 1. A production says where it came from
-- =============================================================================
alter table public.productions
  add column if not exists source  text not null default 'generated',
  add column if not exists title   text,
  add column if not exists brief   text,
  add column if not exists is_aigc boolean not null default true;

alter table public.productions drop constraint if exists productions_source_check;
alter table public.productions add constraint productions_source_check
  check (source in ('generated', 'upload'));

comment on column public.productions.source is
  'How this production entered the system. ''generated'' came through Gate 1 from an approved idea and was rendered; ''upload'' is a finished cut an owner supplied, which enters at the quality check and has no idea, no style and no render.';
comment on column public.productions.title is
  'The subject line for an upload, which has no idea to take one from. Null on a generated production -- `ideas.title` is that one''s subject, and duplicating it here would give the review screen two answers to the same question.';
comment on column public.productions.brief is
  'What an uploaded video is about, in the uploader''s own words. This is the input `generate_platform_copy` writes the four captions from, standing in for the approved script a generated production has. Not a script: nothing narrates it and nothing renders from it.';
comment on column public.productions.is_aigc is
  'Whether this video is AI-generated, for the platforms'' disclosure flags -- TikTok''s `is_aigc` today, and the YouTube and Meta equivalents in docs/GAPS.md B3 when they are handled. Defaults true because everything this pipeline renders is AI-generated; an upload has to answer for itself, because only the uploader knows.';

-- =============================================================================
-- 2. A production need not come from an idea
-- =============================================================================
alter table public.productions alter column idea_id         drop not null;
alter table public.productions alter column style_preset_id drop not null;

-- Nullable, but not optional: which two columns a production carries is decided
-- entirely by `source`, and neither shape may borrow from the other. Without
-- this, "an upload with a style preset" and "a generated production with no
-- idea" both become representable, and every reader downstream would have to
-- decide for itself what those mean.
alter table public.productions drop constraint if exists productions_origin_columns;
alter table public.productions add constraint productions_origin_columns check (
  case source
    when 'generated' then idea_id is not null and style_preset_id is not null
    when 'upload'    then idea_id is null     and style_preset_id is null
                          and coalesce(btrim(title), '') <> ''
                          and coalesce(btrim(brief), '') <> ''
    else false
  end
);

-- `productions_one_live_per_idea` is deliberately left exactly as it is. A
-- unique index treats nulls as distinct, so every uploaded production has a
-- null `idea_id` that collides with nothing -- which is the right answer, since
-- the guarantee it exists to make ("approving one idea does not render it
-- twice") has nothing to say about a video somebody uploaded.
--
-- `start_approved_productions` needs no change either, for the same reason:
-- its `not exists (select 1 from productions p where p.idea_id = i.id)` never
-- matches a null, so uploads are invisible to Gate 1 rather than blocking it.

-- =============================================================================
-- 3. A re-run needs a render to re-run
-- =============================================================================
--
-- `rerun_production` opens a fresh row at `submit_render` carrying the old
-- row's idea and style. An upload has neither, so the copy it made would fail
-- `productions_origin_columns` -- and if it somehow did not, it would be a row
-- that renders an idea nobody wrote. Refused with the answer instead: making
-- an uploaded video again means uploading it again.
create or replace function public.rerun_production(
  p_production_id uuid,
  p_style_id      uuid default null,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_old   public.productions;
  v_new   public.productions;
  v_style uuid;
  v_id    uuid := gen_random_uuid();
begin
  if not public.is_owner() then
    raise exception 'Only an owner may re-run a production' using errcode = '42501';
  end if;

  select * into v_old from public.productions where id = p_production_id;
  if v_old.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_old.source = 'upload' then
    raise exception 'This production is an uploaded cut, so there is nothing to make again. Upload the video again to put a new one through Gate 2.'
      using errcode = '22023';
  end if;
  if v_old.superseded_by is not null then
    raise exception 'That production has already been re-run' using errcode = 'P0002';
  end if;
  if not (
       v_old.status in ('parked', 'cancelled', 'rejected', 'published', 'failed')
    or (v_old.status = 'approved' and v_old.run_state ->> 'step' = 'publishing_disabled')
  ) then
    raise exception 'This production is still running. Cancel it first, or wait for it to finish.'
      using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_old);

  v_style := coalesce(p_style_id, v_old.style_preset_id);
  if not exists (select 1 from public.style_presets where id = v_style and is_active) then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  -- Mark the old row first: a `published` row is not excluded by status, so
  -- without this the insert below collides with the unique index. The foreign
  -- key is deferred, which is what makes this ordering legal.
  update public.productions set superseded_by = v_id where id = p_production_id;

  insert into public.productions
    (id, idea_id, style_preset_id, status, stage, cost_estimate_usd, run_state)
  select v_id, v_old.idea_id, v_style, 'queued', 'queued', sp.est_cost_max_usd,
         jsonb_build_object('rerun_of', p_production_id::text)
    from public.style_presets sp
   where sp.id = v_style
  returning * into v_new;

  perform public.record_control_event(
    p_production_id, 'rerun',
    'Re-run by an owner. This production was superseded by a new one.',
    p_note,
    jsonb_build_object('superseded_by', v_id, 'style_preset_id', v_style)
  );
  perform public.record_control_event(
    v_id, 'rerun',
    'Opened by an owner as a re-run of an earlier production.',
    p_note,
    jsonb_build_object('rerun_of', p_production_id, 'style_preset_id', v_style)
  );
  return v_new;
end;
$$;

revoke all on function public.rerun_production(uuid, uuid, text) from public, anon;
grant execute on function public.rerun_production(uuid, uuid, text) to authenticated;

-- =============================================================================
-- 4. Letting the browser put a file in the renders bucket
-- =============================================================================
--
-- `..._renders_bucket.sql` added no storage policies and said why: the pipeline
-- writes with the service role, which bypasses RLS, and the review UI reads
-- through signed URLs, which do not consult RLS at all. An upload is the first
-- write to that bucket that comes from a browser, so it is the first thing a
-- policy has to allow -- and the bucket stays private, so nothing here makes an
-- unreviewed cut reachable by guessing a path.
--
-- INSERT only, and that is the interesting half. Without an UPDATE policy an
-- upload to a path that already holds an object is refused rather than
-- overwriting it, so no owner can replace the cut behind a production that has
-- already been checked, reviewed or published. The pipeline can, because the
-- service role bypasses this entirely -- which is what `fetch_and_qc` needs to
-- re-check a render.
--
-- The path is `<production id>/final.mp4`: the id is minted in the browser and
-- carried into `create_upload_production` below, so the object lands where
-- every downstream step already looks for it and no rename is needed.
drop policy if exists renders_owner_upload on storage.objects;
create policy renders_owner_upload on storage.objects
  for insert to authenticated
  with check (bucket_id = 'renders' and public.is_owner());

-- =============================================================================
-- 5. Opening a production for an uploaded cut
-- =============================================================================
--
-- Owner-gated in Postgres like every other write in this app, and for the same
-- reason: there is no server tier, so a security-definer function is what keeps
-- "create the row" and "write the audit row" in one transaction, and what
-- refuses a viewer rather than trusting a disabled button.
--
-- It insists the file is already there. The browser uploads first and calls
-- this second, and if the order came apart -- an upload that failed, a call
-- fabricated by hand -- the production would be opened against nothing, the
-- worker would find no object, and it would park. Checking here turns that into
-- an error the uploader sees while they can still do something about it.
create or replace function public.create_upload_production(
  p_production_id uuid,
  p_title         text,
  p_brief         text,
  p_is_aigc       boolean,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.productions;
  v_key text := p_production_id::text || '/final.mp4';
begin
  if not public.is_owner() then
    raise exception 'Only an owner may upload a finished cut' using errcode = '42501';
  end if;

  if coalesce(btrim(p_title), '') = '' then
    raise exception 'An upload needs a title -- it is what the platform copy is written about'
      using errcode = '22023';
  end if;
  if coalesce(btrim(p_brief), '') = '' then
    raise exception 'An upload needs a short description. There is no script to write the captions from, so this is what they are written from instead.'
      using errcode = '22023';
  end if;
  if p_is_aigc is null then
    raise exception 'Say whether this video is AI-generated. The platforms require the disclosure and only you know the answer.'
      using errcode = '22023';
  end if;

  if not exists (
    select 1 from storage.objects where bucket_id = 'renders' and name = v_key
  ) then
    raise exception 'No uploaded file was found for this production. The upload did not finish -- try it again.'
      using errcode = 'P0002';
  end if;

  -- `status = 'queued'` with `run_state.step = 'check_upload'` is the whole of
  -- the entrance. `claim_production` admits a queued row at any step but
  -- `await_gate2`, so the next worker to tick picks this up and runs the
  -- quality check over it; `current_step` reads the step rather than defaulting
  -- to `write_script`, so nothing drafts a script for it and nothing renders.
  insert into public.productions
    (id, source, title, brief, is_aigc, status, stage, run_state)
  values
    (p_production_id, 'upload', btrim(p_title), btrim(p_brief), p_is_aigc,
     'queued', 'uploaded',
     jsonb_build_object('step', 'check_upload', 'storage_key', v_key))
  returning * into v_row;

  -- The upload has to be visible *as* an upload. `production_events` is what
  -- the review screen reads, and a production that arrived with a finished
  -- video and left no trace of arriving would read as a render that lost its
  -- log. This is a control event, so it carries the actor: somebody did this.
  perform public.record_control_event(
    p_production_id, 'upload',
    'A finished cut was uploaded by an owner. It skips idea approval, the script gate and the render, and goes straight to the quality check.',
    p_note,
    jsonb_build_object('storage_key', v_key, 'is_aigc', p_is_aigc)
  );

  return v_row;
end;
$$;

revoke all on function public.create_upload_production(uuid, text, text, boolean, text) from public, anon;
grant execute on function public.create_upload_production(uuid, text, text, boolean, text) to authenticated;
