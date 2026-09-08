-- =============================================================================
-- The source-footage lane — "here is a video, make something from it"
-- =============================================================================
--
-- Every render mode so far generates from *text*. `mpt` and `fal_visuals` fetch
-- or generate footage from the script, `fal_full` generates footage and speech,
-- and `heygen` delivers the script to a pre-built avatar. None of them accepts a
-- video as an input, so there was no way to say "here is footage, make something
-- from it".
--
-- This adds one input pair -- **a source video and a free-text instruction** --
-- and one render mode that consumes it, `fal_video`. The backend stays a
-- property of the preset, exactly as `20260902190400_fal_render_backend.sql`
-- decided: `render_mode` chooses the engine, `lane` describes the look, and
-- `productions.render_backend` records which one actually ran.
--
-- -----------------------------------------------------------------------------
-- Why there is no `heygen_video` here
-- -----------------------------------------------------------------------------
--
-- The brief asked for two paths, fal and HeyGen, and for a decision on which
-- HeyGen product the second one is. The decision is that neither candidate is
-- an "upload plus instructions" feature, so neither belongs behind this mode:
--
--   * **An avatar look built from footage of a real person** is not a new mode
--     at all. HeyGen builds the look through a consent-verified flow in its own
--     console -- that verification is the whole point of it, and it is not a
--     fire-and-forget API call -- and what comes out is an `avatar_id`. Rendering
--     our approved script through that id is `render_mode = 'heygen'`, which
--     already exists: it is a second presenter preset with a different
--     `params.heygen.avatar_id`, not a second lane.
--   * **Lip-syncing or translating existing footage** does take a video, but its
--     other input is a target language, not a free-text instruction. Wiring the
--     instruction box to it would either ignore what the owner typed -- the dead
--     knob `20260903120000_heygen_presenter_lane.sql` went out of its way to
--     delete -- or turn a text area into a language picker. It is a different
--     feature with a different input shape and it deserves its own issue.
--
-- So this migration builds the input pair once, for whichever modes need it, and
-- ships exactly one mode that consumes it.
--
-- -----------------------------------------------------------------------------
-- The gate
-- -----------------------------------------------------------------------------
--
-- An instruction is content a person authored, so it is held to the same rule as
-- the script: **nothing renders on it until a human has read it back.** That is
-- the script gate, reused rather than duplicated -- there is no fourth gate here.
-- Concretely:
--
--   * `save_render_instruction` and `attach_source_video` clear
--     `script_approved_at`, exactly as `save_script` does. Changing the footage
--     or the instruction un-approves the production, because approval is a
--     statement about *all* of what will be rendered.
--   * `approve_script` refuses to open the gate at all on a source lane whose
--     footage, instruction or consent is missing.
--   * `productions_source_lane_is_ready` refuses the `task_id` write underneath
--     both of them, so the rule holds against a stale worker as well as against
--     a browser.
--
-- -----------------------------------------------------------------------------
-- Consent
-- -----------------------------------------------------------------------------
--
-- `docs/GAPS.md` section C listed "voice-cloning consent if a real person's
-- voice is used" as deferred. Accepting uploaded footage makes it live: the
-- footage may show a person, and the system has no other way to know whether
-- they agreed to be in it. `source_consent_at` is that record, it names the
-- owner who made it, and it carries their own words about who is in the footage
-- and how they agreed -- which is the part a lawyer would ask for and a boolean
-- could not hold. It is required before the first render and it is cleared
-- whenever the footage changes, because consent is about a particular file.

-- -----------------------------------------------------------------------------
-- 1. The new render mode
-- -----------------------------------------------------------------------------

alter table public.style_presets drop constraint if exists style_presets_render_mode_check;
alter table public.style_presets add constraint style_presets_render_mode_check
  check (render_mode in ('mpt', 'fal_visuals', 'fal_full', 'heygen', 'fal_video'));

comment on column public.style_presets.render_mode is
  $$Which backend renders this preset.
    'mpt'         - MoneyPrinterTurbo does everything (script, voice, visuals, captions, assembly).
    'fal_visuals' - fal generates the clips; they are uploaded to MPT and it assembles, voices and captions as usual.
    'fal_full'    - fal generates visuals and narration; we assemble and burn captions ourselves.
    'heygen'      - HeyGen renders a finished presenter reel from our script; the pipeline only fetches and checks it.
    'fal_video'   - fal transforms footage the owner uploaded, guided by an instruction the owner wrote and approved.$$;

