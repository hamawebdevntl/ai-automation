-- =============================================================================
-- Seed — demo rows, enough to exercise both gates before the pipeline that
-- writes them for real exists.
--
-- This is a seed, not a migration: it runs only when asked for explicitly
--
--   supabase db push --include-seed
--
-- so an ordinary `db push` on a fresh environment does not carry demo content
-- into it. Safe to delete once real data flows:
--
--   delete from public.productions where task_id like 'demo-%';
--   delete from public.ideas where source = 'demo';
-- =============================================================================

with inserted_ideas as (
  insert into public.ideas
    (title, hook, angle, rationale, source, trend_keyword, velocity_ratio, velocity_label, target_platforms, status)
  values
    (
      'The 6-upload ceiling nobody mentions',
      'Your YouTube automation caps out at six posts a day — here is why.',
      'Explain the 10,000-unit quota against the 1,600-unit cost of videos.insert.',
      'Search interest in "youtube api quota" is running well above its own baseline this week.',
      'demo', 'youtube api quota', 3.40, 'breakout',
      array['youtube', 'linkedin'], 'pending'
    ),
    (
      'Why your scheduler quietly stopped posting',
      'It did not fail. It landed in an inbox and expired.',
      'Walk through what UPLOAD vs DIRECT_POST actually does to a queued post.',
      'Steady interest, but no one has covered the 24-hour expiry clearly.',
      'demo', 'tiktok direct post', 1.70, 'rising',
      array['tiktok', 'instagram'], 'pending'
    ),
    (
      'Measuring a trend against itself',
      'Absolute view counts lie. Baseline-relative velocity does not.',
      'Contrast raw view counts with a 75/25 window split scored against baseline.',
      'Evergreen framing that consistently outperforms for this audience.',
      'demo', 'trend velocity', 0.95, 'steady',
      array['linkedin', 'youtube'], 'pending'
    )
  returning id, title
)
select count(*) from inserted_ideas;

-- One finished cut sitting at Gate 2, and one that failed QC.
with approved_idea as (
  insert into public.ideas
    (title, hook, angle, source, trend_keyword, velocity_ratio, velocity_label, target_platforms,
     status, approved_style_id, decided_at)
  values
    (
      'Four platforms, four different captions',
      'One reel. Four pieces of writing. Most tools give you one.',
      'Show the same cut with per-platform copy side by side.',
      'demo', 'social scheduling', 2.10, 'rising',
      array['instagram', 'tiktok', 'youtube', 'linkedin'],
      'approved', (select id from public.style_presets where slug = 'stock-broll'), now() - interval '2 hours'
    ),
    (
      'The slideshow problem',
      'If every clip is four seconds, you did not make a video.',
      'Demonstrate slideshow risk scoring on a deliberately bad cut.',
      'demo', 'video editing', 1.20, 'steady',
      array['instagram', 'tiktok'],
      'approved', (select id from public.style_presets where slug = 'generative'), now() - interval '40 minutes'
    )
  returning id, title
)
insert into public.productions
  (idea_id, style_preset_id, status, stage, task_id, script, video_url, thumbnail_url,
   duration_seconds, qc, platform_copy, cost_estimate_usd, cost_actual_usd, completed_at)
select
  ai.id,
  case when ai.title like 'Four platforms%'
    then (select id from public.style_presets where slug = 'stock-broll')
    else (select id from public.style_presets where slug = 'generative') end,
  case when ai.title like 'Four platforms%' then 'awaiting_review' else 'qc_failed' end,
  'quality-check',
  'demo-' || left(ai.id::text, 8),
  case when ai.title like 'Four platforms%'
    then 'One reel is never one piece of writing. A TikTok caption, a YouTube title and description, a LinkedIn post and an Instagram caption are four different jobs for the same forty seconds of video...'
    else 'If every clip runs exactly four seconds and nothing moves inside the frame, what you have made is a slideshow with music...' end,
  'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
  null,
  case when ai.title like 'Four platforms%' then 38.4 else 41.2 end,
  case when ai.title like 'Four platforms%' then
    '{"passed": true, "slideshow_risk": 0.12, "checks": [
       {"key": "file_integrity",   "label": "File integrity",   "status": "pass", "detail": "h264 1080x1920, 30fps, no truncated frames"},
       {"key": "audio_levels",     "label": "Audio levels",     "status": "pass", "detail": "-16.2 LUFS integrated, peak -1.4 dBTP"},
       {"key": "caption_presence", "label": "Captions present", "status": "pass", "detail": "104 cues, 0 gaps over 2s"},
       {"key": "slideshow_risk",   "label": "Slideshow risk",   "status": "pass", "detail": "0.12 — motion detected in 9 of 10 clips"}
     ]}'::jsonb
  else
    '{"passed": false, "slideshow_risk": 0.81, "checks": [
       {"key": "file_integrity",   "label": "File integrity",   "status": "pass", "detail": "h264 1080x1920, 30fps"},
       {"key": "audio_levels",     "label": "Audio levels",     "status": "warn", "detail": "-21.8 LUFS integrated — quiet for mobile playback"},
       {"key": "caption_presence", "label": "Captions present", "status": "pass", "detail": "88 cues"},
       {"key": "slideshow_risk",   "label": "Slideshow risk",   "status": "fail", "detail": "0.81 — 8 of 10 clips are static holds at exactly 5.0s"}
     ]}'::jsonb
  end,
  case when ai.title like 'Four platforms%' then
    '{"instagram": {"caption": "One reel, four captions. Here is why that matters."},
      "tiktok":    {"caption": "your scheduler is writing one caption for four platforms"},
      "youtube":   {"title": "Why one caption never fits four platforms",
                    "description": "A short walkthrough of per-platform copy variants."},
      "linkedin":  {"caption": "Most scheduling tools accept one caption and fan it out. That is a formatting decision masquerading as a distribution strategy."}}'::jsonb
  else '{}'::jsonb end,
  case when ai.title like 'Four platforms%' then 0.25 else 1.60 end,
  case when ai.title like 'Four platforms%' then 0.22 else 1.84 end,
  now() - interval '15 minutes'
from approved_idea ai;
