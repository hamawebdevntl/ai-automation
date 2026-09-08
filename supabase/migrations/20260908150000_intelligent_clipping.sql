-- =============================================================================
-- Intelligent clipping — propose candidate clips, let the owner pick
-- =============================================================================
--
-- Every lane before this one *creates* video. This one cuts video down: an owner
-- uploads a long recording, the pipeline transcribes it, a model proposes ranked
-- candidate clips with timecodes and a reason for each, and an owner accepts the
-- ones worth making. Nothing is rendered until they do.
--
-- The shape is copied deliberately from the script gate, whose argument was:
-- "the gate costs one LLM call and no video generation, so rejecting a script is
-- free in a way that rejecting a cut is not." The same is true twice over here.
-- Transcription and one structured LLM call produce the whole candidate list, so
-- an owner who discards all ten has spent cents; a top-N auto-cutter would have
-- spent ten renders to find that out.
--
--   upload -> transcribe -> propose -> [clip gate] -> N productions
--                                          ^
--                                          +-- the source rests here until a
--                                              person accepts or discards each
--                                              candidate, for minutes or weeks
--
-- What this file deliberately does NOT introduce is a fourth kind of waiting.
-- An accepted candidate becomes an ordinary `ideas` row and an ordinary
-- `productions` row, so it goes through the script gate and Gate 2 exactly as
-- everything else does, and `claim_production` is untouched.
--
-- The relationship question the issue asked us to settle
-- -----------------------------------------------------
-- `productions_one_live_per_idea` refuses a second live production per idea, so
-- N clips from one upload need either N idea rows or a different relationship.
-- **N idea rows**, sharing a parent in `ideas.clip_source_id`.
--
-- The alternative — one idea with N productions — would have meant weakening
-- that index, and it is the index that stopped a measured 44 renders being
-- opened for a single idea in two seconds (see
-- `20260907140000_one_production_per_approval.sql`). Trading that away to save
-- a row would be a poor bargain. Going the other way costs nothing: a clip's
-- idea row is a real idea, with a title and a hook, which is exactly what the
-- queue, the review screen and `write_script` already know how to read.

-- -----------------------------------------------------------------------------
-- 1. The bucket takes a long recording
-- -----------------------------------------------------------------------------
--
-- `20260908140000_source_video_lane.sql` set this to 500 MB and added the
-- `sources/` policies that this feature reuses wholesale — an owner uploading a
-- long recording is the same Storage write, under the same prefix, admitted by
-- the same three policies.
--
-- Only the ceiling has to move. 500 MB is right for the footage that lane takes
-- (whatever the owner shot on their phone, cut to a reel) and far too low for
-- this one, whose entire premise is a long recording: a one-hour 1080p screen
-- capture or webinar is routinely over a gigabyte. The failure it would cause is
-- the worst-shaped one available — a 413 at the end of a long upload, with the
-- transcription that would have justified it never attempted.
--
-- 5 GiB is Supabase's own per-object ceiling for a resumable upload, so this is
-- "as much as the platform allows" rather than a number with an argument behind
-- it. It costs nothing to raise: the bucket is private, only owners can write
-- under `sources/`, and a render still writes the same 20 MB reel it always did.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'renders', 'renders', false, 5368709120,  -- 5 GiB
  array['video/mp4', 'image/jpeg', 'video/quicktime', 'video/webm']
)
on conflict (id) do update
  set public             = false,
      file_size_limit    = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- -----------------------------------------------------------------------------