comment on column public.productions.render_backend is
  'The render_mode in force when this production ran (mpt | fal_visuals | fal_full | heygen | fal_video). Kept even if the preset later changes.';

-- Which modes read `productions.source_video_key`.
--
-- A function rather than a literal in five places, so "does this preset need
-- footage?" has one answer that the trigger, the gate and the browser's own
-- mirror of it are all checked against.
create or replace function public.render_mode_needs_source(p_mode text)
returns boolean
language sql
immutable
set search_path = public
as $$
  select coalesce(p_mode, 'mpt') in ('fal_video');
$$;

grant execute on function public.render_mode_needs_source(text) to authenticated, service_role;

comment on function public.render_mode_needs_source(text) is
  'Whether a render mode consumes uploaded source footage, and therefore requires a source video, an instruction and a consent record before it may be rendered.';

-- -----------------------------------------------------------------------------
-- 2. The inputs
-- -----------------------------------------------------------------------------
--
-- The footage lives in the `renders` bucket under `sources/<production_id>/`,
-- not in a bucket of its own. That bucket is already private, already sized for
-- video (500 MB), and already the one thing in this system that knows how to
-- mint a signed URL a provider can fetch -- which is exactly what fal needs.
-- Only the key is stored: the bytes are Storage's job, and a URL stored here
-- would be a URL that has since expired.

alter table public.productions add column if not exists source_video_key         text;
alter table public.productions add column if not exists source_video_name        text;
alter table public.productions add column if not exists source_video_bytes       bigint;
alter table public.productions add column if not exists source_video_uploaded_at timestamptz;
alter table public.productions add column if not exists source_video_uploaded_by uuid references public.profiles (id);

alter table public.productions add column if not exists render_instruction            text;
alter table public.productions add column if not exists render_instruction_updated_at timestamptz;
alter table public.productions add column if not exists render_instruction_updated_by uuid references public.profiles (id);

alter table public.productions add column if not exists source_consent_at   timestamptz;
alter table public.productions add column if not exists source_consent_by   uuid references public.profiles (id);
alter table public.productions add column if not exists source_consent_note text;

comment on column public.productions.source_video_key is
  'Object key of the uploaded source footage in the renders bucket, under sources/<production_id>/. The bytes are Storage''s; a signed URL is minted at submit time, because one stored here would have expired by the time it was read.';
comment on column public.productions.source_video_name is
  'The file name the owner uploaded, for the review screen. Never used to build the key.';
comment on column public.productions.source_video_bytes is
  'Size as reported by the upload, so the review screen can say what is attached without fetching it.';
comment on column public.productions.render_instruction is
  'What the owner asked the model to do with the footage. On the fal_video lane this is the prompt, verbatim -- which is why it is held to the same gate as the script.';
comment on column public.productions.source_consent_at is
  'When an owner recorded that the people in the uploaded footage agreed to it being used this way. Null means no source-lane render may be submitted. Cleared whenever the footage changes.';
comment on column public.productions.source_consent_note is
  'The owner''s own words: who is in the footage, and how they agreed. A boolean would not survive being asked about a year later.';

-- The longest instruction any lane accepts. fal's own prompt fields are far
-- longer than this, but an instruction is a direction rather than a document,
-- and `_submit_fal_video` truncates its prompt at 1500 characters -- so a cap
-- the owner can see beats silent truncation of words they approved.
alter table public.productions drop constraint if exists productions_render_instruction_length;
alter table public.productions add constraint productions_render_instruction_length
  check (render_instruction is null or length(render_instruction) <= 1500);

-- Each of the three facts names who recorded it, the way
-- `productions_script_approval_is_attributed` does. Set together or not at all.
alter table public.productions drop constraint if exists productions_source_video_is_attributed;
alter table public.productions add constraint productions_source_video_is_attributed
  check (
    (source_video_key is null) = (source_video_uploaded_at is null)
    and (source_video_key is null) = (source_video_uploaded_by is null)
  );

alter table public.productions drop constraint if exists productions_source_consent_is_attributed;
alter table public.productions add constraint productions_source_consent_is_attributed
  check ((source_consent_at is null) = (source_consent_by is null));

alter table public.productions drop constraint if exists productions_instruction_is_attributed;
alter table public.productions add constraint productions_instruction_is_attributed
  check ((render_instruction is null) = (render_instruction_updated_at is null));

