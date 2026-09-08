-- =============================================================================
-- Choosing the presenter and the narrator, without a migration.
-- =============================================================================
--
-- `20260903120000_heygen_presenter_lane.sql` wrote an avatar id and a voice id
-- into `style_presets.params.heygen` as literals. That made the presenter a
-- deploy-time constant: changing who fronts a reel meant writing SQL, and
-- nobody could see what the account was even able to use. DEPLOY.md's answer
-- was a curl and a `jsonb_set`, which is a workaround rather than a feature.
--
-- There is no server tier here, and the browser must never hold HEYGEN_API_KEY,
-- so the picker cannot call HeyGen. It does what every other cross-boundary
-- thing in this system does: the worker writes what HeyGen says into Postgres,
-- and the app reads that. `heygen_looks` and `heygen_voices` are that cache,
-- and `heygen_catalogue` is the single row that asks for it to be refilled and
-- records how the refill went.
--
-- The two constraints below are why the picker validates rather than merely
-- collecting a string. Both are already documented in the lane's own code, and
-- both fail *terminally* -- `avatar_not_found` and `voice_not_found` are in
-- TERMINAL_CODES, so a bad pick parks a production after Gate 1 has already
-- been passed, having spent a review to learn it:
--
--   * Orientation. A 9:16 render from a landscape source crops the speaker to
--     fill the frame. Every look in HeyGen's public catalogue is landscape.
--   * Engine. Omitting `engine` selects Avatar IV; a look that advertises
--     `avatar_iii` only fails on an engine it never claimed to support.
--
-- Orientation is a warning -- a landscape look with `fit: cover` is a
-- deliberate choice someone may want. An engine the look does not advertise is
-- refused outright, because there is no configuration in which it works.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- The cache.
-- ---------------------------------------------------------------------------

create table if not exists public.heygen_looks (
  avatar_id         text primary key,
  name              text,
  preview_image_url text,
  preview_video_url text,

  -- 'portrait' | 'landscape' | 'square' | 'unknown'. Not a check constraint on
  -- a fixed list: this is HeyGen's word, normalised by the worker, and a value
  -- we have not seen before must land in the table rather than fail the whole
  -- refresh. 'unknown' is what the worker writes when it cannot tell.
  orientation       text not null default 'unknown',

  -- The engines this look advertises, lower-cased. Empty means the response
  -- said nothing about engines, which is treated as "no opinion" rather than
  -- "supports none" -- refusing every engine on a silent response would make
  -- the picker unusable the moment HeyGen renames the field.
  engines           text[] not null default '{}',

  default_voice_id  text,
  gender            text,
  ownership         text not null default 'private',
  seen_at           timestamptz not null default now()
);

comment on table public.heygen_looks is
  'Cache of GET /v3/avatars/looks, refilled by the worker. The app cannot call HeyGen, so this is what the picker lists.';
comment on column public.heygen_looks.orientation is
  'portrait | landscape | square | unknown. A 9:16 render from a landscape source crops the speaker to fill the frame.';
comment on column public.heygen_looks.engines is
  'Engines this look advertises. Omitting engine at submit time selects avatar_iv, so a look that advertises avatar_iii only must say so.';

create table if not exists public.heygen_voices (
  voice_id          text primary key,
  -- 'pending'  someone asked for this id and the worker has not looked yet
  -- 'ok'       GET /v3/voices/{id} resolved it; it can be saved
  -- 'unknown'  HeyGen does not recognise it on this account
  status            text not null default 'pending'
                      check (status in ('pending', 'ok', 'unknown')),
  name              text,
  language          text,
  gender            text,
  preview_audio_url text,
  error             text,
  requested_at      timestamptz not null default now(),
  resolved_at       timestamptz
);

comment on table public.heygen_voices is
  $$Voices resolved one at a time by GET /v3/voices/{id}.
    Not a mirror of GET /v3/voices: that is 3,089 entries over 62 pages on this
    account and does not contain the cloned voice the preset names. Resolving a
    single id is the only cheap way to tell "this account cannot use it" -- which
    fails terminally as voice_not_found -- from a working configuration.$$;

