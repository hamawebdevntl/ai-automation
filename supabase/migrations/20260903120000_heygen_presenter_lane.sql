-- HeyGen as the third render backend: the presenter lane.
--
-- This is the migration `20260902190000_pipeline_integration.sql` promised.
-- That one deactivated the seeded `ai-presenter` preset with the note "the
-- state machine has only a MoneyPrinterTurbo path, so approving it would
-- strand the job. Re-enable this when the HeyGen lane is built." The lane now
-- exists, so this re-enables it.
--
-- Unlike the two fal modes, HeyGen is not a *step* in a render -- it is the
-- whole render. It returns a finished 9:16 reel that is already voiced and
-- captioned, so nothing is handed to MoneyPrinterTurbo afterwards and none of
-- the MPT `VideoParams` in this preset's `params` apply to the output. Only
-- the script generator is shared, which is what keeps a presenter reel and a
-- stock reel on the same idea recognisably the same script.

alter table public.style_presets drop constraint if exists style_presets_render_mode_check;
alter table public.style_presets add constraint style_presets_render_mode_check
  check (render_mode in ('mpt', 'fal_visuals', 'fal_full', 'heygen'));

comment on column public.style_presets.render_mode is
  $$Which backend renders this preset.
    'mpt'         - MoneyPrinterTurbo does everything (script, voice, visuals, captions, assembly).
    'fal_visuals' - fal generates the clips; they are uploaded to MPT and it assembles, voices and captions as usual.
    'fal_full'    - fal generates visuals and narration; we assemble and burn captions ourselves.
    'heygen'      - HeyGen renders a finished presenter reel from our script; the pipeline only fetches and checks it.$$;

-- ---------------------------------------------------------------------------
-- The presenter preset.
--
-- `params.heygen` is the whole configuration of the lane, and every value in
-- it was read from the live account rather than guessed:
--
--   avatar_id  - a *portrait* look (1536x2752) from this organisation's own
--                avatar group. Portrait matters: every look in HeyGen's public
--                catalogue is landscape, and rendering one at 9:16 crops the
--                speaker to fill the frame. Starting from a portrait source
--                means no crop at all.
--   voice_id   - sent explicitly even though the API would fall back to the
--                avatar's default voice, so the narrator is a property of this
--                preset rather than of HeyGen's catalogue.
--   engine     - omitted deliberately, which selects Avatar IV. This look
--                advertises avatar_iii, avatar_iv and avatar_v, so the default
--                is valid. A preset naming a *studio* avatar from the public
--                catalogue must set 'engine': 'avatar_iii' -- those advertise
--                that engine only, and would otherwise fail on an engine they
--                never claimed to support.
--   burn_captions - true. Our quality check looks for a caption track, and in
--                a muted feed captions are the difference between a watched
--                reel and a skipped one. Note that this puts the captioned
--                render on a different URL than the clean cut; the pipeline
--                reads the one it actually got.
--
-- Cost is left at the $1-2 range STACK.md set, which is an estimate and not
-- yet a measurement: this account bills pay-as-you-go against a wallet, so the
-- first few real renders are what should replace these figures. Gate 1 shows
-- this range at the moment of choosing, which is the control on presenter
-- spend -- at ten reels a day an all-presenter week is $300-600/month against
-- $30-120 for stock.
-- ---------------------------------------------------------------------------

-- `params` is replaced rather than merged. It was seeded with MoneyPrinterTurbo
-- VideoParams -- subtitle_position, font_size, stroke_width, bgm_volume -- from
-- when this preset was expected to run through MPT. Nothing reads them on this
-- lane, and leaving dead knobs that look like live caption styling is how
-- someone later spends an afternoon wondering why changing font_size does
-- nothing.
update public.style_presets
set render_mode = 'heygen',
    is_active   = true,
    params      = jsonb_build_object(
      'lane', 'heygen',
      'heygen', jsonb_build_object(
        'avatar_id',     'e6e4d0f4b4704568b32e8de751179c83',
        'voice_id',      '506420c8af914cb6a3cc3c350ccb411d',
        'aspect_ratio',  '9:16',
        'resolution',    '1080p',
        'burn_captions', true,
        'paragraphs',    1
      )
    )
where slug = 'ai-presenter';

-- Recorded so a parked or completed production can say which backend ran
-- without the reader reconstructing it from a preset that may since have been
-- edited. 'heygen' now joins the values this column can hold.
comment on column public.productions.render_backend is
  'The render_mode in force when this production ran (mpt | fal_visuals | fal_full | heygen). Kept even if the preset later changes.';