-- Consent without footage is not wrong so much as meaningless, and it is how a
-- stale consent record survives a replaced file. Every function below clears the
-- two together; this is what holds if one ever forgets.
alter table public.productions drop constraint if exists productions_consent_needs_footage;
alter table public.productions add constraint productions_consent_needs_footage
  check (source_consent_at is null or source_video_key is not null);

-- -----------------------------------------------------------------------------
-- 3. The backstop: a source lane cannot be paid for without its inputs
-- -----------------------------------------------------------------------------
--
-- The same job `productions_render_needs_approved_script` does for the script,
-- and it cannot be a CHECK for the same reason that one could be: the rule
-- depends on `style_presets.render_mode`, which lives on another table.
--
-- `task_id` is set by `claim_render_slot` at the instant of submitting to a
-- backend, so "task_id became non-null" is precisely "money is about to move".
-- Firing only on that transition is what keeps this off the path of every other
-- update -- a production is written to dozens of times and only one of those
-- writes is this one.
create or replace function public.assert_source_lane_ready()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  v_mode text;
begin
  select sp.render_mode into v_mode
    from public.style_presets sp
   where sp.id = new.style_preset_id;

  if not public.render_mode_needs_source(v_mode) then
    return new;
  end if;

  if new.source_video_key is null then
    raise exception 'This style renders from uploaded footage, and none is attached to production %', new.id
      using errcode = '22023';
  end if;
  if btrim(coalesce(new.render_instruction, ''), E' \t\n\r\f\v') = '' then
    raise exception 'This style renders from uploaded footage and needs an instruction saying what to do with it'
      using errcode = '22023';
  end if;
  if new.source_consent_at is null then
    raise exception 'No consent has been recorded for the footage attached to production %. Record it before rendering.', new.id
      using errcode = '22023';
  end if;
  return new;
end;
$$;

revoke all on function public.assert_source_lane_ready() from public, anon, authenticated;

drop trigger if exists productions_source_lane_is_ready on public.productions;
create trigger productions_source_lane_is_ready
  before insert or update of task_id on public.productions
  for each row
  when (new.task_id is not null)
  execute function public.assert_source_lane_ready();

-- -----------------------------------------------------------------------------
-- 4. Validating an instruction
-- -----------------------------------------------------------------------------
--
-- `clean_script`'s sibling, and deliberately its shape: the whitespace set is
-- named character by character because `btrim` with one argument strips spaces
-- and nothing else -- the bug `20260907170000_script_gate_whitespace.sql` exists
-- to record. A gate that can be passed by pressing Enter is not a gate.
create or replace function public.clean_render_instruction(p_instruction text)
returns text
language plpgsql
immutable
set search_path = public
as $$
declare
  v text := btrim(coalesce(p_instruction, ''), E' \t\n\r\f\v');
begin
  if v = '' then
    raise exception 'An instruction cannot be empty: it is what the model is told to do with your footage'
      using errcode = '22023';
  end if;
  if length(v) > 1500 then
    raise exception 'That instruction is % characters; the limit is 1500', length(v)
      using errcode = '22023';
  end if;
  return v;
end;
$$;