-- 2. clip_sources — one uploaded recording, and how far it has got
-- -----------------------------------------------------------------------------
--
-- This row is the request, the lock that stops two workers transcribing the same
-- file, and the record of what came back — the same three jobs `trend_runs` does
-- for a scout run, and for the same reason: there is no server tier, so the row
-- is the only place those three facts can live together.
create table if not exists public.clip_sources (
  id                uuid primary key default gen_random_uuid(),

  -- Where the bytes are, in the private `renders` bucket under `sources/`.
  -- Unique so that two rows can never claim the same object: transcribing one
  -- file twice is waste, but proposing candidates against a file another row
  -- also owns would attach one owner's clips to another's upload.
  storage_key       text not null unique,
  filename          text,
  content_type      text,
  size_bytes        bigint,

  -- Filled in by the pipeline once ffprobe has seen the file. Null until then,
  -- which is why nothing validates a candidate's timecodes against it at insert
  -- time — `pipeline.clips` clamps them instead, where the number is known.
  duration_seconds  numeric(10, 2),

  -- Which style the accepted clips render with. Chosen at upload rather than per
  -- candidate: the whole point of the gate is a fast yes/no on ten candidates,
  -- and a style picker on each one would make it ten Gate 1 decisions.
  style_preset_id   uuid not null references public.style_presets (id),

  status            text not null default 'uploaded' check (status in (
                      -- Bytes are in the bucket; nothing has read them yet.
                      'uploaded',
                      -- A worker holds it and fal is transcribing.
                      'transcribing',
                      -- Transcribed; the model is proposing candidates.
                      'proposing',
                      -- The gate. Candidates exist and a person has not
                      -- finished deciding. The row rests here.
                      'awaiting_picks',
                      -- Every candidate has been accepted or discarded.
                      'resolved',
                      -- Transcription or proposal failed. Retryable by an
                      -- owner; nothing has been rendered.
                      'failed'
                    )),

  -- The transcript, with timings. `{"text", "language", "model", "segments":
  -- [{"start", "end", "text"}]}` — written by `transcribe_source` and read by
  -- both `propose_candidates` and the caption burn-in, which is the reason the
  -- timings are stored rather than recomputed: the clip's captions come from the
  -- same segments the model chose its timecodes from, so they cannot disagree.
  transcript        jsonb not null default '{}'::jsonb,

  -- The most candidates the model may return for this upload.
  --
  -- Per-source rather than global because reviewer load is the binding
  -- constraint, not model capability — `docs/GAPS.md` B7 already flags two gates
  -- times ten reels a day as twenty unmodelled review actions, and this gate
  -- adds to that queue. An owner who wants a wider net for one long recording
  -- can ask for it without changing the default for every upload.
  candidate_cap     int not null default 6 check (candidate_cap between 1 and 20),

  error             text,

  -- The claim, in the shape `productions` uses. Not `trend_runs`' idiom, which
  -- is a conditional UPDATE on status: that works there because a run has one
  -- long phase, and this row has three, so it has to be claimable at each of
  -- them without the status being the lock.
  leased_by         text,
  lease_expires_at  timestamptz,

  uploaded_by       uuid references public.profiles (id),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

comment on table public.clip_sources is
  'A long recording an owner uploaded to be clipped. Holds the transcript, the review state, and the lease a worker takes while transcribing or proposing. One row per upload; the clips it produces are `ideas` rows pointing back at it.';
comment on column public.clip_sources.storage_key is
  'Object path in the private `renders` bucket, under `sources/clips/<source id>/`. Unique: two rows must never claim one upload.';
comment on column public.clip_sources.transcript is
  'Timestamped transcript: {"text", "language", "model", "segments": [{"start","end","text"}]}. The timings are the product here — the candidate timecodes and the burned-in captions both come from these segments, so they cannot drift apart.';
comment on column public.clip_sources.candidate_cap is
  'How many candidates the model may propose for this upload. Bounded because the constraint is reviewer attention, not model capability — see docs/GAPS.md B7.';
comment on column public.clip_sources.status is
  'uploaded -> transcribing -> proposing -> awaiting_picks -> resolved. `failed` is retryable and costs nothing: no render has happened at any point before `awaiting_picks` is passed.';

create index if not exists clip_sources_status_created_idx
  on public.clip_sources (status, created_at desc);

-- A failed source says why, and a resolved one does not.
alter table public.clip_sources drop constraint if exists clip_sources_failure_is_explained;
alter table public.clip_sources add constraint clip_sources_failure_is_explained
  check (status <> 'failed' or coalesce(btrim(error), '') <> '');

-- `updated_at` maintained by the same trigger function the rest of the schema
-- uses, so "when did this row last move?" means one thing across the database.
drop trigger if exists clip_sources_touch on public.clip_sources;
create trigger clip_sources_touch
  before update on public.clip_sources
  for each row execute function public.touch_updated_at();

-- -----------------------------------------------------------------------------
-- 3. clip_candidates — one proposed clip, and its decision
-- -----------------------------------------------------------------------------
--
-- The reviewable unit. Everything on it is text and numbers, which is the
-- feature: an owner decides from a title, a hook, a reason and two timecodes,
-- with a scrub preview of the source at the start point, and nothing has been
-- rendered to make that possible.
create table if not exists public.clip_candidates (
  id                 uuid primary key default gen_random_uuid(),
  source_id          uuid not null references public.clip_sources (id) on delete cascade,

  -- The model's own ranking, 1 = best. Unique per source so the list has one
  -- order rather than a stable-sort-dependent one.
  rank               int not null check (rank >= 1),

  start_seconds      numeric(10, 2) not null check (start_seconds >= 0),
  end_seconds        numeric(10, 2) not null,

  title              text not null,
  hook               text,
  -- Why this range stands alone without the rest of the recording. The model is
  -- required to state it, and it is shown to the owner: a candidate that cannot
  -- be justified in a sentence is one to discard.
  reason             text,
  -- The words actually spoken in the range, from the transcript. Becomes the
  -- production's script, so the owner reads exactly what the clip says before
  -- anything is cut.
  transcript_excerpt text,

  decision           text not null default 'pending'
                       check (decision in ('pending', 'accepted', 'discarded')),
  decided_by         uuid references public.profiles (id),
  decided_at         timestamptz,
  decision_note      text,

  -- Set when accepted. `on delete set null` rather than cascade: deleting the
  -- idea must not erase the record that this candidate was once accepted.
  idea_id            uuid references public.ideas (id) on delete set null,

  created_at         timestamptz not null default now(),

  constraint clip_candidates_rank_unique unique (source_id, rank)
);

comment on table public.clip_candidates is
  'One clip a model proposed from a `clip_sources` recording, and the owner''s decision on it. Reviewable entirely as text and timecodes: no render happens before a decision, which is what makes discarding one free.';
comment on column public.clip_candidates.reason is
  'The model''s stated reason this range stands alone without the rest of the recording. Required of the model and shown to the owner.';
comment on column public.clip_candidates.transcript_excerpt is
  'The words spoken between start_seconds and end_seconds. Becomes the production''s script, so an owner reads what the clip says before it is cut, and can correct a mis-transcription before it is burned in as a caption.';
comment on column public.clip_candidates.idea_id is
  'The idea created when this candidate was accepted. Null while pending or discarded. Each accepted candidate gets its own idea, which is what keeps `productions_one_live_per_idea` intact for a batch of clips.';

create index if not exists clip_candidates_source_rank_idx
  on public.clip_candidates (source_id, rank);
create index if not exists clip_candidates_pending_idx
  on public.clip_candidates (source_id) where decision = 'pending';

-- A clip runs forwards. Without this a model that swapped the two would produce
-- an ffmpeg command that silently outputs nothing.
alter table public.clip_candidates drop constraint if exists clip_candidates_runs_forwards;
alter table public.clip_candidates add constraint clip_candidates_runs_forwards
  check (end_seconds > start_seconds);

-- A clip nobody would publish is not a candidate. Both bounds are the platforms'
-- rather than ours: under about three seconds there is no hook, and every one of
-- the four targets caps a short at ninety seconds or less.
alter table public.clip_candidates drop constraint if exists clip_candidates_publishable_length;
alter table public.clip_candidates add constraint clip_candidates_publishable_length
  check (end_seconds - start_seconds between 3 and 90);

-- A decision names its decider and its moment, or it is not a decision. The
-- same rule, written the same way, as `productions_script_approval_is_attributed`.
alter table public.clip_candidates drop constraint if exists clip_candidates_decision_is_attributed;
alter table public.clip_candidates add constraint clip_candidates_decision_is_attributed
  check (
    case decision
      when 'pending' then decided_at is null and decided_by is null
      else decided_at is not null and decided_by is not null
    end
  );

-- Only an accepted candidate has an idea, and every accepted one does.
alter table public.clip_candidates drop constraint if exists clip_candidates_idea_iff_accepted;
alter table public.clip_candidates add constraint clip_candidates_idea_iff_accepted
  check ((decision = 'accepted') = (idea_id is not null));

-- A candidate must say something.
alter table public.clip_candidates drop constraint if exists clip_candidates_title_not_empty;
alter table public.clip_candidates add constraint clip_candidates_title_not_empty
  check (coalesce(btrim(title), '') <> '');

-- -----------------------------------------------------------------------------
-- 4. ideas learns where a clip came from
-- -----------------------------------------------------------------------------
--
-- The parent link the issue asked for. Clips from one upload share
-- `clip_source_id`, which is what lets the queue group a batch of eight instead
-- of showing eight unrelated ideas that happen to have arrived together.
--
-- Both references are `on delete restrict`, and that is deliberate rather than
-- the default falling where it may. `on delete set null` would have been wrong
-- in a way that only shows up much later: deleting a recording would null both
-- ids while leaving `clip_start_seconds` and `clip_end_seconds` set, which
-- `ideas_clip_fields_are_complete` below refuses -- so the delete would fail
-- anyway, with a constraint name instead of a reason.
--
-- Restricting says the real rule: a recording that produced a clip cannot be
-- deleted out from under it. A published video has to stay explicable, and
-- "which recording is this from, and where in it?" is not a question to lose.
-- A recording whose candidates were all discarded created no ideas and deletes
-- freely, cascading its candidates.
alter table public.ideas add column if not exists clip_source_id    uuid references public.clip_sources (id) on delete restrict;
alter table public.ideas add column if not exists clip_candidate_id uuid references public.clip_candidates (id) on delete restrict;
alter table public.ideas add column if not exists clip_start_seconds numeric(10, 2);
alter table public.ideas add column if not exists clip_end_seconds   numeric(10, 2);

comment on column public.ideas.clip_source_id is
  'The uploaded recording this idea was clipped from. The shared parent for a batch: every clip accepted from one upload carries the same value, which is how the queue groups them.';
comment on column public.ideas.clip_candidate_id is
  'The candidate an owner accepted to create this idea.';
comment on column public.ideas.clip_start_seconds is
  'Where the cut starts in the source, in seconds. Copied from the candidate rather than read through it, so a re-run reproduces exactly the range the owner accepted.';
comment on column public.ideas.clip_end_seconds is
  'Where the cut ends in the source, in seconds.';

-- One idea per candidate, enforced rather than assumed. `accept_clip_candidate`
-- refuses a candidate that is not pending, so this should be unreachable; it is
-- here because the failure it prevents is two productions cutting the same range
-- and both reaching Gate 2, which reads as a bug in the model rather than in us.
create unique index if not exists ideas_one_per_clip_candidate
  on public.ideas (clip_candidate_id) where clip_candidate_id is not null;

-- The four columns travel together or not at all. A clip idea with no range
-- would render the whole recording.
alter table public.ideas drop constraint if exists ideas_clip_fields_are_complete;
alter table public.ideas add constraint ideas_clip_fields_are_complete
  check (
    (clip_source_id is null and clip_candidate_id is null
     and clip_start_seconds is null and clip_end_seconds is null)
    or
    (clip_source_id is not null and clip_candidate_id is not null
     and clip_start_seconds is not null and clip_end_seconds is not null
     and clip_end_seconds > clip_start_seconds)
  );

-- -----------------------------------------------------------------------------
-- 5. The `clip` render mode
-- -----------------------------------------------------------------------------
--
-- The only lane that calls no generative provider at all. Everything it does is
-- ffmpeg on a file we already hold: cut the range, reframe to 9:16, burn the
-- captions from the transcript that chose the range. So it is also the only lane
-- where "the render failed" never means "money was spent" — which is why
-- `submit_render` releases its claim on any failure on this lane, and why the
-- cost estimate below is the storage and the CPU rather than an API bill.
alter table public.style_presets drop constraint if exists style_presets_render_mode_check;
alter table public.style_presets add constraint style_presets_render_mode_check
  check (render_mode in ('mpt', 'fal_visuals', 'fal_full', 'heygen', 'fal_video', 'clip'));

comment on column public.style_presets.render_mode is
  $$Which backend renders this preset.
    'mpt'         - MoneyPrinterTurbo does everything (script, voice, visuals, captions, assembly).
    'fal_visuals' - fal generates the clips; they are uploaded to MPT and it assembles, voices and captions as usual.
    'fal_full'    - fal generates visuals and narration; we assemble and burn captions ourselves.
    'heygen'      - HeyGen renders a finished presenter reel from our script; the pipeline only fetches and checks it.
    'fal_video'   - fal transforms footage the owner uploaded, guided by an instruction the owner wrote and approved.
    'clip'        - no provider at all: cut a range out of an uploaded recording, reframe it to 9:16 and burn captions from its own transcript.$$;

comment on column public.productions.render_backend is
  'The render_mode in force when this production ran (mpt | fal_visuals | fal_full | heygen | fal_video | clip). Kept even if the preset later changes.';

-- Active, unlike the `fal-restyle` preset beside it, and the difference is the
-- whole argument: that one ships inactive because video-to-video is priced above
-- text-to-video and there is no spend cap yet. This one calls no paid API. Its
-- cost is a signed download, four ffmpeg passes and an upload, all of which the
-- pipeline already pays for on every other lane at `fetch_and_qc`.
--
-- The transcription and the one LLM call that produce the candidates are charged
-- against the *upload*, once, before any of this — not per clip.
insert into public.style_presets
  (slug, name, description, lane, video_source, render_mode,
   est_cost_min_usd, est_cost_max_usd, est_minutes, params, is_active, sort_order)
values
  (
    'clip-cut',
    'Clip from my recording',
    'Cut a moment out of a long recording you uploaded, reframe it to 9:16 and burn in captions from its own transcript. No generation, so no per-clip API cost — the transcription and the candidate list are charged once against the upload.',
    'stock',
    'local',
    'clip',
    0.00, 0.02, 3,
    jsonb_build_object(
      'clip', jsonb_build_object(
        -- How a 16:9 source becomes 9:16.
        --
        -- `crop` centre-crops. It is the default because the source on this lane
        -- is a recording of a person talking, and a centre crop of a talking
        -- head is what every clipping tool produces and what the platforms
        -- expect. It does remove the sides of the frame, which is the honest
        -- cost of a full-bleed reel.
        --
        -- `pad` pillarboxes instead, which is what `assemble.to_portrait` does
        -- for the generative lanes and for the reason recorded there: padding is
        -- visible and honest where cropping is silent. A preset that sets it
        -- trades screen area for keeping the whole frame — right for a slide, a
        -- screen recording or a two-shot, where the sides carry the content.
        --
        -- Speaker tracking is deliberately not offered. It would need per-frame
        -- detection and a smoothed crop path, which is a different size of
        -- feature and a new dependency; see the PR's follow-ups.
        'reframe', 'crop',
        'font_size', 60,
        'burn_captions', true
      )
    ),
    true,
    18
  )
on conflict (slug) do update
  set render_mode      = excluded.render_mode,
      video_source     = excluded.video_source,
      est_cost_min_usd = excluded.est_cost_min_usd,
      est_cost_max_usd = excluded.est_cost_max_usd,
      est_minutes      = excluded.est_minutes,
      params           = excluded.params,
      is_active        = excluded.is_active,
      description      = excluded.description;

-- -----------------------------------------------------------------------------
-- 6. approvals learns the third gate
-- -----------------------------------------------------------------------------
--
-- A clip decision is recorded the way every other gate decision is, in the same
-- append-only table, because the question it answers later is the same one:
-- who decided this, when, and what did they say about it.
--
-- Gate 3 rather than an unnumbered kind. It sits before Gate 1 in time — a
-- candidate becomes an idea, and the idea is then already approved — but
-- numbering it 0 would imply the queue has a stage before ideas for everything,
-- which it does not: this gate exists only on the clipping path.
alter table public.approvals drop constraint if exists approvals_gate_check;
alter table public.approvals drop constraint if exists approvals_gate_valid;
alter table public.approvals add constraint approvals_gate_valid check (gate in (1, 2, 3));

alter table public.approvals drop constraint if exists approvals_subject_type_check;
alter table public.approvals drop constraint if exists approvals_subject_type_valid;
alter table public.approvals add constraint approvals_subject_type_valid
  check (subject_type in ('idea', 'production', 'clip_candidate'));

comment on column public.approvals.gate is
  'Which gate this decision was made at. 1 = an idea and its style, 2 = a finished cut, 3 = a proposed clip candidate. Gate 3 only exists on the clipping path.';

-- -----------------------------------------------------------------------------
-- 7. Registering an upload
-- -----------------------------------------------------------------------------
--
-- The bytes go to Storage from the browser, under the `sources/` policies the
-- footage lane added; this is the row change that makes the object something the
-- pipeline will pick up. Two steps rather than one for the same reason
-- `attach_source_video` is two: there is no server tier to do both, and the row
-- is written second on purpose — a row pointing at an upload that failed is
-- worse than an object no row points at.
create or replace function public.create_clip_source(
  p_key           text,
  p_style_id      uuid,
  p_filename      text default null,
  p_content_type  text default null,
  p_bytes         bigint default null,
  p_candidate_cap int default null
)
returns public.clip_sources
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row    public.clip_sources;
  v_key    text := btrim(coalesce(p_key, ''));
  v_cap    int  := coalesce(p_candidate_cap, 6);
  v_prefix text := 'sources/clips/';
begin
  if not public.is_owner() then
    raise exception 'Only an owner may upload a recording to clip' using errcode = '42501';
  end if;

  -- The prefix is what the Storage policies admit, so a key outside it names an
  -- object this function's caller could not have written. `clips/` separates
  -- these from the footage lane's `sources/<production id>/` folders; a
  -- production id is a uuid and can never be the literal `clips`.
  if left(v_key, length(v_prefix)) <> v_prefix
     or btrim(substr(v_key, length(v_prefix) + 1)) = '' then
    raise exception 'A recording must be stored at %<something>, not at %',
      v_prefix, coalesce(nullif(v_key, ''), '<empty>')
      using errcode = '22023';
  end if;

  if not exists (select 1 from public.style_presets where id = p_style_id and is_active) then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  -- The cap is bounded by the column's own CHECK as well. Refusing here gives
  -- the number back in the message, which a constraint violation does not.
  if v_cap < 1 or v_cap > 20 then
    raise exception 'Ask for between 1 and 20 candidates, not %', v_cap using errcode = '22023';
  end if;

  insert into public.clip_sources
    (storage_key, filename, content_type, size_bytes, style_preset_id, candidate_cap, uploaded_by)
  values
    (v_key,
     nullif(btrim(coalesce(p_filename, '')), ''),
     nullif(btrim(coalesce(p_content_type, '')), ''),
     p_bytes,
     p_style_id,
     v_cap,
     auth.uid())
  returning * into v_row;

  return v_row;
end;
$$;

revoke all on function public.create_clip_source(text, uuid, text, text, bigint, int) from public, anon;
grant execute on function public.create_clip_source(text, uuid, text, text, bigint, int) to authenticated;

-- Another go at a source that failed to transcribe or propose.
--
-- Without this a `failed` row is a dead end holding an upload the owner paid to
-- transfer: the worker only claims `uploaded`, `transcribing` and `proposing`,
-- so nothing would ever look at it again. Costs nothing to allow — everything
-- before the gate is a transcription and one LLM call, and no render exists.
create or replace function public.retry_clip_source(p_source_id uuid)
returns public.clip_sources
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row    public.clip_sources;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may retry a recording' using errcode = '42501';
  end if;

  select * into v_row from public.clip_sources where id = p_source_id;
  if v_row.id is null then
    raise exception 'Recording % does not exist', p_source_id using errcode = 'P0002';
  end if;
  if v_row.status <> 'failed' then
    raise exception 'Only a failed recording can be retried; this one is %', v_row.status
      using errcode = 'P0002';
  end if;
  if v_row.lease_expires_at is not null and v_row.lease_expires_at > now() then
    raise exception 'A worker is running this recording right now — try again in a moment'
      using errcode = '55006';
  end if;

  -- Back to the phase whose output is missing rather than to the start: a
  -- transcript that took ten minutes to produce should not be thrown away
  -- because the LLM call after it failed.
  update public.clip_sources
     set status = case
                    when transcript ? 'segments' then 'proposing'
                    else 'uploaded'
                  end,
         error  = null
   where id = p_source_id
  returning * into v_row;

  return v_row;
end;
$$;

revoke all on function public.retry_clip_source(uuid) from public, anon;
grant execute on function public.retry_clip_source(uuid) to authenticated;

-- -----------------------------------------------------------------------------
-- 8. The clip gate
-- -----------------------------------------------------------------------------
--
-- Modelled on `approve_script`, and on the same argument: the decision *is* the
-- row change, written in one statement with the audit row, by a security-definer
-- function that refuses a viewer rather than trusting a disabled button.
--
-- Accepting is the expensive direction — it creates a production, which will
-- reach `submit_render` — so it is the one that does the work. Discarding writes
-- a decision and nothing else.

-- Shared tail: a source whose candidates have all been decided is finished with.
--
-- Called by both decisions so "is this recording still at the gate?" has one
-- answer. Deliberately not a trigger on `clip_candidates`: a trigger would also
-- fire on the pipeline's own inserts, and a source whose candidates have just
-- been written has none pending only because none exist yet.
create or replace function public.resolve_clip_source_if_decided(p_source_id uuid)
returns void
language sql
security definer
set search_path = public
as $$
  update public.clip_sources
     set status = 'resolved'
   where id = p_source_id
     and status = 'awaiting_picks'
     and not exists (
       select 1 from public.clip_candidates
        where source_id = p_source_id and decision = 'pending'
     );
$$;

revoke all on function public.resolve_clip_source_if_decided(uuid) from public, anon, authenticated;

-- Accept one candidate: it becomes an idea, and the idea becomes a production.
--
-- Both are created here rather than left to `start_approved_productions`, which
-- would also have worked. The reason is what the owner sees: they accepted four
-- candidates out of ten, and four productions should be in the queue when the
-- page settles, not up to five seconds later depending on which worker thread
-- asks first. The sweep is idempotent against this — it opens a production only
-- for an idea that has none at all.
create or replace function public.accept_clip_candidate(
  p_candidate_id uuid,
  p_note         text default null
)
returns public.clip_candidates
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cand   public.clip_candidates;
  v_source public.clip_sources;
  v_preset public.style_presets;
  v_idea   public.ideas;
  v_prod   public.productions;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may accept a clip' using errcode = '42501';
  end if;

  -- `for update` on the candidate, not the source: two owners accepting two
  -- different candidates of one recording is a legitimate concurrent action and
  -- must not serialise. What must not happen twice is one candidate being
  -- accepted, and `ideas_one_per_clip_candidate` is the backstop for that.
  select * into v_cand from public.clip_candidates where id = p_candidate_id for update;
  if v_cand.id is null then
    raise exception 'Clip candidate % does not exist', p_candidate_id using errcode = 'P0002';
  end if;
  if v_cand.decision <> 'pending' then
    raise exception 'That clip has already been %', v_cand.decision using errcode = 'P0002';
  end if;

  select * into v_source from public.clip_sources where id = v_cand.source_id;
  select * into v_preset from public.style_presets where id = v_source.style_preset_id;
  if v_preset.id is null then
    raise exception 'The style this recording was uploaded with no longer exists'
      using errcode = '22023';
  end if;

  -- The idea. Created already approved, because accepting the candidate *is*
  -- the Gate 1 decision: the owner has seen the title, the hook, the reason and
  -- the words, and has chosen the style once for the whole recording. Sending
  -- it to Gate 1 pending would ask them the same question twice — and B7's
  -- reviewer-load problem is the reason not to.
  insert into public.ideas
    (title, hook, angle, rationale, source, status, approved_style_id,
     decided_by, decided_at, decision_note,
     clip_source_id, clip_candidate_id, clip_start_seconds, clip_end_seconds)
  values
    (v_cand.title,
     v_cand.hook,
     -- The recording is the angle: it is where this claim comes from.
     'Clipped from ' || coalesce(v_source.filename, 'an uploaded recording') || '.',
     v_cand.reason,
     'clip',
     'approved',
     v_source.style_preset_id,
     auth.uid(),
     now(),
     p_note,
     v_source.id,
     v_cand.id,
     v_cand.start_seconds,
     v_cand.end_seconds)
  returning * into v_idea;

  -- The production. `cost_estimate_usd` from the preset, exactly as
  -- `start_approved_productions` reads it, so Gate 1's figure and this one
  -- cannot disagree.
  insert into public.productions
    (idea_id, style_preset_id, status, stage, cost_estimate_usd)
  values
    (v_idea.id, v_source.style_preset_id, 'queued', 'queued', v_preset.est_cost_max_usd)
  returning * into v_prod;

  update public.clip_candidates
     set decision      = 'accepted',
         decided_by    = auth.uid(),
         decided_at    = now(),
         decision_note = p_note,
         idea_id       = v_idea.id
   where id = p_candidate_id
  returning * into v_cand;

  -- One audit row, not two. Accepting the candidate and approving the idea it
  -- creates are the same click by the same person at the same moment; writing a
  -- gate 1 row beside this one would double every clip in any count of review
  -- actions, which is the number B7 is about. `style_preset_id` is carried the
  -- way `approve_idea` carries it, so the style that was in force is recorded.
  insert into public.approvals
    (gate, subject_type, subject_id, decision, note, style_preset_id, actor_id)
  values
    (3, 'clip_candidate', p_candidate_id, 'approved', p_note, v_source.style_preset_id, auth.uid());

  perform public.record_control_event(
    v_prod.id, 'clip_accepted',
    format('Accepted from a recording: %ss to %ss of %s.',
           v_cand.start_seconds, v_cand.end_seconds,
           coalesce(v_source.filename, 'an uploaded recording')),
    p_note,
    jsonb_build_object(
      'clip_source_id', v_source.id,
      'clip_candidate_id', p_candidate_id,
      'rank', v_cand.rank,
      'start_seconds', v_cand.start_seconds,
      'end_seconds', v_cand.end_seconds
    )
  );

  perform public.resolve_clip_source_if_decided(v_source.id);
  return v_cand;
end;
$$;

revoke all on function public.accept_clip_candidate(uuid, text) from public, anon;
grant execute on function public.accept_clip_candidate(uuid, text) to authenticated;

-- Discard one candidate. Nothing has been rendered, so nothing is wasted.
create or replace function public.discard_clip_candidate(
  p_candidate_id uuid,
  p_note         text default null
)
returns public.clip_candidates
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cand public.clip_candidates;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may discard a clip' using errcode = '42501';
  end if;

  select * into v_cand from public.clip_candidates where id = p_candidate_id for update;
  if v_cand.id is null then
    raise exception 'Clip candidate % does not exist', p_candidate_id using errcode = 'P0002';
  end if;
  if v_cand.decision <> 'pending' then
    raise exception 'That clip has already been %', v_cand.decision using errcode = 'P0002';
  end if;

  update public.clip_candidates
     set decision      = 'discarded',
         decided_by    = auth.uid(),
         decided_at    = now(),
         decision_note = p_note
   where id = p_candidate_id
  returning * into v_cand;

  insert into public.approvals
    (gate, subject_type, subject_id, decision, note, actor_id)
  values
    (3, 'clip_candidate', p_candidate_id, 'rejected', p_note, auth.uid());

  perform public.resolve_clip_source_if_decided(v_cand.source_id);
  return v_cand;
end;
$$;

revoke all on function public.discard_clip_candidate(uuid, text) from public, anon;
grant execute on function public.discard_clip_candidate(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 9. The worker's claim
-- -----------------------------------------------------------------------------
--
-- `claim_production`'s idiom rather than `claim_trend_run`'s, and the difference
-- matters. A trend run has one long phase, so a conditional UPDATE on its status
-- is a sufficient lock. A source has three — transcribe, propose, open the gate
-- — and has to be claimable at each without the status being the lock, because
-- the status is also what the *review UI* reads to say where it has got to.
--
-- `awaiting_picks` and `resolved` are absent from the predicate, which is the
-- whole of the gate: a source waiting for a person is never claimed, however
-- long it waits, and there is no timeout on it for the same reason Gate 2 has
-- none. `failed` is absent too — `retry_clip_source` is what readmits it.
create or replace function public.claim_clip_source(p_worker text, p_lease_seconds int)
returns public.clip_sources
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.clip_sources;
begin
  update public.clip_sources s
     set leased_by        = p_worker,
         lease_expires_at = now() + make_interval(secs => p_lease_seconds)
   where s.id = (
     select id
       from public.clip_sources
      where status in ('uploaded', 'transcribing', 'proposing')
        and (lease_expires_at is null or lease_expires_at < now())
      order by created_at
      for update skip locked
      limit 1
   )
  returning s.* into v_row;
  return v_row;
end;
$$;

revoke all on function public.claim_clip_source(text, int) from public, anon, authenticated;
grant execute on function public.claim_clip_source(text, int) to service_role;

comment on function public.claim_clip_source(text, int) is
  'Take one uploaded recording that still needs work. Never returns a source at `awaiting_picks` — that is the whole of the clip gate — nor a `failed` one, which `retry_clip_source` readmits.';

-- -----------------------------------------------------------------------------
-- 10. Row-level security
-- -----------------------------------------------------------------------------
--
-- The same shape as every other table here: authenticated users read, and
-- nobody writes from the browser. The only writers are the service-role worker,
-- which bypasses RLS, and the security-definer functions above — which is what
-- keeps "record the decision" and "write the audit row" in one transaction.
alter table public.clip_sources    enable row level security;
alter table public.clip_candidates enable row level security;

drop policy if exists clip_sources_read on public.clip_sources;
create policy clip_sources_read on public.clip_sources
  for select to authenticated using (true);

drop policy if exists clip_candidates_read on public.clip_candidates;
create policy clip_candidates_read on public.clip_candidates
  for select to authenticated using (true);

grant select on public.clip_sources    to authenticated;
grant select on public.clip_candidates to authenticated;