create table if not exists public.heygen_catalogue (
  id           boolean primary key default true,
  status       text not null default 'idle'
                 check (status in ('idle', 'requested', 'running', 'failed')),
  requested_at timestamptz,
  requested_by uuid references public.profiles (id) on delete set null,
  started_at   timestamptz,
  refreshed_at timestamptz,
  looks        integer,
  voices       integer,
  error        text,

  constraint heygen_catalogue_singleton check (id)
);

comment on table public.heygen_catalogue is
  'Single row. The request to refill the look and voice caches, the in-flight lock, and how the last refill went.';

insert into public.heygen_catalogue (id) values (true) on conflict (id) do nothing;

-- The voice the presenter preset already names, so the first refresh resolves
-- it and the settings page can say who the current narrator is by name rather
-- than by id. Seeded as pending, not as fact: nothing here has spoken to
-- HeyGen yet, and claiming the voice is usable before checking is exactly the
-- assumption this table exists to remove.
insert into public.heygen_voices (voice_id)
select sp.params -> 'heygen' ->> 'voice_id'
  from public.style_presets sp
 where sp.slug = 'ai-presenter'
   and coalesce(sp.params -> 'heygen' ->> 'voice_id', '') <> ''
on conflict (voice_id) do nothing;

-- ---------------------------------------------------------------------------
-- Validation, in one place.
--
-- Called by both writers below -- the preset default and the per-production
-- override -- because a rule enforced on one path and not the other is not a
-- rule. It raises rather than returning a verdict: every caller's only correct
-- response to an unusable pair is to refuse it.
-- ---------------------------------------------------------------------------
create or replace function public.presenter_choice(
  p_avatar_id text,
  p_voice_id  text,
  p_engine    text default null
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_look    public.heygen_looks;
  v_voice   public.heygen_voices;
  v_engine  text := nullif(btrim(coalesce(p_engine, '')), '');
  v_wanted  text;
begin
  select * into v_look from public.heygen_looks where avatar_id = p_avatar_id;
  if v_look.avatar_id is null then
    raise exception 'No look % in the cached HeyGen catalogue. Refresh it, or pick one that is listed.',
      coalesce(p_avatar_id, '(none)') using errcode = '22023';
  end if;

  select * into v_voice from public.heygen_voices where voice_id = p_voice_id;
  if v_voice.voice_id is null then
    raise exception 'Voice % has not been checked against HeyGen yet', coalesce(p_voice_id, '(none)')
      using errcode = '22023';
  end if;
  if v_voice.status <> 'ok' then
    raise exception 'Voice % is %, so it cannot be saved: %',
      p_voice_id, v_voice.status, coalesce(v_voice.error, 'HeyGen did not recognise it')
      using errcode = '22023';
  end if;

  -- Omitting `engine` at submit time is not "no engine", it is Avatar IV. So
  -- the thing to check against what the look advertises is the engine that
  -- will actually run, not the one that was typed.
  if cardinality(v_look.engines) > 0 then
    v_wanted := coalesce(v_engine, 'avatar_iv');
    if not (v_wanted = any (v_look.engines)) then
      raise exception
        'Look % (%) advertises % and not %. Rendering it on an engine it never claimed fails terminally.',
        v_look.avatar_id, coalesce(v_look.name, 'unnamed'),
        array_to_string(v_look.engines, ', '), v_wanted
        using errcode = '22023';
    end if;
  end if;

  -- Orientation is deliberately not checked here; it is a warning the picker
  -- shows, not a refusal. See the header.
  return jsonb_strip_nulls(jsonb_build_object(
    'avatar_id',   v_look.avatar_id,
    'avatar_name', v_look.name,
    'orientation', v_look.orientation,
    'voice_id',    v_voice.voice_id,
    'voice_name',  v_voice.name,
    'engine',      v_engine
  ));
end;
$$;

-- ---------------------------------------------------------------------------
-- The preset default.
--
-- A function rather than the owner's existing UPDATE on style_presets, for two
-- reasons: the validation above, and `params` is a single jsonb column holding
-- the whole lane configuration. A browser sending a replacement object would
-- silently drop aspect_ratio, resolution and burn_captions on any request
-- built from a stale read.
-- ---------------------------------------------------------------------------
create or replace function public.set_preset_presenter(
  p_preset_id uuid,
  p_avatar_id text,
  p_voice_id  text,
  p_engine    text default null
)
returns public.style_presets
language plpgsql
security definer
set search_path = public
as $$
declare
  v_preset public.style_presets;
  v_choice jsonb;
  v_heygen jsonb;
  v_engine text := nullif(btrim(coalesce(p_engine, '')), '');
begin
  if not public.is_owner() then
    raise exception 'Only an owner may change the presenter' using errcode = '42501';
  end if;

  select * into v_preset from public.style_presets where id = p_preset_id;
  if v_preset.id is null then
    raise exception 'Unknown style preset %', p_preset_id using errcode = 'P0002';
  end if;
  if v_preset.render_mode <> 'heygen' then
    raise exception 'Preset % renders with %, which has no presenter to choose',
      v_preset.slug, v_preset.render_mode using errcode = '22023';
  end if;

  v_choice := public.presenter_choice(p_avatar_id, p_voice_id, v_engine);

  -- Merged into whatever `params.heygen` already holds rather than replacing
  -- it: aspect_ratio, resolution, burn_captions and paragraphs live there too,
  -- and this function is about two of the keys, not the object.
  v_heygen := coalesce(v_preset.params -> 'heygen', '{}'::jsonb)
              || jsonb_build_object('avatar_id', v_choice ->> 'avatar_id',
                                    'voice_id',  v_choice ->> 'voice_id');
  if v_engine is null then
    -- Removed rather than written as null. `create_avatar_video` sends the key
    -- only when it is truthy, so a null would change nothing at render time
    -- while reading, in the row, like a decision someone made.
    v_heygen := v_heygen - 'engine';
  else
    v_heygen := v_heygen || jsonb_build_object('engine', v_engine);
  end if;

  update public.style_presets
     set params = jsonb_set(params, '{heygen}', v_heygen, true)
   where id = p_preset_id
  returning * into v_preset;

  return v_preset;
end;
$$;

revoke all on function public.presenter_choice(text, text, text) from public, anon;
revoke all on function public.set_preset_presenter(uuid, text, text, text) from public, anon;
grant execute on function public.presenter_choice(text, text, text) to authenticated;
grant execute on function public.set_preset_presenter(uuid, text, text, text) to authenticated;

-- ---------------------------------------------------------------------------
-- The per-production override, chosen at Gate 1.
--
-- It rides on the idea rather than on the production because at the moment the
-- owner picks, the production does not exist: `start_approved_productions`
-- opens it a few seconds later. The production is where it is *recorded* --
-- `productions.presenter`, written by the render step with what actually ran,
-- the way `render_backend` already records the backend.
-- ---------------------------------------------------------------------------
alter table public.ideas
  add column if not exists presenter_override jsonb;

comment on column public.ideas.presenter_override is
  'Avatar and voice chosen for this one production at Gate 1, overriding the preset. Null uses the preset.';

alter table public.productions
  add column if not exists presenter jsonb;

comment on column public.productions.presenter is
  'The avatar and voice this production actually rendered with, and whether they came from the preset or a Gate 1 override. Kept even if the preset later changes.';

-- The three-argument form is dropped rather than left beside the new one:
-- PostgREST resolves by argument name, and two candidates matching
-- {p_idea_id, p_style_id, p_note} is ambiguous at the wire level.
drop function if exists public.approve_idea(uuid, uuid, text);

create or replace function public.approve_idea(
  p_idea_id   uuid,
  p_style_id  uuid,
  p_note      text default null,
  p_presenter jsonb default null
)
returns public.ideas
language plpgsql
security definer
set search_path = public
as $$
declare
  v_idea   public.ideas;
  v_preset public.style_presets;
  v_choice jsonb;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may approve an idea' using errcode = '42501';
  end if;

  select * into v_preset from public.style_presets where id = p_style_id and is_active;
  if v_preset.id is null then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  if p_presenter is not null then
    if v_preset.render_mode <> 'heygen' then
      raise exception 'Preset % renders with %, so a presenter cannot be chosen for it',
        v_preset.slug, v_preset.render_mode using errcode = '22023';
    end if;
    -- Validated here rather than at render time on purpose: an avatar this
    -- account cannot use fails terminally, and terminal failures happen after
    -- the gate, having already spent the review.
    v_choice := public.presenter_choice(
      p_presenter ->> 'avatar_id',
      p_presenter ->> 'voice_id',
      p_presenter ->> 'engine'
    );
  end if;

  update public.ideas
     set status             = 'approved',
         approved_style_id  = p_style_id,
         presenter_override = v_choice,
         decided_by         = auth.uid(),
         decided_at         = now(),
         decision_note      = p_note
   where id = p_idea_id
     and status = 'pending'
  returning * into v_idea;

  if v_idea.id is null then
    raise exception 'Idea % is not pending', p_idea_id using errcode = 'P0002';
  end if;

  insert into public.approvals (gate, subject_type, subject_id, decision, note, style_preset_id, actor_id)
  values (1, 'idea', p_idea_id, 'approved', p_note, p_style_id, auth.uid());

  return v_idea;
end;
$$;

revoke all on function public.approve_idea(uuid, uuid, text, jsonb) from public, anon;
grant execute on function public.approve_idea(uuid, uuid, text, jsonb) to authenticated;

-- ---------------------------------------------------------------------------
-- Asking the worker for something.
--
-- Both of these record an intention and return; the worker's
-- `refresh_presenter_catalogue` sweep acts on it within a minute. Same shape
-- as `request_trend_run`, and for the same reason -- the app has no way to
-- reach an external API itself.
-- ---------------------------------------------------------------------------
create or replace function public.request_heygen_catalogue_refresh()
returns public.heygen_catalogue
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.heygen_catalogue;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may refresh the HeyGen catalogue' using errcode = '42501';
  end if;

  -- A refresh already under way is doing exactly what was asked, so pressing
  -- the button again is not an error -- it returns the run in flight. Only a
  -- second *queued* request would be pointless, and the WHERE prevents it by
  -- leaving the existing one alone.
  update public.heygen_catalogue
     set status       = 'requested',
         requested_at = now(),
         requested_by = auth.uid(),
         error        = null
   where id = true
     and status in ('idle', 'failed')
  returning * into v_row;

  if v_row.id is null then
    select * into v_row from public.heygen_catalogue where id = true;
  end if;
  return v_row;
end;
$$;

create or replace function public.request_heygen_voice(p_voice_id text)
returns public.heygen_voices
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id  text := btrim(coalesce(p_voice_id, ''));
  v_row public.heygen_voices;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may add a voice' using errcode = '42501';
  end if;
  if v_id = '' then
    raise exception 'A voice id is required' using errcode = '22023';
  end if;

  insert into public.heygen_voices (voice_id)
  values (v_id)
  on conflict (voice_id) do update
    -- Re-asking about a voice HeyGen did not recognise is a real request: the
    -- id may have been fixed on their side, or typed wrong the first time. A
    -- voice already resolved is left exactly as it is, so the button is cheap.
    set status       = case when public.heygen_voices.status = 'unknown' then 'pending'
                            else public.heygen_voices.status end,
        error        = case when public.heygen_voices.status = 'unknown' then null
                            else public.heygen_voices.error end,
        requested_at = now()
  returning * into v_row;

  -- Nudge the sweep rather than waiting for its next periodic pass, so the
  -- owner sees a name instead of a spinner that outlives their attention.
  update public.heygen_catalogue
     set status = 'requested', requested_at = now(), requested_by = auth.uid()
   where id = true and status in ('idle', 'failed');

  return v_row;
end;
$$;

revoke all on function public.request_heygen_catalogue_refresh() from public, anon;
revoke all on function public.request_heygen_voice(text) from public, anon;
grant execute on function public.request_heygen_catalogue_refresh() to authenticated;
grant execute on function public.request_heygen_voice(text) to authenticated;

-- ---------------------------------------------------------------------------
-- Access. Read for anyone signed in; every write goes through a function above
-- or through the worker's service-role key. There is deliberately no INSERT,
-- UPDATE or DELETE policy on any of the three: a viewer watching the catalogue
-- refresh is useful, a viewer editing what the account can use is not.
-- ---------------------------------------------------------------------------
alter table public.heygen_looks     enable row level security;
alter table public.heygen_voices    enable row level security;
alter table public.heygen_catalogue enable row level security;

drop policy if exists heygen_looks_read on public.heygen_looks;
create policy heygen_looks_read on public.heygen_looks
  for select to authenticated using (true);

drop policy if exists heygen_voices_read on public.heygen_voices;
create policy heygen_voices_read on public.heygen_voices
  for select to authenticated using (true);

drop policy if exists heygen_catalogue_read on public.heygen_catalogue;
create policy heygen_catalogue_read on public.heygen_catalogue
  for select to authenticated using (true);