revoke all on function public.clean_render_instruction(text) from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 5. Attaching the footage
-- -----------------------------------------------------------------------------
--
-- The bytes go to Storage from the browser; this records that they arrived. Two
-- steps rather than one because there is no server tier to do both: the upload
-- is a Storage call the owner's own policy admits, and this is the row change
-- that makes it part of the production.
--
-- Attaching clears the script approval *and* the consent record. The approval,
-- because approval is a statement about everything that will be rendered and the
-- footage is now different; the consent, because consent is about a particular
-- file and this is a different file.
create or replace function public.attach_source_video(
  p_production_id uuid,
  p_key           text,
  p_name          text default null,
  p_bytes         bigint default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row    public.productions;
  v_key    text := btrim(coalesce(p_key, ''));
  v_prefix text := 'sources/' || p_production_id::text || '/';
  v_name   text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may attach source footage' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  -- The same window the script may be edited in, for the same reason: once
  -- `task_id` is set the render has been paid for against this footage, and
  -- swapping the file would only make the record wrong.
  perform public.assert_script_editable(v_row);

  -- The key has to name *this* production's own folder. Without this an owner
  -- could point one production's row at another's footage -- which would render
  -- a file whose consent record lives on a different row.
  v_name := case when left(v_key, length(v_prefix)) = v_prefix
                 then substr(v_key, length(v_prefix) + 1)
                 else null end;
  if v_name is null or v_name = '' or position('/' in v_name) > 0 then
    raise exception 'Source footage for this production must be stored at %<filename>', v_prefix
      using errcode = '22023';
  end if;

  update public.productions
     set source_video_key         = v_key,
         source_video_name        = coalesce(nullif(btrim(coalesce(p_name, '')), ''), v_name),
         source_video_bytes       = p_bytes,
         source_video_uploaded_at = now(),
         source_video_uploaded_by = auth.uid(),
         -- New footage is unconsented footage and an un-approved production.
         source_consent_at        = null,
         source_consent_by        = null,
         source_consent_note      = null,
         script_approved_at       = null,
         script_approved_by       = null
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'source_video_attached',
    'Source footage attached by an owner. Any earlier approval and consent are cleared, because both were about a different file.',
    null,
    jsonb_build_object('key', v_key, 'name', v_row.source_video_name, 'bytes', p_bytes)
  );
  return v_row;
end;
$$;

revoke all on function public.attach_source_video(uuid, text, text, bigint) from public, anon;
grant execute on function public.attach_source_video(uuid, text, text, bigint) to authenticated;

-- Detaching, for the owner who uploaded the wrong file. The object itself is
-- left in Storage: deleting it is a separate Storage call the owner's policy
-- also admits, and a row that no longer points at a file is the safe half to
-- write first.
create or replace function public.clear_source_video(
  p_production_id uuid
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.productions;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may remove source footage' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);

  update public.productions
     set source_video_key         = null,
         source_video_name        = null,
         source_video_bytes       = null,
         source_video_uploaded_at = null,
         source_video_uploaded_by = null,
         source_consent_at        = null,
         source_consent_by        = null,
         source_consent_note      = null,
         script_approved_at       = null,
         script_approved_by       = null
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'source_video_cleared',
    'Source footage removed by an owner. This production cannot render on a footage lane until another file is attached.',
    null,
    jsonb_build_object('key', null)
  );
  return v_row;
end;
$$;

revoke all on function public.clear_source_video(uuid) from public, anon;
grant execute on function public.clear_source_video(uuid) to authenticated;

-- -----------------------------------------------------------------------------
-- 6. Saving the instruction
-- -----------------------------------------------------------------------------
--
-- `save_script`'s sibling, and the same trade: it persists the words and leaves
-- the gate shut. There is no `approve_instruction`, on purpose -- approving is
-- `approve_script`, which is now a statement about the script *and* the
-- instruction together, because they are rendered together.
create or replace function public.save_render_instruction(
  p_production_id uuid,
  p_instruction   text
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_text text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may edit a render instruction' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);

  v_text := public.clean_render_instruction(p_instruction);

  update public.productions
     set render_instruction            = v_text,
         render_instruction_updated_at = now(),
         render_instruction_updated_by = auth.uid(),
         -- The edit is the un-approval, exactly as in `save_script`.
         script_approved_at            = null,
         script_approved_by            = null
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'instruction_saved',
    'Render instruction edited and saved by an owner. Not yet approved, so no render can start.',
    null,
    jsonb_build_object('chars', length(v_text))
  );
  return v_row;
end;
$$;

revoke all on function public.save_render_instruction(uuid, text) from public, anon;
grant execute on function public.save_render_instruction(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 7. Recording consent
-- -----------------------------------------------------------------------------
--
-- Its own action rather than a checkbox folded into approval, because it is a
-- different kind of claim: approval says "render this", consent says "the people
-- in this footage agreed to it". The first is reversible by editing; the second
-- is a fact about the world, and it is recorded with a note, a name and a time so
-- that it can be answered for later.
create or replace function public.confirm_source_consent(
  p_production_id uuid,
  p_note          text
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_note text := btrim(coalesce(p_note, ''), E' \t\n\r\f\v');
begin
  if not public.is_owner() then
    raise exception 'Only an owner may record consent for source footage' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);

  if v_row.source_video_key is null then
    raise exception 'There is no footage attached to this production to record consent for'
      using errcode = '22023';
  end if;
  -- A note, not a tick. "Yes" is not a record anyone can act on a year later,
  -- and this is the field a rights question is answered out of.
  if length(v_note) < 10 then
    raise exception 'Say who is in the footage and how they agreed -- at least a sentence'
      using errcode = '22023';
  end if;
  if length(v_note) > 2000 then
    raise exception 'That consent note is % characters; the limit is 2000', length(v_note)
      using errcode = '22023';
  end if;

  update public.productions
     set source_consent_at   = now(),
         source_consent_by   = auth.uid(),
         source_consent_note = v_note
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'source_consent_recorded',
    'An owner recorded consent for the uploaded footage.',
    v_note,
    jsonb_build_object('key', v_row.source_video_key)
  );
  return v_row;
end;
$$;

revoke all on function public.confirm_source_consent(uuid, text) from public, anon;
grant execute on function public.confirm_source_consent(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 8. The gate learns about the footage
-- -----------------------------------------------------------------------------
--
-- Replaced only to add the block below. Everything else is
-- `20260907160000_script_gate.sql` verbatim, and it is repeated rather than
-- patched because Postgres has no way to add a statement to a function.
--
-- Approving is the click that spends money, so this is where a source lane's
-- missing inputs have to be refused: the trigger in section 3 would refuse it
-- too, but by then the row has been handed back to the driver and the owner is
-- looking at a production that parks a few seconds later for a reason nobody
-- explained.
create or replace function public.approve_script(
  p_production_id uuid,
  p_script        text,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_text text;
  v_mode text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may approve a script' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);
  -- Here the lease *does* matter, unlike `save_script`: this hands the row back
  -- to the driver, and doing that while a worker holds it would have two
  -- writers moving the same production at once.
  perform public.assert_unleased(v_row);

  v_text := public.clean_script(p_script);

  -- The source lane's own preconditions. Read from the preset rather than from
  -- `render_backend`, which is not written until the render is submitted.
  select sp.render_mode into v_mode
    from public.style_presets sp
   where sp.id = v_row.style_preset_id;

  if public.render_mode_needs_source(v_mode) then
    if v_row.source_video_key is null then
      raise exception 'This style renders from your own footage. Upload a video before approving.'
        using errcode = '22023';
    end if;
    if btrim(coalesce(v_row.render_instruction, ''), E' \t\n\r\f\v') = '' then
      raise exception 'Write the instruction saying what to do with the footage before approving.'
        using errcode = '22023';
    end if;
    if v_row.source_consent_at is null then
      raise exception 'Record consent for the people in the footage before approving.'
        using errcode = '22023';
    end if;
  end if;

  update public.productions
     set script             = v_text,
         script_updated_at  = now(),
         script_updated_by  = auth.uid(),
         script_approved_at = now(),
         script_approved_by = auth.uid(),
         error              = null,
         -- Claimable again. `await_script` + `running` + a non-null
         -- `script_approved_at` is the exact combination `claim_production`
         -- admits, and all three are set in this one statement.
         status             = 'running',
         stage              = 'script approved',
         run_state          = (coalesce(v_row.run_state, '{}'::jsonb)
                                - 'due_at' - 'ended_at' - 'error')
                              || jsonb_build_object(
                                   'step', 'await_script',
                                   'previous_step',
                                     coalesce(v_row.run_state ->> 'step', 'write_script'),
                                   'lease_expiries', 0
                                 )
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'script_approved',
    'Script approved by an owner. The render may now be submitted against exactly these words.',
    p_note,
    jsonb_build_object(
      'chars', length(v_text),
      -- Recorded here as well as in its own event, so the one row a reader looks
      -- at to answer "what was approved?" carries the instruction too.
      'instruction_chars', length(coalesce(v_row.render_instruction, '')),
      'source_video_key', v_row.source_video_key
    )
  );
  return v_row;
end;
$$;

revoke all on function public.approve_script(uuid, text, text) from public, anon;
grant execute on function public.approve_script(uuid, text, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 9. The bucket, and the first policies on it
-- -----------------------------------------------------------------------------
--
-- `20260902190300_renders_bucket.sql` added no Storage policies and said why:
-- the pipeline writes with the service role, which bypasses RLS, and the review
-- UI reads signed URLs, which do not consult it. Neither is true of an upload
-- from the owner's browser, so this is the first thing in the system that needs
-- a policy -- and it is scoped as narrowly as the feature allows.
--
--   * `sources/` only. A finished cut stays unreadable to every client: the
--     `LIKE` in each policy is what keeps this from being a read policy over
--     every unreviewed render in the bucket.
--   * owners only, via `is_owner()`. The app has open signup, so `authenticated`
--     is not a trust boundary.
--   * no UPDATE policy. A re-upload writes a new object; overwriting one in
--     place would change footage a consent record already points at.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'renders', 'renders', false, 524288000,
  -- The three containers a phone or a camera actually produces, plus the two
  -- the pipeline already wrote. QuickTime is what an iPhone uploads, and
  -- rejecting it at the Storage layer would be a failure with no good message.
  array['video/mp4', 'image/jpeg', 'video/quicktime', 'video/webm']
)
on conflict (id) do update
  set public             = false,
      file_size_limit    = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "owners upload source footage" on storage.objects;
create policy "owners upload source footage"
  on storage.objects for insert to authenticated
  with check (bucket_id = 'renders' and name like 'sources/%' and public.is_owner());

drop policy if exists "owners read source footage" on storage.objects;
create policy "owners read source footage"
  on storage.objects for select to authenticated
  using (bucket_id = 'renders' and name like 'sources/%' and public.is_owner());

drop policy if exists "owners delete source footage" on storage.objects;
create policy "owners delete source footage"
  on storage.objects for delete to authenticated
  using (bucket_id = 'renders' and name like 'sources/%' and public.is_owner());

-- -----------------------------------------------------------------------------
-- 10. The preset
-- -----------------------------------------------------------------------------
--
-- **Inactive, and it must stay inactive until two things are true.** This is the
-- same decision `fal-end-to-end` was shipped with, for stronger reasons:
--
--   1. **A measured cost.** The figures below are derived from a published
--      per-second rate against a 20-40 second reel, not observed. Video-to-video
--      is priced above text-to-video, and this is the first lane where the
--      owner's own upload length feeds the bill.
--   2. **A spend cap.** There is none yet. Gate 1 showing a range is the only
--      control on spend today, and a range is not a ceiling.
--
-- `params.fal.model` is also unverified against fal's live catalogue -- the rest
-- of this repo says "checked against the live API", and this line cannot, so it
-- says so instead. An inactive preset cannot be approved by mistake, which is
-- what makes an unverified slug harmless rather than a bill.
insert into public.style_presets
  (slug, name, description, lane, video_source, render_mode,
   est_cost_min_usd, est_cost_max_usd, est_minutes, params, is_active, sort_order)
values
  (
    'fal-restyle',
    'Restyle my footage (fal)',
    'Upload a video and say what to do with it. fal generates a new reel from your footage rather than from a stock library or a text prompt. The most expensive lane, and the only one whose cost depends on how long a file you upload.',
    'generative',
    'fal',
    'fal_video',
    2.00, 8.00, 14,
    jsonb_build_object(
      'fal', jsonb_build_object(
        -- Pinned rather than resolved at runtime, like every other fal preset:
        -- the model decides both the payload shape and the bill.
        --
        -- NOT YET CONFIRMED against fal's catalogue, and the reason this preset
        -- is inactive. Whoever activates it must check the slug, the payload
        -- field names below, and the rate.
        'model', 'fal-ai/ltx-2.3/video-to-video',
        'usd_per_second', 0.20,
        'resolution', '1080p',
        -- Asked for, because the quality check *fails* anything that is not
        -- 9:16 and the source is whatever the owner had on their phone. A model
        -- that honours it turns a landscape upload into a publishable reel; one
        -- that ignores it produces a landscape render that reaches Gate 2
        -- flagged, having been paid for.
        'aspect_ratio', '9:16',
        -- A ceiling on the bill as well as on the runtime: the cost is
        -- per-second of *output*, so this is the only number standing between a
        -- ten-minute upload and a ten-minute charge.
        'max_duration_seconds', 40,
        -- How closely the output follows the upload. Low keeps the footage and
        -- restyles it; high treats it as a suggestion.
        'strength', 0.65
      )
    ),
    false,
    17
  )
on conflict (slug) do update
  set render_mode      = excluded.render_mode,
      video_source     = excluded.video_source,
      est_cost_min_usd = excluded.est_cost_min_usd,
      est_cost_max_usd = excluded.est_cost_max_usd,
      est_minutes      = excluded.est_minutes,
      params           = excluded.params,
      description      = excluded.description;
